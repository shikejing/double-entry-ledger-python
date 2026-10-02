"""账本服务自动化测试：幂等、并发、冲正、重启恢复、校验与查询。"""

from __future__ import annotations

import os
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from ledger.db import connect
from ledger.errors import (
    AccountClosedError,
    AccountExistsError,
    AccountNotFoundError,
    BalanceViolationError,
    InsufficientEntriesError,
    InvalidAmountError,
    InvalidDirectionError,
    TransactionNotFoundError,
    UnbalancedTransactionError,
)
from ledger.models import Direction, SubjectType, TransactionStatus
from ledger.service import LedgerService


@pytest.fixture
def service():
    """内存数据库服务（单连接）。"""
    conn = connect(None)
    svc = LedgerService(conn)
    yield svc
    conn.close()


@pytest.fixture
def funded_service(service):
    """已创建三个账户的服务。"""
    service.create_account(code="1001", name="银行存款", subject_type=SubjectType.ASSET)
    service.create_account(code="2001", name="短期借款", subject_type=SubjectType.LIABILITY)
    service.create_account(code="3001", name="实收资本", subject_type=SubjectType.EQUITY)
    return service


def _post(svc, key, debit_account, credit_account, amount, desc=None):
    return svc.post_transaction(
        idempotency_key=key,
        description=desc,
        entries=[
            (debit_account, Direction.DEBIT, amount),
            (credit_account, Direction.CREDIT, amount),
        ],
    )


# ----------------------------------------------------------------------
# 基础记账与余额
# ----------------------------------------------------------------------
def test_post_and_balance(funded_service):
    svc = funded_service
    _post(svc, "tx-001", 1, 2, 10000, "收到借款")
    # 资产类正常方向为借，余额=10000（借方余额）
    assert svc.get_balance(1).balance == 10000
    # 负债类正常方向为贷，余额=10000（贷方余额）
    assert svc.get_balance(2).balance == 10000
    assert svc.get_balance(3).balance == 0


def test_balance_direction_derived_from_subject_type(service):
    service.create_account(code="6001", name="主营业务收入", subject_type=SubjectType.REVENUE)
    service.create_account(code="6002", name="管理费用", subject_type=SubjectType.EXPENSE)
    revenue = service.get_account_by_code("6001")
    expense = service.get_account_by_code("6002")
    assert revenue.normal_direction == Direction.CREDIT
    assert expense.normal_direction == Direction.DEBIT


# ----------------------------------------------------------------------
# 幂等
# ----------------------------------------------------------------------
def test_idempotent_post_returns_same_result(funded_service):
    svc = funded_service
    first = _post(svc, "tx-idem", 1, 2, 10000)
    second = _post(svc, "tx-idem", 1, 2, 10000)
    # 两次返回同一笔交易
    assert second.id == first.id
    assert second.idempotency_key == "tx-idem"
    # 只产生一笔交易，余额只计一次
    assert len(svc.list_transactions()) == 1
    assert svc.get_balance(1).balance == 10000
    assert svc.get_balance(2).balance == 10000


def test_idempotent_post_does_not_duplicate_entries(funded_service):
    svc = funded_service
    _post(svc, "tx-idem-2", 1, 2, 5000)
    _post(svc, "tx-idem-2", 1, 2, 5000)
    tx = svc.get_transaction("tx-idem-2")
    assert len(tx.entries) == 2  # 只有两条分录，没有重复


# ----------------------------------------------------------------------
# 并发
# ----------------------------------------------------------------------
def test_concurrent_posts_no_lost_update():
    """多线程并发记账到同一批账户，余额不得丢失更新。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "ledger.db")

        def make_service():
            return LedgerService(connect(db_path))

        # 初始化账户
        init = make_service()
        init.create_account(code="1001", name="银行存款", subject_type=SubjectType.ASSET)
        init.create_account(code="2001", name="短期借款", subject_type=SubjectType.LIABILITY)
        init.conn.close()

        amounts = [100, 200, 300, 400, 500, 600, 700, 800]

        def worker(i):
            svc = make_service()
            try:
                _post(svc, f"tx-conc-{i}", 1, 2, amounts[i], f"并发第{i}笔")
            finally:
                svc.conn.close()

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(worker, range(len(amounts))))

        check = make_service()
        # 8 笔交易全部入账，无丢失
        assert len(check.list_transactions()) == 8
        # 余额 = 全部发生额之和
        assert check.get_balance(1).balance == sum(amounts)
        assert check.get_balance(2).balance == sum(amounts)
        check.conn.close()


def test_concurrent_same_key_exactly_one_transaction():
    """多线程使用同一幂等键并发提交，只产生一笔交易。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "ledger.db")

        def make_service():
            return LedgerService(connect(db_path))

        init = make_service()
        init.create_account(code="1001", name="银行存款", subject_type=SubjectType.ASSET)
        init.create_account(code="2001", name="短期借款", subject_type=SubjectType.LIABILITY)
        init.conn.close()

        def worker(i):
            svc = make_service()
            try:
                return _post(svc, "same-key", 1, 2, 10000)
            finally:
                svc.conn.close()

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(worker, range(8)))

        # 所有线程返回同一笔交易
        tx_ids = {r.id for r in results}
        assert len(tx_ids) == 1
        check = make_service()
        assert len(check.list_transactions()) == 1
        assert check.get_balance(1).balance == 10000
        check.conn.close()


