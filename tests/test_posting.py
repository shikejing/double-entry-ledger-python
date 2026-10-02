"""基础记账：原子性、科目类型余额方向、多分录、时间过滤。"""

from decimal import Decimal

import pytest

from ledger.errors import UnbalancedEntryError
from ledger.models import AccountType, Direction, TransactionStatus

from tests.conftest import CASH, EQUITY, EXPENSE, INCOME, LOAN, make_entries


def test_post_balanced_transaction(seeded_service):
    result = seeded_service.post_transaction(
        "sale-1",
        make_entries([(CASH, Direction.DEBIT, "100.00"), (INCOME, Direction.CREDIT, "100.00")]),
        description="销售收款",
    )
    assert result.idempotent_hit is False
    assert result.transaction.status is TransactionStatus.POSTED
    assert result.transaction.reversal_of_id is None
    assert len(result.entries) == 2
    assert seeded_service.get_balance(CASH).value == Decimal("100.00")
    # 贷方类科目（收入）余额按正常方向取符号
    assert seeded_service.get_balance(INCOME).value == Decimal("100.00")


def test_all_account_type_balances(seeded_service):
    # 借现金 / 贷收入
    seeded_service.post_transaction(
        "t1", make_entries([(CASH, Direction.DEBIT, "100"), (INCOME, Direction.CREDIT, "100")])
    )
    # 借管理费用 / 贷现金
    seeded_service.post_transaction(
        "t2", make_entries([(EXPENSE, Direction.DEBIT, "30"), (CASH, Direction.CREDIT, "30")])
    )
    # 借现金 / 贷短期借款
    seeded_service.post_transaction(
        "t3", make_entries([(CASH, Direction.DEBIT, "50"), (LOAN, Direction.CREDIT, "50")])
    )
    # 借现金 / 贷实收资本
    seeded_service.post_transaction(
        "t4", make_entries([(CASH, Direction.DEBIT, "20"), (EQUITY, Direction.CREDIT, "20")])
    )
    assert seeded_service.get_balance(CASH).value == Decimal("140.00")  # 100-30+50+20
    assert seeded_service.get_balance(INCOME).value == Decimal("100.00")
    assert seeded_service.get_balance(EXPENSE).value == Decimal("30.00")
    assert seeded_service.get_balance(LOAN).value == Decimal("50.00")
    assert seeded_service.get_balance(EQUITY).value == Decimal("20.00")


def test_multi_entry_atomic_post(seeded_service):
    result = seeded_service.post_transaction(
        "multi",
        make_entries([
            (CASH, Direction.DEBIT, "60"),
            (EXPENSE, Direction.DEBIT, "40"),
            (INCOME, Direction.CREDIT, "70"),
            (LOAN, Direction.CREDIT, "30"),
        ]),
    )
    assert len(result.entries) == 4
    assert seeded_service.get_balance(CASH).debit_total == Decimal("60.00")


def test_failed_post_leaves_nothing(seeded_service):
    with pytest.raises(UnbalancedEntryError):
        seeded_service.post_transaction(
            "bad",
            make_entries([(CASH, Direction.DEBIT, "10"), (INCOME, Direction.CREDIT, "11")]),
        )
    # 失败后：没有业务头，没有分录，余额为零，业务标识可被合法单据复用
    assert seeded_service.query_entries(include_reversed=True) == []
    assert seeded_service.get_balance(CASH).value == 0
    ok = seeded_service.post_transaction(
        "bad",  # 同一标识重新用于合法单据，不应被失败尝试占用
        make_entries([(CASH, Direction.DEBIT, "11"), (INCOME, Direction.CREDIT, "11")]),
    )
    assert ok.idempotent_hit is False


def test_balance_of_empty_account_is_zero(seeded_service):
    balance = seeded_service.get_balance(CASH)
    assert balance.value == Decimal("0.00")
    assert balance.debit_total == balance.credit_total == Decimal("0.00")


def test_query_entries_by_account_and_status(seeded_service):
    seeded_service.post_transaction(
        "t1", make_entries([(CASH, Direction.DEBIT, "100"), (INCOME, Direction.CREDIT, "100")])
    )
    seeded_service.post_transaction(
        "t2", make_entries([(EXPENSE, Direction.DEBIT, "20"), (CASH, Direction.CREDIT, "20")])
    )
    cash_entries = seeded_service.query_entries(account_code=CASH)
    assert {e.account_code for e in cash_entries} == {CASH}
    assert len(cash_entries) == 2
    assert seeded_service.query_entries(account_code=EXPENSE)[0].status is TransactionStatus.POSTED


def test_query_entries_by_time_range(service_factory):
    """通过注入时钟验证按创建时间范围过滤。"""
    from datetime import datetime as dt

    times = iter([
        dt(2026, 1, 1, 10, 0, 0),
        dt(2026, 2, 1, 10, 0, 0),
        dt(2026, 3, 1, 10, 0, 0),
    ])
    svc = service_factory()
    svc.create_account(CASH, "现金", AccountType.ASSET)
    svc.create_account(INCOME, "收入", AccountType.INCOME)
    svc._clock = lambda: next(times)
    for bid in ["jan", "feb", "mar"]:
        svc.post_transaction(
            bid, make_entries([(CASH, Direction.DEBIT, "10"), (INCOME, Direction.CREDIT, "10")])
        )
    entries = svc.query_entries(start=dt(2026, 1, 15), end=dt(2026, 2, 15))
    assert {e.transaction_id for e in entries} == {svc.get_transaction("feb").id}
    entries = svc.query_entries(start=dt(2026, 2, 1))
    assert {e.transaction_id for e in entries} == {
        svc.get_transaction(b).id for b in ["feb", "mar"]
    }
