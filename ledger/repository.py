"""账本持久化层：基于单文件 SQLite（WAL + 显式 IMMEDIATE 事务）。

设计要点
--------
* 金额：``Decimal`` 规范化后以 TEXT 存储，读回时还原为 ``Decimal``，杜绝浮点误差。
* 时间：naive UTC ``datetime`` 以 ISO8601 TEXT 存储，字典序即时间序。
* 并发：WAL 允许读写并发；所有写事务以 ``BEGIN IMMEDIATE`` 起始，立刻获取
  RESERVED 写锁，配合 ``busy_timeout`` 让并发写者排队，SQLite 保证写事务
  全局串行化，因此不会出现交叉写入。
* 原子性：业务头与全部分录在同一事务内写入，要么全部可见，要么全部不可见；
  崩溃后由 SQLite WAL 回滚日志自动恢复，不存在悬空分录。
* 余额：不维护余额计数器，而是对已提交分录实时 SUM（事件溯源），因此不存在
  "读改写"式的丢失更新；任何一次读取都对应某个已完整提交的账目状态。
* 冲正可见性：原业务 status=reversed；冲正业务 status=posted 且
  reversal_of_id 指向原业务。"有效集" = status=posted 且非冲正业务，
  冲正双方成对排除（净影响为 0）；include_reversed=True 时可查见全部记录。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal

from ledger.errors import DuplicateAccountError
from ledger.models import (
    Account,
    AccountType,
    Balance,
    Direction,
    Entry,
    NORMAL_BALANCE_SIDE,
    Transaction,
    TransactionStatus,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    code         TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    account_type TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transactions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id    TEXT NOT NULL UNIQUE,
    description    TEXT NOT NULL,
    status         TEXT NOT NULL,
    reversal_of_id INTEGER REFERENCES transactions(id),
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entries (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id INTEGER NOT NULL REFERENCES transactions(id),
    sequence       INTEGER NOT NULL,
    account_code   TEXT NOT NULL REFERENCES accounts(code),
    direction      TEXT NOT NULL,
    amount         TEXT NOT NULL,
    status         TEXT NOT NULL,
    created_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_entries_account   ON entries(account_code);
CREATE INDEX IF NOT EXISTS idx_entries_txn       ON entries(transaction_id);
CREATE INDEX IF NOT EXISTS idx_txn_created       ON transactions(created_at);
CREATE INDEX IF NOT EXISTS idx_entries_created   ON entries(created_at);
"""


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


