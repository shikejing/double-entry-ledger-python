"""账本服务：核心业务逻辑。

一致性保证：
- 原子性：一笔交易的所有分录与余额校验在同一个 SQLite 事务内完成，整体成功或整体失败；
- 幂等性：以 idempotency_key 作为唯一业务标识，重复提交返回先前结果；
- 并发控制：写事务使用 BEGIN IMMEDIATE 立即获取写锁，配合 busy_timeout 使并发写串行化，
  分录追加式写入，余额由分录聚合得到，从根本上避免丢失更新；
- 冲正：已入账交易只能通过一笔方向相反的新交易抵消，原分录追加保留、不可改删。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Optional, Sequence

from .errors import (
    AccountClosedError,
    AccountExistsError,
    AccountNotFoundError,
    AlreadyReversedError,
    BalanceViolationError,
    InsufficientEntriesError,
    InvalidAmountError,
    InvalidDirectionError,
    TransactionNotFoundError,
    UnbalancedTransactionError,
)
from .models import (
    NORMAL_DIRECTION,
    Account,
    Balance,
    Direction,
    SubjectType,
    Transaction,
    TransactionStatus,
)
from .repository import AccountRepository, EntryRepository, TransactionRepository


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_direction(direction: Direction) -> None:
    if not isinstance(direction, Direction) or direction not in (Direction.DEBIT, Direction.CREDIT):
        raise InvalidDirectionError(f"非法记账方向: {direction!r}，必须为 debit 或 credit")


def _validate_amount(amount: int) -> None:
    if not isinstance(amount, int) or isinstance(amount, bool):
        raise InvalidAmountError(f"金额必须为整数（分），收到: {amount!r}")
    if amount <= 0:
        raise InvalidAmountError(f"金额必须为正整数（分），收到: {amount}")


class LedgerService:
    """账本服务。

    线程安全：写事务通过 BEGIN IMMEDIATE + busy_timeout 串行化并发写，可在多线程环境使用。
    推荐每个线程使用独立连接；若共享连接，写操作的事务控制仍保证串行与原子。
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.accounts = AccountRepository(conn)
        self.transactions = TransactionRepository(conn)
        self.entries = EntryRepository(conn)

    def _begin(self) -> None:
        self.conn.execute("BEGIN IMMEDIATE")

    def _commit(self) -> None:
        self.conn.execute("COMMIT")

    def _rollback(self) -> None:
        try:
            self.conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass

    # ------------------------------------------------------------------
    # 账户与科目
    # ------------------------------------------------------------------
    def create_account(
        self,
        *,
        code: str,
        name: str,
        subject_type: SubjectType,
    ) -> Account:
        """创建账户（会计科目）。正常余额方向由科目类型自动决定，避免方向不一致。"""
        if not isinstance(subject_type, SubjectType):
            raise ValueError(f"非法科目类型: {subject_type!r}")
        if not code or not code.strip():
            raise ValueError("科目代码不能为空")
        if not name or not name.strip():
            raise ValueError("科目名称不能为空")
        normal_direction = NORMAL_DIRECTION[subject_type]
        try:
            return self.accounts.create(
                code=code.strip(),
                name=name.strip(),
                subject_type=subject_type,
                normal_direction=normal_direction,
                created_at=_now(),
            )
        except sqlite3.IntegrityError as exc:
            if "UNIQUE" in str(exc).upper() and "code" in str(exc).lower():
                raise AccountExistsError(f"科目代码已存在: {code!r}") from exc
            raise

    def get_account(self, account_id: int) -> Account:
        account = self.accounts.get_by_id(account_id)
        if account is None:
            raise AccountNotFoundError(f"账户不存在: id={account_id}")
        return account

    def get_account_by_code(self, code: str) -> Account:
        account = self.accounts.get_by_code(code)
        if account is None:
            raise AccountNotFoundError(f"账户不存在: code={code!r}")
        return account

    def list_accounts(self) -> list[Account]:
        return self.accounts.list_all()

    # ------------------------------------------------------------------
    # 记账
    # ------------------------------------------------------------------
    def post_transaction(
        self,
        *,
        idempotency_key: str,
        entries: Sequence[tuple[int, Direction, int]],
        description: Optional[str] = None,
    ) -> Transaction:
        """提交一笔交易（记账）。

        entries: (account_id, direction, amount_cents) 序列，至少两条，借贷必须相等。
        幂等：同一 idempotency_key 重复提交返回先前结果，不重复入账。
        """
        if not idempotency_key or not idempotency_key.strip():
            raise ValueError("幂等业务标识 idempotency_key 不能为空")
        key = idempotency_key.strip()

        # 先做不依赖事务的格式校验（尽早拒绝非法输入）
        normalized = self._validate_entries_shape(entries)

        self._begin()
        try:
            # 幂等检查：已存在则直接返回先前结果
            existing = self.transactions.get_by_key(key)
            if existing is not None:
                self._commit()
                return existing

            # 校验账户存在且可用
            accounts: dict[int, Account] = {}
            for account_id, _, _ in normalized:
                if account_id in accounts:
                    continue
                account = self.accounts.get_by_id(account_id)
                if account is None:
                    raise AccountNotFoundError(f"账户不存在: id={account_id}")
                if account.status.value != "active":
                    raise AccountClosedError(f"账户已停用，不能记账: id={account_id}")
                accounts[account_id] = account

            # 余额方向一致性校验：记账后不得出现与正常方向相反的余额
            self._check_balance_direction(accounts, normalized)

            # 写入交易与分录（同一事务，整体成功或整体失败）
            tx = self.transactions.create(
                idempotency_key=key,
                description=description,
                status=TransactionStatus.POSTED,
                reversal_of=None,
                created_at=_now(),
            )
            for account_id, direction, amount in normalized:
                self.entries.create(
                    transaction_id=tx.id,
                    account_id=account_id,
                    direction=direction,
                    amount=amount,
                )
            result = self.transactions.get_by_id(tx.id)
            self._commit()
            return result
        except Exception:
            self._rollback()
            raise

    def _validate_entries_shape(
        self, entries: Sequence[tuple[int, Direction, int]]
    ) -> list[tuple[int, Direction, int]]:
        if entries is None:
            raise InsufficientEntriesError("分录列表不能为空")
        if len(entries) < 2:
            raise InsufficientEntriesError(
                f"一笔交易至少包含 2 条分录，当前 {len(entries)} 条"
            )
        normalized: list[tuple[int, Direction, int]] = []
        debit_total = 0
        credit_total = 0
        for idx, item in enumerate(entries):
            if not isinstance(item, (tuple, list)) or len(item) != 3:
                raise InvalidAmountError(f"第 {idx + 1} 条分录格式非法，应为 (account_id, direction, amount)")
            account_id, direction, amount = item
            _validate_direction(direction)
            _validate_amount(amount)
            if not isinstance(account_id, int) or isinstance(account_id, bool) or account_id <= 0:
                raise AccountNotFoundError(f"第 {idx + 1} 条分录账户 id 非法: {account_id!r}")
            normalized.append((account_id, direction, amount))
            if direction == Direction.DEBIT:
                debit_total += amount
            else:
                credit_total += amount
        if debit_total != credit_total:
            raise UnbalancedTransactionError(
                f"借贷不平：借方合计 {debit_total} 分，贷方合计 {credit_total} 分，差额 {debit_total - credit_total} 分"
            )
        return normalized

    def _check_balance_direction(
        self,
        accounts: dict[int, Account],
        entries: Sequence[tuple[int, Direction, int]],
    ) -> None:
        """校验记账后各账户余额方向与科目类型一致，不得出现反向透支余额。"""
        delta: dict[int, int] = {}
        for account_id, direction, amount in entries:
            account = accounts[account_id]
            if direction == account.normal_direction:
                delta[account_id] = delta.get(account_id, 0) + amount
            else:
                delta[account_id] = delta.get(account_id, 0) - amount
        for account_id, net in delta.items():
            current = self.entries.balance(account_id)
            projected = current + net
            if projected < 0:
                account = accounts[account_id]
                raise BalanceViolationError(
                    f"账户 {account.code}({account.name}) 记账后余额为 {projected} 分，"
                    f"与科目正常方向 {account.normal_direction.value} 相反，拒绝入账"
                )

    # ------------------------------------------------------------------
    # 冲正
    # ------------------------------------------------------------------
    def reverse_transaction(
        self,
        *,
        original_key: str,
        reversal_key: str,
        description: Optional[str] = None,
    ) -> Transaction:
        """对已入账交易进行冲正。

        冲正生成一笔方向相反的新交易（reversal_of 指向原交易），原交易状态置为 reversed。
        原交易分录保留不删改，两笔交易状态可追溯。
        幂等：若 reversal_key 已存在，返回先前的冲正结果；若原交易已冲正，返回既有冲正交易。
        """
        if not original_key or not original_key.strip():
            raise ValueError("原交易幂等标识不能为空")
        if not reversal_key or not reversal_key.strip():
            raise ValueError("冲正交易幂等标识不能为空")
        orig_key = original_key.strip()
        rev_key = reversal_key.strip()

        self._begin()
        try:
            # 幂等：冲正交易已存在则直接返回
            existing_reversal = self.transactions.get_by_key(rev_key)
            if existing_reversal is not None:
                self._commit()
                return existing_reversal

            original = self.transactions.get_by_key(orig_key)
            if original is None:
                raise TransactionNotFoundError(f"原交易不存在: {orig_key!r}")

            if original.status == TransactionStatus.REVERSED:
                # 已冲正：返回既有冲正交易，保证幂等
                if original.reversed_by is not None:
                    existing = self.transactions.get_by_id(original.reversed_by)
                    if existing is not None:
                        self._commit()
                        return existing
                raise AlreadyReversedError(f"交易已冲正且未找到冲正记录: {orig_key!r}")

            # 构造相反分录
            reversal_entries: list[tuple[int, Direction, int]] = []
            for entry in original.entries:
                opposite = Direction.CREDIT if entry.direction == Direction.DEBIT else Direction.DEBIT
                reversal_entries.append((entry.account_id, opposite, entry.amount))

            # 校验冲正后余额方向（冲正不得导致反向透支）
            accounts: dict[int, Account] = {}
            for account_id, _, _ in reversal_entries:
                if account_id not in accounts:
                    account = self.accounts.get_by_id(account_id)
                    if account is None:
                        raise AccountNotFoundError(f"账户不存在: id={account_id}")
                    accounts[account_id] = account
            self._check_balance_direction(accounts, reversal_entries)

            # 写入冲正交易与分录
            reversal = self.transactions.create(
                idempotency_key=rev_key,
                description=description or f"冲正 {orig_key}",
                status=TransactionStatus.POSTED,
                reversal_of=original.id,
                created_at=_now(),
            )
            for account_id, direction, amount in reversal_entries:
                self.entries.create(
                    transaction_id=reversal.id,
                    account_id=account_id,
                    direction=direction,
                    amount=amount,
                )
            # 标记原交易已冲正
            self.transactions.mark_reversed(original.id, reversal.id)
            result = self.transactions.get_by_id(reversal.id)
            self._commit()
            return result
        except Exception:
            self._rollback()
            raise

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def get_balance(self, account_id: int) -> Balance:
        """查询账户当前余额（由全部分录聚合得到，已提交与已冲正分录均参与计算）。"""
        account = self.accounts.get_by_id(account_id)
        if account is None:
            raise AccountNotFoundError(f"账户不存在: id={account_id}")
        return Balance(account_id=account_id, balance=self.entries.balance(account_id))

    def get_balance_by_code(self, code: str) -> Balance:
        account = self.get_account_by_code(code)
        return self.get_balance(account.id)

    def list_transactions(
        self,
        *,
        account_id: Optional[int] = None,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        status: Optional[TransactionStatus] = None,
    ) -> list[Transaction]:
        """查询交易列表，可按账户、时间范围、状态过滤。"""
        return self.transactions.list(
            account_id=account_id, start_time=start_time, end_time=end_time, status=status
        )

    def get_transaction(self, idempotency_key: str) -> Transaction:
        tx = self.transactions.get_by_key(idempotency_key.strip())
        if tx is None:
            raise TransactionNotFoundError(f"交易不存在: {idempotency_key!r}")
        return tx

    def list_flow(
        self,
        *,
        account_id: int,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
    ) -> list[dict]:
        """查询账户流水（分录级），每条记录区分所属交易状态（posted/reversed）。"""
        account = self.accounts.get_by_id(account_id)
        if account is None:
            raise AccountNotFoundError(f"账户不存在: id={account_id}")
        rows = self.entries.list_by_account(account_id, start_time=start_time, end_time=end_time)
        result = []
        for entry, tx in rows:
            result.append(
                {
                    "entry_id": entry.id,
                    "transaction_id": tx.id,
                    "idempotency_key": tx.idempotency_key,
                    "account_id": entry.account_id,
                    "direction": entry.direction.value,
                    "amount": entry.amount,
                    "transaction_status": tx.status.value,
                    "reversal_of": tx.reversal_of,
                    "reversed_by": tx.reversed_by,
                    "created_at": tx.created_at,
                }
            )
        return result

    # ------------------------------------------------------------------
    # 重启恢复
    # ------------------------------------------------------------------
    def recover(self) -> dict:
        """重启后恢复与一致性校验。

        校验内容：
        - 每笔交易借贷平衡；
        - 分录无孤儿（均归属存在的交易与账户）；
        - 冲正链接一致（reversed_by 与 reversal_of 互相指向，状态为 reversed）。

        由于分录追加写入且所有写操作在单事务内提交，正常情况下恢复不做任何修改；
        若发现不一致则抛出异常，避免带着损坏状态对外服务。
        """
        report = {"transactions": 0, "entries": 0, "issues": []}

        tx_rows = self.conn.execute("SELECT id FROM transactions").fetchall()
        report["transactions"] = len(tx_rows)
        for r in tx_rows:
            tx = self.transactions.get_by_id(r["id"])
            debit = sum(e.amount for e in tx.entries if e.direction == Direction.DEBIT)
            credit = sum(e.amount for e in tx.entries if e.direction == Direction.CREDIT)
            if debit != credit:
                report["issues"].append(f"交易 {tx.id} 借贷不平: 借 {debit} 贷 {credit}")
            if tx.status == TransactionStatus.REVERSED:
                if tx.reversed_by is None:
                    report["issues"].append(f"已冲正交易 {tx.id} 缺少 reversed_by")
                else:
                    rev = self.transactions.get_by_id(tx.reversed_by)
                    if rev is None or rev.reversal_of != tx.id:
                        report["issues"].append(f"交易 {tx.id} 的冲正链接不一致")

        entry_rows = self.conn.execute("SELECT COUNT(*) AS c FROM entries").fetchone()
        report["entries"] = entry_rows["c"]

        orphan = self.conn.execute(
            "SELECT e.id FROM entries e LEFT JOIN transactions t ON t.id = e.transaction_id "
            "WHERE t.id IS NULL"
        ).fetchall()
        if orphan:
            report["issues"].append(f"发现 {len(orphan)} 条孤儿分录")

        if report["issues"]:
            raise RuntimeError("账本一致性校验失败: " + "; ".join(report["issues"]))
        return report
