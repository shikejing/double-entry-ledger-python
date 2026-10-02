"""SQLite 持久层：连接管理、Schema 初始化与重启恢复。

设计要点：
- 分录采用追加式日志（append-only journal），是账务数据的唯一事实来源；
- 余额由分录实时聚合得到，避免余额表与分录不一致；
- 所有写操作在单个 SQLite 事务内完成，天然满足原子性与隔离性；
- 事务采用手动控制（isolation_level=None），写事务以 BEGIN IMMEDIATE 立即获取写锁，
  配合 busy_timeout 实现并发写串行化，避免丢失更新。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    subject_type TEXT NOT NULL,
    normal_direction TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    CHECK (subject_type IN ('asset','liability','equity','revenue','expense')),
    CHECK (normal_direction IN ('debit','credit')),
    CHECK (status IN ('active','closed'))
);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT NOT NULL UNIQUE,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'posted',
    reversal_of INTEGER REFERENCES transactions(id),
    reversed_by INTEGER REFERENCES transactions(id),
    created_at TEXT NOT NULL,
    CHECK (status IN ('posted','reversed'))
);

CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id INTEGER NOT NULL REFERENCES transactions(id),
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    direction TEXT NOT NULL,
    amount INTEGER NOT NULL,
    CHECK (direction IN ('debit','credit')),
    CHECK (amount > 0)
);

CREATE INDEX IF NOT EXISTS idx_entries_account ON entries(account_id);
CREATE INDEX IF NOT EXISTS idx_entries_transaction ON entries(transaction_id);
CREATE INDEX IF NOT EXISTS idx_transactions_created ON transactions(created_at);
"""


def connect(db_path: Optional[str | Path] = None) -> sqlite3.Connection:
    """打开数据库连接并初始化 Schema。

    db_path 为 None 时使用内存数据库（每次连接独立，主要用于测试）。
    事务隔离采用手动模式，由 service 层显式 BEGIN IMMEDIATE / COMMIT / ROLLBACK。
    """
    if db_path is None:
        conn = sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False)
    else:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(path), isolation_level=None, check_same_thread=False, timeout=30.0
        )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    conn.executescript(SCHEMA)
    return conn
