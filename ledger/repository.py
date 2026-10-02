"""数据访问层：账户、交易、分录的增删改查。

所有方法接收一个已存在的 sqlite3.Connection，事务边界由 service 层控制。
"""

from __future__ import annotations

import sqlite3
from typing import Optional, Sequence

from .models import (
    Account,
    AccountStatus,
    Direction,
    Entry,
    SubjectType,
    Transaction,
    TransactionStatus,
)


def _row_to_account(row: sqlite3.Row) -> Account:
    return Account(
        id=row["id"],
        code=row["code"],
        name=row["name"],
        subject_type=SubjectType(row["subject_type"]),
        normal_direction=Direction(row["normal_direction"]),
        status=AccountStatus(row["status"]),
        created_at=row["created_at"],
    )


def _row_to_entry(row: sqlite3.Row) -> Entry:
    return Entry(
        id=row["id"],
        transaction_id=row["transaction_id"],
        account_id=row["account_id"],
        direction=Direction(row["direction"]),
        amount=row["amount"],
    )


def _row_to_transaction(row: sqlite3.Row, entries: Sequence[Entry] = ()) -> Transaction:
    return Transaction(
        id=row["id"],
        idempotency_key=row["idempotency_key"],
        description=row["description"],
        status=TransactionStatus(row["status"]),
        reversal_of=row["reversal_of"],
        reversed_by=row["reversed_by"],
        created_at=row["created_at"],
        entries=tuple(entries),
    )


class AccountRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def create(
        self,
        *,
        code: str,
        name: str,
        subject_type: SubjectType,
        normal_direction: Direction,
        created_at: str,
    ) -> Account:
        cur = self.conn.execute(
            "INSERT INTO accounts (code, name, subject_type, normal_direction, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (code, name, subject_type.value, normal_direction.value, AccountStatus.ACTIVE.value, created_at),
        )
        row = self.conn.execute("SELECT * FROM accounts WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _row_to_account(row)

    def get_by_id(self, account_id: int) -> Optional[Account]:
        row = self.conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        return _row_to_account(row) if row else None

    def get_by_code(self, code: str) -> Optional[Account]:
        row = self.conn.execute("SELECT * FROM accounts WHERE code = ?", (code,)).fetchone()
        return _row_to_account(row) if row else None

    def list_all(self) -> list[Account]:
        rows = self.conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()
        return [_row_to_account(r) for r in rows]

    def close(self, account_id: int) -> None:
        self.conn.execute("UPDATE accounts SET status = ? WHERE id = ?", (AccountStatus.CLOSED.value, account_id))


class TransactionRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def create(
        self,
        *,
        idempotency_key: str,
        description: Optional[str],
        status: TransactionStatus,
        reversal_of: Optional[int],
        created_at: str,
    ) -> Transaction:
        cur = self.conn.execute(
            "INSERT INTO transactions (idempotency_key, description, status, reversal_of, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (idempotency_key, description, status.value, reversal_of, created_at),
        )
        row = self.conn.execute("SELECT * FROM transactions WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _row_to_transaction(row)

    def get_by_id(self, tx_id: int) -> Optional[Transaction]:
        row = self.conn.execute("SELECT * FROM transactions WHERE id = ?", (tx_id,)).fetchone()
        if not row:
            return None
        entries = EntryRepository(self.conn).list_by_transaction(tx_id)
        return _row_to_transaction(row, entries)

    def get_by_key(self, idempotency_key: str) -> Optional[Transaction]:
        row = self.conn.execute("SELECT * FROM transactions WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
        if not row:
            return None
        entries = EntryRepository(self.conn).list_by_transaction(row["id"])
        return _row_to_transaction(row, entries)

    def mark_reversed(self, tx_id: int, reversed_by: int) -> None:
        self.conn.execute(
            "UPDATE transactions SET status = ?, reversed_by = ? WHERE id = ?",
            (TransactionStatus.REVERSED.value, reversed_by, tx_id),
        )

    def list(
        self,
        *,
        account_id: Optional[int] = None,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        status: Optional[TransactionStatus] = None,
    ) -> list[Transaction]:
        sql = "SELECT DISTINCT t.* FROM transactions t"
        params: list = []
        if account_id is not None:
            sql += " JOIN entries e ON e.transaction_id = t.id WHERE e.account_id = ?"
            params.append(account_id)
        else:
            sql += " WHERE 1=1"
        if start_time is not None:
            sql += " AND t.created_at >= ?"
            params.append(start_time)
        if end_time is not None:
            sql += " AND t.created_at <= ?"
            params.append(end_time)
        if status is not None:
            sql += " AND t.status = ?"
            params.append(status.value)
        sql += " ORDER BY t.id"
        rows = self.conn.execute(sql, params).fetchall()
        repo = EntryRepository(self.conn)
        result = []
        for r in rows:
            result.append(_row_to_transaction(r, repo.list_by_transaction(r["id"])))
        return result


class EntryRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def create(self, *, transaction_id: int, account_id: int, direction: Direction, amount: int) -> Entry:
        cur = self.conn.execute(
            "INSERT INTO entries (transaction_id, account_id, direction, amount) VALUES (?, ?, ?, ?)",
            (transaction_id, account_id, direction.value, amount),
        )
        row = self.conn.execute("SELECT * FROM entries WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _row_to_entry(row)

    def list_by_transaction(self, transaction_id: int) -> list[Entry]:
        rows = self.conn.execute(
            "SELECT * FROM entries WHERE transaction_id = ? ORDER BY id", (transaction_id,)
        ).fetchall()
        return [_row_to_entry(r) for r in rows]

    def list_by_account(
        self,
        account_id: int,
        *,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
    ) -> list[tuple[Entry, Transaction]]:
        sql = (
            "SELECT e.*, t.id AS t_id, t.idempotency_key, t.description, t.status AS t_status, "
            "t.reversal_of, t.reversed_by, t.created_at AS t_created_at "
            "FROM entries e JOIN transactions t ON t.id = e.transaction_id "
            "WHERE e.account_id = ?"
        )
        params: list = [account_id]
        if start_time is not None:
            sql += " AND t.created_at >= ?"
            params.append(start_time)
        if end_time is not None:
            sql += " AND t.created_at <= ?"
            params.append(end_time)
        sql += " ORDER BY t.id, e.id"
        rows = self.conn.execute(sql, params).fetchall()
        out: list[tuple[Entry, Transaction]] = []
        for r in rows:
            entry = _row_to_entry(r)
            tx = Transaction(
                id=r["t_id"],
                idempotency_key=r["idempotency_key"],
                description=r["description"],
                status=TransactionStatus(r["t_status"]),
                reversal_of=r["reversal_of"],
                reversed_by=r["reversed_by"],
                created_at=r["t_created_at"],
            )
            out.append((entry, tx))
        return out

    def balance(self, account_id: int) -> int:
        """计算账户当前余额（分），正数表示与账户正常方向一致。

        余额 = 正常方向合计 - 相反方向合计。
        """
        account = self.conn.execute("SELECT normal_direction FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if not account:
            return 0
        normal = account["normal_direction"]
        row = self.conn.execute(
            "SELECT "
            "COALESCE(SUM(CASE WHEN direction = ? THEN amount ELSE 0 END), 0) AS normal_total, "
            "COALESCE(SUM(CASE WHEN direction = ? THEN amount ELSE 0 END), 0) AS opposite_total "
            "FROM entries WHERE account_id = ?",
            (normal, Direction.DEBIT.value if normal == Direction.CREDIT.value else Direction.CREDIT.value, account_id),
        ).fetchone()
        return row["normal_total"] - row["opposite_total"]
