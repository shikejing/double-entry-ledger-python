"""幂等提交测试：同 business_id 重复提交只入账一次。"""

from decimal import Decimal

import pytest

from ledger.errors import DuplicateTransactionError
from ledger.models import Direction

from tests.conftest import CASH, INCOME, make_entries


def _sale(amount: str = "100.00"):
    return make_entries([(CASH, Direction.DEBIT, amount), (INCOME, Direction.CREDIT, amount)])


def test_repeated_submit_returns_previous_result(seeded_service):
    first = seeded_service.post_transaction("biz-1", _sale())
    second = seeded_service.post_transaction("biz-1", _sale())

    assert first.idempotent_hit is False
    assert second.idempotent_hit is True
    assert second.transaction.id == first.transaction.id
    assert tuple(second.entries) == tuple(first.entries)
    # 余额只增长一次
    assert seeded_service.get_balance(CASH).value == Decimal("100.00")


def test_repeated_submit_counts_as_one(seeded_service):
    for _ in range(10):
        seeded_service.post_transaction("biz-x", _sale("12.34"))
    entries = seeded_service.query_entries()
    assert len(entries) == 2
    assert seeded_service.get_balance(CASH).value == Decimal("12.34")


def test_same_id_different_content_rejected(seeded_service):
    seeded_service.post_transaction("biz-1", _sale("100"))
    with pytest.raises(DuplicateTransactionError):
        seeded_service.post_transaction("biz-1", _sale("200"))
    # 被拒绝后仍是首次入账的结果
    assert seeded_service.get_balance(CASH).value == Decimal("100.00")


def test_different_ids_both_posted(seeded_service):
    r1 = seeded_service.post_transaction("a", _sale("10"))
    r2 = seeded_service.post_transaction("b", _sale("10"))
    assert r1.idempotent_hit is r2.idempotent_hit is False
    assert r1.transaction.id != r2.transaction.id
    assert seeded_service.get_balance(CASH).value == Decimal("20.00")


def test_idempotent_after_reversal_returns_reversed_status(seeded_service):
    seeded_service.post_transaction("biz-1", _sale())
    seeded_service.reverse_transaction("biz-1", "rev-1")
    hit = seeded_service.post_transaction("biz-1", _sale())
    assert hit.idempotent_hit is True
    assert hit.transaction.status.value == "reversed"
    assert all(e.status.value == "reversed" for e in hit.entries)
    assert seeded_service.get_balance(CASH).value == 0


def test_reversal_id_idempotent_conflict(seeded_service):
    seeded_service.post_transaction("biz-1", _sale())
    seeded_service.post_transaction("biz-2", _sale("5"))
    seeded_service.reverse_transaction("biz-1", "rev-1")
    # 冲正号撞上已存在的普通业务标识 -> 409
    with pytest.raises(DuplicateTransactionError):
        seeded_service.reverse_transaction("biz-2", "biz-1")
    # biz-2 未受影响
    assert seeded_service.get_transaction("biz-2").status.value == "posted"
