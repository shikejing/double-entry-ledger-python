"""余额与流水查询：状态区分、科目与时间过滤。"""

from datetime import datetime
from decimal import Decimal

import pytest

from ledger.errors import AccountNotFoundError, TransactionNotFoundError
from ledger.models import AccountType, Direction, TransactionStatus

from tests.conftest import CASH, EXPENSE, INCOME, make_entries


def test_entries_default_excludes_reversed(seeded_service):
    seeded_service.post_transaction(
        "s1", make_entries([(CASH, Direction.DEBIT, "100"), (INCOME, Direction.CREDIT, "100")])
    )
    seeded_service.post_transaction(
        "s2", make_entries([(CASH, Direction.DEBIT, "50"), (INCOME, Direction.CREDIT, "50")])
    )
    seeded_service.reverse_transaction("s1", "r1")

    effective = seeded_service.query_entries()
    assert len(effective) == 2
    assert {e.transaction_id for e in effective} == {
        seeded_service.get_transaction("s2").id
    }
    assert all(e.status is TransactionStatus.POSTED for e in effective)


def test_entries_include_reversed_flags_status(seeded_service):
    seeded_service.post_transaction(
        "s1", make_entries([(CASH, Direction.DEBIT, "100"), (INCOME, Direction.CREDIT, "100")])
    )
    seeded_service.reverse_transaction("s1", "r1")

    all_entries = seeded_service.query_entries(include_reversed=True)
    assert len(all_entries) == 4
    posted = [e for e in all_entries if e.status is TransactionStatus.POSTED]
    reversed_ = [e for e in all_entries if e.status is TransactionStatus.REVERSED]
    assert len(posted) == 2 and len(reversed_) == 2


def test_filter_by_account(seeded_service):
    seeded_service.post_transaction(
        "s1", make_entries([(CASH, Direction.DEBIT, "100"), (INCOME, Direction.CREDIT, "100")])
    )
    seeded_service.post_transaction(
        "s2", make_entries([(EXPENSE, Direction.DEBIT, "30"), (CASH, Direction.CREDIT, "30")])
    )
    cash = seeded_service.query_entries(account_code=CASH)
    assert {e.account_code for e in cash} == {CASH}
    assert len(cash) == 2
    income = seeded_service.query_entries(account_code=INCOME)
    assert len(income) == 1 and income[0].direction is Direction.CREDIT


def test_filter_by_account_unknown_raises(seeded_service):
    with pytest.raises(AccountNotFoundError):
        seeded_service.query_entries(account_code="ghost")


def test_filter_by_time_range(service_factory):
    times = iter([
        datetime(2026, 3, 1), datetime(2026, 4, 1), datetime(2026, 5, 1),
    ])
    svc = service_factory()
    svc.create_account(CASH, "现金", AccountType.ASSET)
    svc.create_account(INCOME, "收入", AccountType.INCOME)
    svc._clock = lambda: next(times)
    for bid in ("m3", "m4", "m5"):
        svc.post_transaction(
            bid, make_entries([(CASH, Direction.DEBIT, "10"), (INCOME, Direction.CREDIT, "10")])
        )

    apr_only = svc.query_entries(start=datetime(2026, 3, 2), end=datetime(2026, 4, 30))
    assert {e.transaction_id for e in apr_only} == {svc.get_transaction("m4").id}

    from_apr = svc.query_entries(start=datetime(2026, 4, 1))
    assert {e.transaction_id for e in from_apr} == {
        svc.get_transaction(b).id for b in ("m4", "m5")
    }

    until_mar = svc.query_entries(end=datetime(2026, 3, 1))
    assert {e.transaction_id for e in until_mar} == {svc.get_transaction("m3").id}


def test_balance_excludes_reversed_pair(seeded_service):
    seeded_service.post_transaction(
        "s1", make_entries([(CASH, Direction.DEBIT, "100"), (INCOME, Direction.CREDIT, "100")])
    )
    seeded_service.reverse_transaction("s1", "r1")
    seeded_service.post_transaction(
        "s2", make_entries([(CASH, Direction.DEBIT, "25"), (INCOME, Direction.CREDIT, "25")])
    )
    assert seeded_service.get_balance(CASH).value == Decimal("25.00")
    # include_reversed 时冲正双方净额抵消，结果仍为 25
    assert seeded_service.get_balance(CASH, include_reversed=True).value == Decimal("25.00")
    # 但借贷双方累计额不同：含冲正历史时借方累计为 100+25
    gross = seeded_service.get_balance(CASH, include_reversed=True)
    assert gross.debit_total == Decimal("125.00")
    assert gross.credit_total == Decimal("100.00")


def test_statement_is_internally_consistent(seeded_service):
    seeded_service.post_transaction(
        "s1", make_entries([(CASH, Direction.DEBIT, "100"), (INCOME, Direction.CREDIT, "100")])
    )
    seeded_service.reverse_transaction("s1", "r1")
    seeded_service.post_transaction(
        "s2", make_entries([(CASH, Direction.DEBIT, "25"), (INCOME, Direction.CREDIT, "25")])
    )
    balance, entries = seeded_service.get_account_statement(CASH)
    debit = sum((e.amount for e in entries if e.direction is Direction.DEBIT), Decimal(0))
    credit = sum((e.amount for e in entries if e.direction is Direction.CREDIT), Decimal(0))
    assert balance.net_debit == debit - credit


def test_get_transaction_not_found(seeded_service):
    with pytest.raises(TransactionNotFoundError):
        seeded_service.get_transaction("missing")
