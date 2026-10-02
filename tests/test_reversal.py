"""冲正测试：反向业务抵消、原始记录不可改/删、状态可追溯。"""

from decimal import Decimal

import pytest

from ledger.errors import (
    AlreadyReversedError,
    ReverseOriginalError,
    TransactionNotFoundError,
)
from ledger.models import Direction, TransactionStatus

from tests.conftest import CASH, EXPENSE, INCOME, make_entries


def _sale(amount: str = "100.00"):
    return make_entries([(CASH, Direction.DEBIT, amount), (INCOME, Direction.CREDIT, amount)])


def test_reversal_creates_opposite_entries(seeded_service):
    original = seeded_service.post_transaction("sale", _sale())
    result = seeded_service.reverse_transaction("sale", "sale-void")

    # 冲正业务方向相反、金额相等，且带原始业务引用
    assert result.transaction.reversal_of_id == original.transaction.id
    assert result.transaction.status is TransactionStatus.POSTED
    original_by_account = {e.account_code: e for e in result.entries}
    assert original_by_account[CASH].direction is Direction.CREDIT
    assert original_by_account[INCOME].direction is Direction.DEBIT
    assert {e.amount for e in result.entries} == {Decimal("100.00")}


def test_reversal_neutralizes_balance(seeded_service):
    seeded_service.post_transaction("sale", _sale("100"))
    assert seeded_service.get_balance(CASH).value == Decimal("100.00")
    seeded_service.reverse_transaction("sale", "sale-void")
    assert seeded_service.get_balance(CASH).value == Decimal("0.00")
    assert seeded_service.get_balance(INCOME).value == Decimal("0.00")


def test_original_records_preserved_after_reversal(seeded_service):
    original = seeded_service.post_transaction("sale", _sale("100"))
    seeded_service.reverse_transaction("sale", "sale-void")

    # 原始业务头与分录仍可查，金额/方向从未被修改，仅状态迁移为 reversed
    txn = seeded_service.get_transaction("sale")
    assert txn.id == original.transaction.id
    assert txn.status is TransactionStatus.REVERSED
    original_entries = seeded_service.get_entries("sale")
    assert all(e.status is TransactionStatus.REVERSED for e in original_entries)
    assert {e.direction for e in original_entries} == {Direction.DEBIT, Direction.CREDIT}
    assert sum((e.amount for e in original_entries), start=Decimal(0)) == Decimal("200.00")


def test_reversal_traceable_in_both_directions(seeded_service):
    seeded_service.post_transaction("sale", _sale())
    reversal = seeded_service.reverse_transaction("sale", "sale-void")

    # 冲正业务 -> 原业务
    assert reversal.transaction.reversal_of_id == seeded_service.get_transaction("sale").id
    # 原业务状态为 reversed；冲正业务为 posted
    assert seeded_service.get_transaction("sale").status is TransactionStatus.REVERSED
    assert seeded_service.get_transaction("sale-void").status is TransactionStatus.POSTED
    # include_reversed 下两者流水均可追溯
    all_entries = seeded_service.query_entries(include_reversed=True)
    assert len(all_entries) == 4
    statuses = {(e.transaction_id, e.status) for e in all_entries}
    assert (seeded_service.get_transaction("sale").id, TransactionStatus.REVERSED) in statuses
    assert (reversal.transaction.id, TransactionStatus.POSTED) in statuses


def test_double_reversal_rejected(seeded_service):
    seeded_service.post_transaction("sale", _sale())
    seeded_service.reverse_transaction("sale", "void-1")
    with pytest.raises(AlreadyReversedError):
        seeded_service.reverse_transaction("sale", "void-2")


def test_cannot_reverse_a_reversal(seeded_service):
    seeded_service.post_transaction("sale", _sale())
    seeded_service.reverse_transaction("sale", "void-1")
    with pytest.raises(ReverseOriginalError):
        seeded_service.reverse_transaction("void-1", "void-2")


def test_reverse_unknown_transaction(seeded_service):
    with pytest.raises(TransactionNotFoundError):
        seeded_service.reverse_transaction("nope", "void-nope")


def test_new_business_after_reversal_still_works(seeded_service):
    seeded_service.post_transaction("sale-1", _sale("100"))
    seeded_service.reverse_transaction("sale-1", "void-1")
    # 冲正后继续发生的正常业务照常入账
    seeded_service.post_transaction(
        "sale-2",
        make_entries([(CASH, Direction.DEBIT, "40"), (INCOME, Direction.CREDIT, "40")]),
    )
    assert seeded_service.get_balance(CASH).value == Decimal("40.00")
    entries = seeded_service.query_entries()  # 有效集
    assert len(entries) == 2
    assert entries[0].transaction_id == seeded_service.get_transaction("sale-2").id


def test_multi_account_reversal(seeded_service):
    # 复合分录：借现金60 + 借费用40 / 贷收入100
    seeded_service.post_transaction(
        "compound",
        make_entries([
            (CASH, Direction.DEBIT, "60"),
            (EXPENSE, Direction.DEBIT, "40"),
            (INCOME, Direction.CREDIT, "100"),
        ]),
    )
    seeded_service.reverse_transaction("compound", "compound-void")
    for code in (CASH, EXPENSE, INCOME):
        assert seeded_service.get_balance(code).value == Decimal("0.00"), code
