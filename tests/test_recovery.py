"""记账失败与进程重启后的恢复测试。

"重启"用关闭服务/连接后重新打开同一个数据库文件来模拟；
"崩溃"用在事务提交前强行 rollback / 直接丢弃连接来模拟。
"""

from decimal import Decimal
from pathlib import Path

import pytest
import sqlite3

from ledger.errors import UnbalancedEntryError
from ledger.models import AccountType, Direction, TransactionStatus
from ledger.repository import LedgerRepository
from ledger.service import LedgerService

from tests.conftest import CASH, INCOME, make_entries


def _seed(svc: LedgerService) -> None:
    svc.create_account(CASH, "现金", AccountType.ASSET)
    svc.create_account(INCOME, "收入", AccountType.INCOME)


def test_state_persists_across_restart(service_factory, db_path: Path):
    svc = service_factory()
    _seed(svc)
    svc.post_transaction(
        "s1", make_entries([(CASH, Direction.DEBIT, "100"), (INCOME, Direction.CREDIT, "100")])
    )
    svc.close()

    # 模拟进程重启：重新打开同一文件
    svc2 = service_factory()
    assert svc2.get_balance(CASH).value == Decimal("100.00")
    txn = svc2.get_transaction("s1")
    assert txn.status is TransactionStatus.POSTED
    assert len(svc2.get_entries("s1")) == 2
    svc2.close()


def test_failed_transaction_rolls_back_across_restart(service_factory, db_path: Path):
    svc = service_factory()
    _seed(svc)
    with pytest.raises(UnbalancedEntryError):
        svc.post_transaction(
            "bad",
            make_entries([(CASH, Direction.DEBIT, "10"), (INCOME, Direction.CREDIT, "9")]),
        )
    svc.close()

    svc2 = service_factory()
    # 无悬空业务头/分录
    assert svc2.query_entries(include_reversed=True) == []
    assert svc2.get_balance(CASH).value == 0
    # 失败的 business_id 未被占用，可正常使用
    svc2.post_transaction(
        "bad", make_entries([(CASH, Direction.DEBIT, "9"), (INCOME, Direction.CREDIT, "9")])
    )
    assert svc2.get_balance(CASH).value == Decimal("9.00")
    svc2.close()


def test_reversal_state_persists_across_restart(service_factory, db_path: Path):
    svc = service_factory()
    _seed(svc)
    svc.post_transaction(
        "s1", make_entries([(CASH, Direction.DEBIT, "100"), (INCOME, Direction.CREDIT, "100")])
    )
    svc.reverse_transaction("s1", "r1")
    svc.close()

    svc2 = service_factory()
    assert svc2.get_transaction("s1").status is TransactionStatus.REVERSED
    assert svc2.get_transaction("r1").reversal_of_id == svc2.get_transaction("s1").id
    assert svc2.get_balance(CASH).value == 0
    # 重启后冲正约束依然生效
    from ledger.errors import AlreadyReversedError
    with pytest.raises(AlreadyReversedError):
        svc2.reverse_transaction("s1", "r2")
    svc2.close()


def test_crash_mid_transaction_leaves_no_dangling_rows(db_path: Path):
    """直接用底层连接模拟"写了业务头、尚未写分录就崩溃"：
    进程死亡 = 未 COMMIT 的事务在下次打开时由 SQLite 自动回滚。"""
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.execute("PRAGMA journal_mode = WAL")
    ts = "2026-01-01T00:00:00"
    conn.executescript(
        f"""
        CREATE TABLE accounts (
            code TEXT PRIMARY KEY, name TEXT NOT NULL,
            account_type TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, business_id TEXT NOT NULL UNIQUE,
            description TEXT NOT NULL, status TEXT NOT NULL,
            reversal_of_id INTEGER, created_at TEXT NOT NULL);
        CREATE TABLE entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT, transaction_id INTEGER NOT NULL,
            sequence INTEGER NOT NULL, account_code TEXT NOT NULL,
            direction TEXT NOT NULL, amount TEXT NOT NULL,
            status TEXT NOT NULL, created_at TEXT NOT NULL);
        INSERT INTO accounts VALUES ('1001','现金','asset','{ts}');
        INSERT INTO accounts VALUES ('4001','收入','income','{ts}');
        """
    )
    # 开启事务写了一半，未提交直接"杀死进程"（丢弃连接）
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO transactions(business_id, description, status, reversal_of_id, created_at)"
        f" VALUES ('ghost','','posted',NULL,'{ts}')"
    )
    conn.close()  # 进程退出，未提交事务随连接死亡

    # 重新打开：幽灵业务必须已被回滚，business_id 可重新使用
    svc = LedgerService(LedgerRepository(str(db_path)))
    assert svc.repository.get_transaction_by_business_id("ghost") is None
    svc.post_transaction(
        "ghost",
        make_entries([(CASH, Direction.DEBIT, "7"), (INCOME, Direction.CREDIT, "7")]),
    )
    assert svc.get_balance(CASH).value == Decimal("7.00")
    assert len(svc.repository.list_entries_by_transaction(
        svc.get_transaction("ghost").id
    )) == 2
    svc.close()


def test_idempotency_survives_restart(service_factory, db_path: Path):
    svc = service_factory()
    _seed(svc)
    svc.post_transaction(
        "s1", make_entries([(CASH, Direction.DEBIT, "100"), (INCOME, Direction.CREDIT, "100")])
    )
    svc.close()

    svc2 = service_factory()
    hit = svc2.post_transaction(
        "s1", make_entries([(CASH, Direction.DEBIT, "100"), (INCOME, Direction.CREDIT, "100")])
    )
    assert hit.idempotent_hit is True
    assert svc2.get_balance(CASH).value == Decimal("100.00")  # 没有重复入账
    svc2.close()


def test_wal_files_recover_consistency(db_path: Path):
    """即使 WAL 中含已提交未落盘数据，重开后也必须完整可见。"""
    repo = LedgerRepository(str(db_path))
    repo.connect()
    svc = LedgerService(repo)
    _seed(svc)
    # 连续多笔提交，强制 WAL 中有内容
    for i in range(10):
        svc.post_transaction(
            f"s{i}",
            make_entries([(CASH, Direction.DEBIT, "1"), (INCOME, Direction.CREDIT, "1")]),
        )
    # 不做 checkpoint，直接关闭
    svc.close()

    assert db_path.exists()
    svc2 = LedgerService(LedgerRepository(str(db_path)))
    assert svc2.get_balance(CASH).value == Decimal("10.00")
    assert len(svc2.query_entries()) == 20
    svc2.close()