class LedgerRepository:
    """薄封装单条 SQLite 连接；线程安全由 SQLite 写锁串行化保证。"""

    def __init__(self, db_path: str = ":memory:", busy_timeout_ms: int = 5000):
        self.db_path = db_path
        self.busy_timeout_ms = busy_timeout_ms
        self._conn: sqlite3.Connection | None = None

    # ---- 连接与事务 ----
    def connect(self) -> None:
        if self._conn is not None:
            return
        # check_same_thread=False：允许多线程共享同一连接；
        # 写事务由 BEGIN IMMEDIATE + busy_timeout 串行化。
        conn = sqlite3.connect(
            self.db_path,
            timeout=self.busy_timeout_ms / 1000.0,
            isolation_level=None,  # autocommit，事务边界完全显式管理
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {int(self.busy_timeout_ms)}")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(_SCHEMA)
        self._conn = conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self.connect()
        assert self._conn is not None
        return self._conn

    def begin_immediate(self) -> None:
        self.conn.execute("BEGIN IMMEDIATE")

    def commit(self) -> None:
        self.conn.execute("COMMIT")

    def rollback(self) -> None:
        self.conn.execute("ROLLBACK")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """写事务上下文：正常结束提交，异常自动回滚。"""
        self.begin_immediate()
        try:
            yield self.conn
        except Exception:
            try:
                self.rollback()
            except sqlite3.Error:
                pass
            raise
        else:
            self.commit()

    @contextmanager
    def snapshot(self) -> Iterator[sqlite3.Connection]:
        """只读快照事务：上下文中的所有 SELECT 共享同一个 MVCC 快照，
        保证一次查询内读到的多个数据项对应同一个已提交状态。

        可重入：若调用方已持有事务，则沿用其事务边界，不另开快照。
        """
        outer = self.conn.in_transaction
        if not outer:
            self.conn.execute("BEGIN")
        try:
            yield self.conn
        finally:
            if not outer:
                try:
                    self.conn.execute("COMMIT")
                except sqlite3.Error:
                    try:
                        self.conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass

    # ---- 科目 ----
    def insert_account(
        self, code: str, name: str, account_type: AccountType, now: datetime
    ) -> Account:
        try:
            self.conn.execute(
                "INSERT INTO accounts(code, name, account_type, created_at) VALUES (?,?,?,?)",
                (code, name, account_type.value, now.isoformat()),
            )
        except sqlite3.IntegrityError as exc:
            raise DuplicateAccountError(f"科目代码 {code!r} 已存在") from exc
        account = self.get_account(code)
        assert account is not None
        return account

    def _row_to_account(self, row: sqlite3.Row) -> Account:
        return Account(
            code=row["code"],
            name=row["name"],
            account_type=AccountType(row["account_type"]),
            created_at=_dt(row["created_at"]),
        )

    def get_account(self, code: str) -> Account | None:
        row = self.conn.execute(
            "SELECT code, name, account_type, created_at FROM accounts WHERE code = ?",
            (code,),
        ).fetchone()
        return self._row_to_account(row) if row is not None else None

    def list_accounts(self) -> list[Account]:
        rows = self.conn.execute(
            "SELECT code, name, account_type, created_at FROM accounts ORDER BY code"
        ).fetchall()
        return [self._row_to_account(row) for row in rows]

    # ---- 业务 ----
    def insert_transaction(
        self,
        business_id: str,
        description: str,
        status: TransactionStatus,
        now: datetime,
        reversal_of_id: int | None,
    ) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO transactions(business_id, description, status, reversal_of_id, created_at)
            VALUES (?,?,?,?,?)
            """,
            (business_id, description, status.value, reversal_of_id, now.isoformat()),
        )
        return int(cur.lastrowid)

    def _row_to_transaction(self, row: sqlite3.Row) -> Transaction:
        return Transaction(
            id=row["id"],
            business_id=row["business_id"],
            description=row["description"],
            status=TransactionStatus(row["status"]),
            created_at=_dt(row["created_at"]),
            reversal_of_id=row["reversal_of_id"],
        )

    def get_transaction_by_business_id(self, business_id: str) -> Transaction | None:
        row = self.conn.execute(
            "SELECT * FROM transactions WHERE business_id = ?", (business_id,)
        ).fetchone()
        return self._row_to_transaction(row) if row is not None else None

    def get_transaction_by_id(self, transaction_id: int) -> Transaction | None:
        row = self.conn.execute(
            "SELECT * FROM transactions WHERE id = ?", (transaction_id,)
        ).fetchone()
        return self._row_to_transaction(row) if row is not None else None

    def mark_reversed(self, transaction_id: int) -> None:
        """把原业务及其分录整体置为 reversed（仅状态列变化，金额/方向等原样保留）。"""
        self.conn.execute(
            "UPDATE transactions SET status = ? WHERE id = ?",
            (TransactionStatus.REVERSED.value, transaction_id),
        )
        self.conn.execute(
            "UPDATE entries SET status = ? WHERE transaction_id = ?",
            (TransactionStatus.REVERSED.value, transaction_id),
        )

    # ---- 分录 ----
    def insert_entries(
        self,
        transaction_id: int,
        entries: list[tuple[str, Direction, Decimal]],
        now: datetime,
    ) -> list[Entry]:
        # 单条多行 INSERT 与业务头在同一事务内提交，杜绝半成品。
        self.conn.executemany(
            """
            INSERT INTO entries(transaction_id, sequence, account_code, direction,
                                amount, status, created_at)
            VALUES (?,?,?,?,?,?,?)
            """,
            [
                (
                    transaction_id,
                    seq,
                    account_code,
                    direction.value,
                    str(amount),
                    TransactionStatus.POSTED.value,
                    now.isoformat(),
                )
                for seq, (account_code, direction, amount) in enumerate(entries)
            ],
        )
        return self.list_entries_by_transaction(transaction_id)

    def list_entries_by_transaction(self, transaction_id: int) -> list[Entry]:
        rows = self.conn.execute(
            """
            SELECT e.id, e.transaction_id, e.sequence, e.account_code, e.direction,
                   e.amount, t.status AS status
            FROM entries e JOIN transactions t ON t.id = e.transaction_id
            WHERE e.transaction_id = ?
            ORDER BY e.sequence, e.id
            """,
            (transaction_id,),
        ).fetchall()
        return [self._row_to_entry(row) for row in rows]

    def _row_to_entry(self, row: sqlite3.Row) -> Entry:
        return Entry(
            id=row["id"],
            transaction_id=row["transaction_id"],
            account_code=row["account_code"],
            direction=Direction(row["direction"]),
            amount=Decimal(row["amount"]),
            sequence=row["sequence"],
            status=TransactionStatus(row["status"]),
        )

    def query_entries(
        self,
        account_code: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        include_reversed: bool = False,
    ) -> list[Entry]:
        sql = [
            "SELECT e.id, e.transaction_id, e.sequence, e.account_code, e.direction,",
            "       e.amount, t.status AS status",
            "FROM entries e JOIN transactions t ON t.id = e.transaction_id",
        ]
        where: list[str] = []
        params: list[object] = []
        if not include_reversed:
            # 有效集：已提交且不是冲正业务（被冲正的原业务与其冲正业务成对排除）。
            where.append("t.status = 'posted' AND t.reversal_of_id IS NULL")
        if account_code is not None:
            where.append("e.account_code = ?")
            params.append(account_code)
        if start is not None:
            where.append("t.created_at >= ?")
            params.append(start.isoformat())
        if end is not None:
            where.append("t.created_at <= ?")
            params.append(end.isoformat())
        if where:
            sql.append("WHERE " + " AND ".join(where))
        sql.append("ORDER BY e.id, e.sequence")
        with self.snapshot():
            rows = self.conn.execute("\n".join(sql), params).fetchall()
        return [self._row_to_entry(row) for row in rows]

    # ---- 余额 ----
    def get_balance(
        self, account_code: str, include_reversed: bool = False
    ) -> Balance | None:
        # 科目存在性与发生额聚合在同一快照读取，避免跨提交点的不一致视图。
        with self.snapshot():
            account = self.get_account(account_code)
            if account is None:
                return None
            sql = [
                "SELECT e.direction AS direction, SUM(e.amount) AS total",
                "FROM entries e JOIN transactions t ON t.id = e.transaction_id",
                "WHERE e.account_code = ?",
            ]
            params: list[object] = [account_code]
            if not include_reversed:
                sql.append("AND t.status = 'posted' AND t.reversal_of_id IS NULL")
            sql.append("GROUP BY e.direction")
            rows = self.conn.execute("\n".join(sql), params).fetchall()
            cents = Decimal("0.01")
            totals = {Direction.DEBIT: Decimal(0), Direction.CREDIT: Decimal(0)}
            for row in rows:
                # SQLite SUM 会对 TEXT 数字做数值聚合，结果可能丢失 "0.00" 标度，
                # 这里统一按最小货币单位（分）规范化。
                totals[Direction(row["direction"])] = Decimal(row["total"]).quantize(cents)
            debit, credit = totals[Direction.DEBIT], totals[Direction.CREDIT]
            debit = debit.quantize(cents)
            credit = credit.quantize(cents)
            net_debit = (debit - credit).quantize(cents)
            if NORMAL_BALANCE_SIDE[account.account_type] is Direction.DEBIT:
                value = net_debit
            else:
                value = -net_debit
            return Balance(
                account_code=account_code,
                debit_total=debit,
                credit_total=credit,
                net_debit=net_debit,
                value=value,
            )
