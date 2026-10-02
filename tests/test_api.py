"""FastAPI 端到端集成测试。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ledger.api import create_app
from ledger.repository import LedgerRepository
from ledger.service import LedgerService


@pytest.fixture
def client():
    svc = LedgerService(LedgerRepository(":memory:"))
    with TestClient(create_app(svc)) as c:
        yield c
    svc.close()


def _create_accounts(c: TestClient) -> None:
    assert c.post("/accounts", json={"code": "1001", "name": "现金", "account_type": "asset"}).status_code == 201
    assert c.post("/accounts", json={"code": "4001", "name": "收入", "account_type": "income"}).status_code == 201


def _sale(bid: str, amount: str = "100.00") -> dict:
    return {
        "business_id": bid,
        "entries": [
            {"account_code": "1001", "direction": "debit", "amount": amount},
            {"account_code": "4001", "direction": "credit", "amount": amount},
        ],
    }


def test_full_lifecycle(client):
    _create_accounts(client)
    # 记账 + 幂等
    r1 = client.post("/transactions", json=_sale("b1"))
    r2 = client.post("/transactions", json=_sale("b1"))
    assert r1.status_code == 201 and r1.json()["idempotent_hit"] is False
    assert r2.json()["idempotent_hit"] is True
    # 余额
    assert client.get("/balances/1001").json()["balance"] == "100.00"
    # 冲正
    r = client.post("/transactions/b1/reverse", json={"reversal_business_id": "r1"})
    assert r.status_code == 201
    assert client.get("/balances/1001").json()["balance"] == "0.00"
    # 业务状态可追溯
    txn = client.get("/transactions/b1").json()
    assert txn["transaction"]["status"] == "reversed"
    # 对账单自洽
    stmt = client.get("/accounts/1001/statement", params={"include_reversed": "true"}).json()
    assert stmt["balance"]["balance"] == "0.00"
    assert len(stmt["entries"]) == 2  # 原借 + 冲正贷


def test_error_status_codes(client):
    _create_accounts(client)
    # 借贷不平 -> 400 + 可解释错误
    r = client.post("/transactions", json=_sale("b1", "100") | {
        "entries": [
            {"account_code": "1001", "direction": "debit", "amount": "100"},
            {"account_code": "4001", "direction": "credit", "amount": "99"},
        ]})
    assert r.status_code == 400
    assert r.json()["error"] == "UnbalancedEntryError"
    assert r.json()["message"]
    # 科目不存在 -> 404
    assert client.get("/balances/7777").status_code == 404
    # 正常记账
    client.post("/transactions", json=_sale("b1"))
    # 重复冲正 -> 409
    client.post("/transactions/b1/reverse", json={"reversal_business_id": "r1"})
    assert client.post("/transactions/b1/reverse", json={"reversal_business_id": "r2"}).status_code == 409


def test_query_filters(client):
    _create_accounts(client)
    client.post("/transactions", json=_sale("b1", "10"))
    client.post("/transactions", json=_sale("b2", "20"))
    client.post("/transactions/b1/reverse", json={"reversal_business_id": "r1"})

    # 默认有效集只有 b2
    entries = client.get("/entries").json()["entries"]
    assert len(entries) == 2
    # 科目过滤
    cash = client.get("/entries", params={"account_code": "1001"}).json()["entries"]
    assert all(e["account_code"] == "1001" for e in cash)
    # 含冲正
    all_entries = client.get("/entries", params={"include_reversed": "true"}).json()["entries"]
    assert len(all_entries) == 6


def test_amount_must_be_positive(client):
    _create_accounts(client)
    r = client.post("/transactions", json={
        "business_id": "b1",
        "entries": [
            {"account_code": "1001", "direction": "debit", "amount": "0"},
            {"account_code": "4001", "direction": "credit", "amount": "0"},
        ]})
    assert r.status_code == 422  # pydantic 层拦截
