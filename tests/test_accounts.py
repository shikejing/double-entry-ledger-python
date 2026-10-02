"""科目创建与分录合法性校验测试。"""

from decimal import Decimal

import pytest

from ledger.errors import (
    AccountNotFoundError,
    DuplicateAccountError,
    EmptyTransactionError,
    InvalidEntryError,
    UnbalancedEntryError,
)
from ledger.models import AccountType, Direction, EntryInput

from tests.conftest import CASH, INCOME, make_entries


def test_create_account_and_list(service):
    account = service.create_account("1001", "现金", AccountType.ASSET)
    assert account.code == "1001"
    assert account.account_type is AccountType.ASSET
    assert [a.code for a in service.list_accounts()] == ["1001"]


def test_duplicate_account_rejected(service):
    service.create_account("1001", "现金", AccountType.ASSET)
    with pytest.raises(DuplicateAccountError):
        service.create_account("1001", "另一个现金", AccountType.ASSET)


@pytest.mark.parametrize("code,name", [("", "现金"), ("   ", "现金"), ("1001", ""), ("1001", "  ")])
def test_blank_account_fields_rejected(service, code, name):
    with pytest.raises(InvalidEntryError):
        service.create_account(code, name, AccountType.ASSET)


def test_invalid_account_type_rejected(service):
    with pytest.raises(InvalidEntryError):
        service.create_account("x", "x", "not-a-type")  # type: ignore[arg-type]


def test_entry_against_unknown_account_rejected(seeded_service):
    with pytest.raises(AccountNotFoundError):
        seeded_service.post_transaction(
            "b1",
            make_entries([("9999", Direction.DEBIT, "10"), (INCOME, Direction.CREDIT, "10")]),
        )


@pytest.mark.parametrize("bad_amount", ["0", "-1", "-0.01"])
def test_non_positive_amount_rejected(seeded_service, bad_amount):
    with pytest.raises(InvalidEntryError):
        seeded_service.post_transaction(
            "b1",
            [
                EntryInput(CASH, Direction.DEBIT, Decimal(bad_amount)),
                EntryInput(INCOME, Direction.CREDIT, Decimal(bad_amount) if Decimal(bad_amount) > 0 else Decimal("1")),
            ],
        )


def test_empty_entries_rejected(seeded_service):
    with pytest.raises(EmptyTransactionError):
        seeded_service.post_transaction("b1", [])


def test_single_direction_entries_rejected(seeded_service):
    with pytest.raises(UnbalancedEntryError):
        seeded_service.post_transaction(
            "b1",
            make_entries([(CASH, Direction.DEBIT, "10"), (CASH, Direction.DEBIT, "10")]),
        )


def test_unbalanced_entries_rejected(seeded_service):
    with pytest.raises(UnbalancedEntryError) as exc:
        seeded_service.post_transaction(
            "b1",
            make_entries([(CASH, Direction.DEBIT, "10.00"), (INCOME, Direction.CREDIT, "9.99")]),
        )
    assert "10.00" in str(exc.value) and "9.99" in str(exc.value)


def test_invalid_direction_rejected(seeded_service):
    with pytest.raises(InvalidEntryError):
        seeded_service.post_transaction(
            "b1",
            [
                EntryInput(CASH, "left", Decimal("10")),  # type: ignore[arg-type]
                EntryInput(INCOME, Direction.CREDIT, Decimal("10")),
            ],
        )


def test_blank_business_id_rejected(seeded_service):
    with pytest.raises(InvalidEntryError):
        seeded_service.post_transaction(
            "  ",
            make_entries([(CASH, Direction.DEBIT, "10"), (INCOME, Direction.CREDIT, "10")]),
        )