# ----------------------------------------------------------------------
# 冲正
# ----------------------------------------------------------------------
def test_reversal(funded_service):
    svc = funded_service
    _post(svc, "tx-rev", 1, 2, 10000, "收到借款")
    original = svc.get_transaction("tx-rev")
    assert original.status == TransactionStatus.POSTED

    reversal = svc.reverse_transaction(original_key="tx-rev", reversal_key="tx-rev-rev")
    # 冲正交易与原交易方向相反
    assert reversal.reversal_of == original.id
    rev_entries = {(e.account_id, e.direction, e.amount) for e in reversal.entries}
    assert rev_entries == {
        (1, Direction.CREDIT, 10000),
        (2, Direction.DEBIT, 10000),
    }

    # 原交易状态已冲正，且仍保留原分录（未被修改/删除）
    original_after = svc.get_transaction("tx-rev")
    assert original_after.status == TransactionStatus.REVERSED
    assert original_after.reversed_by == reversal.id
    assert len(original_after.entries) == 2
    assert {(e.account_id, e.direction, e.amount) for e in original_after.entries} == {
        (1, Direction.DEBIT, 10000),
        (2, Direction.CREDIT, 10000),
    }

    # 冲正后余额归零
    assert svc.get_balance(1).balance == 0
    assert svc.get_balance(2).balance == 0


def test_idempotent_reversal(funded_service):
    svc = funded_service
    _post(svc, "tx-rev-2", 1, 2, 10000)
    first = svc.reverse_transaction(original_key="tx-rev-2", reversal_key="tx-rev-2-rev")
    second = svc.reverse_transaction(original_key="tx-rev-2", reversal_key="tx-rev-2-rev")
    assert second.id == first.id
    # 重复冲正（用原交易 key）也返回同一冲正结果
    third = svc.reverse_transaction(original_key="tx-rev-2", reversal_key="another-key")
    assert third.id == first.id
    assert len(svc.list_transactions()) == 2  # 原交易 + 冲正交易


def test_reversal_nonexistent_raises(funded_service):
    svc = funded_service
    with pytest.raises(TransactionNotFoundError):
        svc.reverse_transaction(original_key="no-such-tx", reversal_key="rev-x")


