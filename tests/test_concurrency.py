"""并发提交测试：同一科目的余额不得丢失更新；同标识并发只有一次入账。

使用文件型 SQLite + 多个线程共享同一路径（每个线程独立连接），
以真实模拟多客户端并发，而非内存库的单连接假象。
"""

from __future__ import annotations

import threading
from decimal import Decimal

import pytest

from ledger.errors import AlreadyReversedError
from ledger.models import AccountType, Direction, EntryInput
from ledger.repository import LedgerRepository
from ledger.service import LedgerService

from tests.conftest import CASH, INCOME


def _pay_entry() -> list[EntryInput]:
    return [
        EntryInput(CASH, Direction.DEBIT, Decimal("1.00")),
        EntryInput(INCOME, Direction.CREDIT, Decimal("1.00")),
    ]


def _post_many(db_path: str, business_ids: list[str], errors: list[Exception]) -> None:
    svc = LedgerService(LedgerRepository(db_path))
    try:
        for bid in business_ids:
            try:
                svc.post_transaction(bid, _pay_entry())
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
    finally:
        svc.close()


@pytest.fixture
def concurrent_db(service_factory, db_path) -> LedgerService:
    svc = service_factory()
    svc.create_account(CASH, "现金", AccountType.ASSET)
    svc.create_account(INCOME, "收入", AccountType.INCOME)
    svc.close()
    return str(db_path)


def test_concurrent_distinct_transactions_no_lost_update(concurrent_db):
    n_threads, per_thread = 8, 25
    errors: list[Exception] = []
    threads = []
    for t in range(n_threads):
        ids = [f"biz-{t:02d}-{i:03d}" for i in range(per_thread)]
        th = threading.Thread(target=_post_many, args=(concurrent_db, ids, errors))
        threads.append(th)
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert errors == []
    svc = LedgerService(LedgerRepository(concurrent_db))
    total = n_threads * per_thread
    assert svc.get_balance(CASH).value == Decimal(total)
    assert svc.get_balance(INCOME).value == Decimal(total)
    # 流水完整，每条业务两条分录
    assert len(svc.query_entries()) == total * 2
    svc.close()


def test_concurrent_same_business_id_single_post(concurrent_db):
    n_threads = 16
    hits: list[bool] = []
    lock = threading.Lock()

    def worker() -> None:
        svc = LedgerService(LedgerRepository(concurrent_db))
        try:
            result = svc.post_transaction("same-id", _pay_entry())
            with lock:
                hits.append(result.idempotent_hit)
        finally:
            svc.close()

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert len(hits) == n_threads
    assert hits.count(False) == 1          # 恰好一次真正入账
    assert hits.count(True) == n_threads - 1  # 其余全部命中幂等
    svc = LedgerService(LedgerRepository(concurrent_db))
    assert svc.get_balance(CASH).value == Decimal("1.00")
    svc.close()


def test_concurrent_reverse_only_wins_once(concurrent_db):
    svc = LedgerService(LedgerRepository(concurrent_db))
    svc.post_transaction("orig", _pay_entry())
    svc.close()

    errors: list[Exception] = []
    lock = threading.Lock()

    def worker(idx: int) -> None:
        local = LedgerService(LedgerRepository(concurrent_db))
        try:
            local.reverse_transaction("orig", f"rev-{idx}")
        except AlreadyReversedError as exc:
            with lock:
                errors.append(exc)
        finally:
            local.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    # 恰好一个冲正成功，其余全部观察到"已冲正"
    assert len(errors) == 7
    svc = LedgerService(LedgerRepository(concurrent_db))
    assert svc.get_transaction("orig").status.value == "reversed"
    assert svc.get_balance(CASH).value == 0
    # 冲正成功的那笔业务能在历史中查到
    winners = [i for i in range(8) if svc.repository.get_transaction_by_business_id(f"rev-{i}")]
    assert winners.__len__() == 1
    svc.close()


def test_balance_always_matches_committed_entries(concurrent_db):
    """任意时刻的余额读取都必须等于已提交分录之和（读一致性）。"""
    stop = threading.Event()
    violations: list[str] = []

    def writer(t: int) -> None:
        svc = LedgerService(LedgerRepository(concurrent_db))
        try:
            for i in range(20):
                svc.post_transaction(f"w{t}-{i}", _pay_entry())
        finally:
            svc.close()

    def reader() -> None:
        svc = LedgerService(LedgerRepository(concurrent_db))
        try:
            while not stop.is_set():
                # 同一快照内的余额与流水必须严格自洽
                balance, posted = svc.get_account_statement(account_code=CASH)
                debit_sum = sum(
                    (e.amount for e in posted if e.direction is Direction.DEBIT),
                    start=Decimal("0"),
                )
                if balance.value != debit_sum:
                    violations.append(f"balance={balance.value} sum={debit_sum}")
        finally:
            svc.close()

    readers = [threading.Thread(target=reader) for _ in range(3)]
    for r in readers:
        r.start()
    writers = [threading.Thread(target=writer, args=(t,)) for t in range(4)]
    for w in writers:
        w.start()
    for w in writers:
        w.join()
    stop.set()
    for r in readers:
        r.join()

    assert violations == []
