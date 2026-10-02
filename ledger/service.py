"""账本领域服务：科目校验、原子记账、幂等提交、冲正与查询。

一致性约定
----------
* ``post_transaction`` 在单个 IMMEDIATE 事务内完成：
  业务标识查重 -> 业务/科目校验 -> 写业务头 -> 写全部分录 -> 提交。
* 幂等：同 ``business_id`` 重复提交直接返回先前结果（``idempotent_hit=True``），
  绝不重复入账；若同标识但分录内容不同则按 :class:`DuplicateTransactionError` 拒绝，
  防止"同号不同单"的误用。
* 冲正：生成一笔方向完全相反、金额逐分录像配的冲正业务，原业务与冲正业务在
  有效余额中成对抵消；原始记录只变更状态，金额与方向永不被修改或删除。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Callable

from ledger.errors import (
    AccountNotFoundError,
    AlreadyReversedError,
    DuplicateAccountError,
    DuplicateTransactionError,
    EmptyTransactionError,
    InvalidEntryError,
    ReverseOriginalError,
    ReversalMismatchError,
    TransactionNotFoundError,
    UnbalancedEntryError,
)
from ledger.models import (
    Account,
    AccountType,
    Balance,
    Direction,
    Entry,
    EntryInput,
    PostResult,
    Transaction,
    TransactionStatus,
)
from ledger.repository import LedgerRepository

_ZERO = Decimal("0")


def _normalize_amount(amount: Decimal) -> Decimal:
    """金额规范化：必须为有限正数，去除多余前导符号（-0 之类），保留标量比较。"""
    if not isinstance(amount, Decimal):
        raise InvalidEntryError(f"金额必须是十进制数值，收到 {type(amount).__name__}")
    if not amount.is_finite():
        raise InvalidEntryError("金额必须为有限数，不允许 NaN/Infinity")
    if amount <= 0:
        raise InvalidEntryError(f"分录金额必须为正数，收到 {amount}")
    return amount.quantize(Decimal("0.01"))


class LedgerService:
    def __init__(
        self,
        repository: LedgerRepository,
        clock: Callable[[], datetime] | None = None,
    ):
        self.repository = repository
        self.repository.connect()
        self._clock = clock or datetime.now

    def close(self) -> None:
        self.repository.close()

    def __enter__(self) -> "LedgerService":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # ---- 科目 ----
    def create_account(self, code: str, name: str, account_type: AccountType) -> Account:
        if not isinstance(code, str) or not code.strip():
            raise InvalidEntryError("科目代码不能为空")
        if not isinstance(name, str) or not name.strip():
            raise InvalidEntryError("科目名称不能为空")
        if not isinstance(account_type, AccountType):
            raise InvalidEntryError(
                f"非法科目类型 {account_type!r}，"
                f"可选：{', '.join(t.value for t in AccountType)}"
            )
        with self.repository.transaction():
            if self.repository.get_account(code) is not None:
                raise DuplicateAccountError(f"科目代码 {code!r} 已存在")
            return self.repository.insert_account(
                code.strip(), name.strip(), account_type, self._clock()
            )

    def get_account(self, code: str) -> Account:
        account = self.repository.get_account(code)
        if account is None:
            raise AccountNotFoundError(f"科目 {code!r} 不存在")
        return account

    def list_accounts(self) -> list[Account]:
        return self.repository.list_accounts()

    # ---- 校验 ----
    def _validate_entries(self, entries: list[EntryInput]) -> list[tuple[str, Direction, Decimal]]:
        if not entries:
            raise EmptyTransactionError("一笔业务至少需要一条分录")
        normalized: list[tuple[str, Direction, Decimal]] = []
        for index, entry in enumerate(entries):
            where = f"第 {index + 1} 条分录"
            if not isinstance(entry, EntryInput):
                raise InvalidEntryError(f"{where}：分录格式非法")
            if not isinstance(entry.account_code, str) or not entry.account_code.strip():
                raise InvalidEntryError(f"{where}：科目代码不能为空")
            if not isinstance(entry.direction, Direction):
                raise InvalidEntryError(
                    f"{where}：借贷方向非法 {entry.direction!r}，"
                    f"必须为 {Direction.DEBIT.value!r} 或 {Direction.CREDIT.value!r}"
                )
            amount = _normalize_amount(entry.amount)
            # 科目必须存在；科目类型随科目记录而固定，方向/金额校验以同一科目数据为准
            account = self.repository.get_account(entry.account_code.strip())
            if account is None:
                raise AccountNotFoundError(
                    f"{where}引用的科目 {entry.account_code!r} 不存在，请先创建科目"
                )
            normalized.append((account.code, entry.direction, amount))

        debit_sum = sum(
            (a for _, d, a in normalized if d is Direction.DEBIT), start=_ZERO
        )
        credit_sum = sum(
            (a for _, d, a in normalized if d is Direction.CREDIT), start=_ZERO
        )
        if debit_sum == 0 or credit_sum == 0:
            raise UnbalancedEntryError(
                f"每笔业务必须同时包含借方与贷方分录（借方合计 {debit_sum}，"
                f"贷方合计 {credit_sum}）"
            )
        if debit_sum != credit_sum:
            raise UnbalancedEntryError(
                f"借贷不平衡：借方合计 {debit_sum} != 贷方合计 {credit_sum}"
            )
        return normalized

    # ---- 记账 ----
    def post_transaction(
        self,
        business_id: str,
        entries: list[EntryInput],
        description: str = "",
    ) -> PostResult:
        if not isinstance(business_id, str) or not business_id.strip():
            raise InvalidEntryError("业务标识 business_id 不能为空")
        business_id = business_id.strip()

        # 先做无锁快路径幂等判断；事务内还会再判一次以覆盖并发竞态。
        existing = self.repository.get_transaction_by_business_id(business_id)
        if existing is not None:
            return self._idempotent_result(existing, entries)

        normalized = self._validate_entries(entries)
        now = self._clock()
        with self.repository.transaction():
            existing = self.repository.get_transaction_by_business_id(business_id)
            if existing is not None:
                # 并发下另一事务已用相同 business_id 提交：返回先前结果，绝不重复入账
                saved_entries = self.repository.list_entries_by_transaction(existing.id)
                return PostResult(existing, tuple(saved_entries), idempotent_hit=True)
            txn_id = self.repository.insert_transaction(
                business_id, description, TransactionStatus.POSTED, now, None
            )
            saved = self.repository.insert_entries(txn_id, normalized, now)
            txn = self.repository.get_transaction_by_id(txn_id)
            assert txn is not None
            return PostResult(txn, tuple(saved), idempotent_hit=False)

    def _idempotent_result(
        self, existing: Transaction, requested_entries: list[EntryInput]
    ) -> PostResult:
        saved_entries = self.repository.list_entries_by_transaction(existing.id)
        # 内容指纹比对：同 business_id 却携带不同分录是明确的调用错误。
        requested = [
            (
                e.account_code.strip(),
                e.direction,
                _normalize_amount(e.amount),
            )
            for e in requested_entries
        ]
        saved_key = [(e.account_code, e.direction, e.amount) for e in saved_entries]
        if requested != saved_key:
            raise DuplicateTransactionError(existing.business_id, existing.business_id)
        return PostResult(existing, tuple(saved_entries), idempotent_hit=True)

    # ---- 冲正 ----
    def reverse_transaction(
        self,
        original_business_id: str,
        reversal_business_id: str,
        description: str = "",
    ) -> PostResult:
        if not isinstance(reversal_business_id, str) or not reversal_business_id.strip():
            raise InvalidEntryError("冲正业务标识 reversal_business_id 不能为空")
        reversal_business_id = reversal_business_id.strip()

        original = self.repository.get_transaction_by_business_id(original_business_id)
        if original is None:
            raise TransactionNotFoundError(
                f"被冲正业务 {original_business_id!r} 不存在"
            )
        if original.reversal_of_id is not None:
            raise ReverseOriginalError("冲正业务本身不能再被冲正，请冲正其原始业务")
        if original.status is TransactionStatus.REVERSED:
            raise AlreadyReversedError(
                f"业务 {original_business_id!r} 已被冲正，不能重复冲正"
            )
        if (
            self.repository.get_transaction_by_business_id(reversal_business_id)
            is not None
        ):
            prior = self.repository.get_transaction_by_business_id(reversal_business_id)
            assert prior is not None
            raise DuplicateTransactionError(reversal_business_id, prior.business_id)

        original_entries = self.repository.list_entries_by_transaction(original.id)
        # 冲正分录 = 原始分录逐条反向，金额必须完全一致，保证逐科目抵消。
        reversal_input = [
            EntryInput(
                account_code=e.account_code,
                direction=(
                    Direction.CREDIT if e.direction is Direction.DEBIT else Direction.DEBIT
                ),
                amount=e.amount,
            )
            for e in original_entries
        ]
        normalized = self._validate_entries(reversal_input)
        now = self._clock()
        with self.repository.transaction():
            # 事务内复查，消除并发重复冲正/并发占号的竞态。
            original_tx = self.repository.get_transaction_by_id(original.id)
            assert original_tx is not None
            if original_tx.status is TransactionStatus.REVERSED:
                raise AlreadyReversedError(
                    f"业务 {original_business_id!r} 已被冲正，不能重复冲正"
                )
            prior = self.repository.get_transaction_by_business_id(reversal_business_id)
            if prior is not None:
                raise DuplicateTransactionError(reversal_business_id, prior.business_id)

            reversal_id = self.repository.insert_transaction(
                reversal_business_id,
                description or f"冲正业务 {original_business_id}",
                TransactionStatus.POSTED,
                now,
                reversal_of_id=original.id,
            )
            saved_reversal = self.repository.insert_entries(reversal_id, normalized, now)
            # 原业务仅做状态迁移，金额/方向/时间等原始字段一律保留。
            self.repository.mark_reversed(original.id)
            reversal_txn = self.repository.get_transaction_by_id(reversal_id)
            assert reversal_txn is not None
            return PostResult(reversal_txn, tuple(saved_reversal), idempotent_hit=False)

    # ---- 查询 ----
    def get_transaction(self, business_id: str) -> Transaction:
        txn = self.repository.get_transaction_by_business_id(business_id)
        if txn is None:
            raise TransactionNotFoundError(f"业务 {business_id!r} 不存在")
        return txn

    def get_entries(self, business_id: str) -> list[Entry]:
        txn = self.get_transaction(business_id)
        return self.repository.list_entries_by_transaction(txn.id)

    def query_entries(
        self,
        account_code: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        include_reversed: bool = False,
    ) -> list[Entry]:
        # 科目存在性校验与流水读取在同一 MVCC 快照内，返回结果对应单一已提交状态。
        with self.repository.snapshot():
            if account_code is not None:
                self.get_account(account_code)  # 不存在则显式报错
            return self.repository.query_entries(
                account_code=account_code,
                start=start,
                end=end,
                include_reversed=include_reversed,
            )

    def get_balance(
        self, account_code: str, include_reversed: bool = False
    ) -> Balance:
        with self.repository.snapshot():
            self.get_account(account_code)
            balance = self.repository.get_balance(
                account_code, include_reversed=include_reversed
            )
            assert balance is not None
            return balance

    def get_account_statement(
        self,
        account_code: str,
        start: datetime | None = None,
        end: datetime | None = None,
        include_reversed: bool = False,
    ) -> tuple[Balance, list[Entry]]:
        """在同一个 MVCC 快照内返回科目余额与其明细流水。

        保证：返回的余额恒等于同一份返回流水中借方合计减贷方合计，
        二者必然对应同一个已提交的完整账目状态。
        """
        with self.repository.snapshot():
            self.get_account(account_code)
            entries = self.repository.query_entries(
                account_code=account_code,
                start=start,
                end=end,
                include_reversed=include_reversed,
            )
            # 余额必须覆盖整个科目（不带时间过滤），因此独立聚合，
            # 但与流水处在同一快照，彼此一致。
            balance = self.repository.get_balance(
                account_code, include_reversed=include_reversed
            )
            assert balance is not None
            return balance, entries