# ----------------------------------------------------------------------
# 重启恢复
# ----------------------------------------------------------------------
def test_restart_recovery():
    """写入后关闭，重新打开（模拟重启），账本与最后一次已提交状态一致。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "ledger.db")

        # 第一次启动：建账并记账
        svc = LedgerService(connect(db_path))
        svc.create_account(code="1001", name="银行存款", subject_type=SubjectType.ASSET)
        svc.create_account(code="2001", name="短期借款", subject_type=SubjectType.LIABILITY)
        _post(svc, "tx-restart-1", 1, 2, 10000)
        _post(svc, "tx-restart-2", 1, 2, 5000)
        svc.reverse_transaction(original_key="tx-restart-1", reversal_key="tx-restart-1-rev")
        report_before = svc.recover()
        svc.conn.close()

        # 第二次启动：恢复并校验
        svc2 = LedgerService(connect(db_path))
        report_after = svc2.recover()
        assert report_after["transactions"] == report_before["transactions"]
        assert report_after["entries"] == report_before["entries"]
        # 余额与重启前一致
        assert svc2.get_balance(1).balance == 5000  # 10000 被冲正，剩 5000
        assert svc2.get_balance(2).balance == 5000
        # 交易状态可追溯
        assert svc2.get_transaction("tx-restart-1").status == TransactionStatus.REVERSED
        assert svc2.get_transaction("tx-restart-2").status == TransactionStatus.POSTED
        # 无重复分录
        assert len(svc2.get_transaction("tx-restart-1").entries) == 2
        svc2.conn.close()


# ----------------------------------------------------------------------
# 校验
# ----------------------------------------------------------------------
def test_unbalanced_entries_rejected(funded_service):
    svc = funded_service
    with pytest.raises(UnbalancedTransactionError):
        svc.post_transaction(
            idempotency_key="tx-unbal",
            entries=[(1, Direction.DEBIT, 10000), (2, Direction.CREDIT, 9000)],
        )
    # 失败后无任何分录写入
    assert len(svc.list_transactions()) == 0


def test_invalid_direction_rejected(funded_service):
    svc = funded_service
    with pytest.raises(InvalidDirectionError):
        svc.post_transaction(
            idempotency_key="tx-dir",
            entries=[(1, Direction.DEBIT, 10000), (2, "xxx", 10000)],
        )


def test_negative_amount_rejected(funded_service):
    svc = funded_service
    with pytest.raises(InvalidAmountError):
        svc.post_transaction(
            idempotency_key="tx-neg",
            entries=[(1, Direction.DEBIT, -100), (2, Direction.CREDIT, -100)],
        )


def test_insufficient_entries_rejected(funded_service):
    svc = funded_service
    with pytest.raises(InsufficientEntriesError):
        svc.post_transaction(idempotency_key="tx-one", entries=[(1, Direction.DEBIT, 100)])


def test_unknown_account_rejected(funded_service):
    svc = funded_service
    with pytest.raises(AccountNotFoundError):
        svc.post_transaction(
            idempotency_key="tx-unknown",
            entries=[(999, Direction.DEBIT, 100), (2, Direction.CREDIT, 100)],
        )


def test_closed_account_rejected(funded_service):
    svc = funded_service
    svc.accounts.close(1)
    with pytest.raises(AccountClosedError):
        _post(svc, "tx-closed", 1, 2, 100)


def test_balance_direction_violation_rejected(funded_service):
    """资产类账户不允许出现贷方（反向）余额。"""
    svc = funded_service
    with pytest.raises(BalanceViolationError):
        svc.post_transaction(
            idempotency_key="tx-overdraft",
            entries=[(1, Direction.CREDIT, 5000), (3, Direction.DEBIT, 5000)],
        )
    # 拒绝后余额仍为 0
    assert svc.get_balance(1).balance == 0


def test_duplicate_account_code_rejected(service):
    service.create_account(code="1001", name="银行存款", subject_type=SubjectType.ASSET)
    with pytest.raises(AccountExistsError):
        service.create_account(code="1001", name="重复", subject_type=SubjectType.ASSET)


# ----------------------------------------------------------------------
# 流水查询
# ----------------------------------------------------------------------
def test_flow_distinguishes_posted_and_reversed(funded_service):
    svc = funded_service
    _post(svc, "tx-flow", 1, 2, 10000)
    svc.reverse_transaction(original_key="tx-flow", reversal_key="tx-flow-rev")
    flow = svc.list_flow(account_id=1)
    # 银行存款有两条分录：一笔借方（已冲正）、一笔贷方（冲正）
    assert len(flow) == 2
    statuses = {row["transaction_status"] for row in flow}
    assert statuses == {"posted", "reversed"}
    directions = {row["direction"] for row in flow}
    assert directions == {"debit", "credit"}


def test_flow_filter_by_time_range(funded_service):
    svc = funded_service
    _post(svc, "tx-time", 1, 2, 10000)
    # 很宽的范围包含全部
    assert len(svc.list_flow(account_id=1, start_time="2000-01-01", end_time="2999-12-31")) == 1
    # 不包含任何内容的范围
    assert len(svc.list_flow(account_id=1, start_time="2999-01-01")) == 0


def test_list_transactions_filter_by_status(funded_service):
    svc = funded_service
    _post(svc, "tx-fs", 1, 2, 10000)
    svc.reverse_transaction(original_key="tx-fs", reversal_key="tx-fs-rev")
    posted = svc.list_transactions(status=TransactionStatus.POSTED)
    reversed_ = svc.list_transactions(status=TransactionStatus.REVERSED)
    assert len(posted) == 1 and posted[0].idempotency_key == "tx-fs-rev"
    assert len(reversed_) == 1 and reversed_[0].idempotency_key == "tx-fs"
