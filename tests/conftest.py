"""pytest 公共夹具与辅助。"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest

from ledger.models import AccountType, Direction, EntryInput
from ledger.repository import LedgerRepository
from ledger.service import LedgerService


@pytest.fixture
def service() -> Iterator[LedgerService]:
    """全新的内存账本服务。"""
    svc = LedgerService(LedgerRepository(":memory:"))
    yield svc
    svc.close()


@pytest.fixture
def seeded_service(service: LedgerService) -> LedgerService:
    """预置 5 类常用科目的账本。"""
    service.create_account("1001", "现金", AccountType.ASSET)
    service.create_account("2001", "短期借款", AccountType.LIABILITY)
    service.create_account("3001", "实收资本", AccountType.EQUITY)
    service.create_account("4001", "主营业务收入", AccountType.INCOME)
    service.create_account("5001", "管理费用", AccountType.EXPENSE)
    return service


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "ledger-test.db"


@pytest.fixture
def service_factory(db_path: Path):
    """可反复打开同一数据库文件的服务工厂，用于重启恢复测试。"""
    created: list[LedgerService] = []

    def _factory(path: str | Path | None = None) -> LedgerService:
        svc = LedgerService(LedgerRepository(str(path if path is not None else db_path)))
        created.append(svc)
        return svc

    yield _factory
    for svc in created:
        svc.close()


def make_entries(pairs: list[tuple[str, Direction, str]]) -> list[EntryInput]:
    """[(科目, 方向, 金额字符串), ...] -> 分录输入。"""
    return [
        EntryInput(account_code=code, direction=direction, amount=Decimal(amount))
        for code, direction, amount in pairs
    ]


CASH = "1001"
INCOME = "4001"
EXPENSE = "5001"
LOAN = "2001"
EQUITY = "3001"
