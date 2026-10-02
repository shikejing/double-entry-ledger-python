"""HTTP 接口端到端测试。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ledger.api import create_app
from ledger.db import connect
from ledger.service import LedgerService


@pytest.fixture
def client():
    conn = connect(None)
    service = LedgerService(conn)
    app = create_app(service)
    with TestClient(app) as c:
        yield c
    conn.close()


def _create_account(client, code, name, subject_type):
    r = client.post(
        "/accounts",
        json={"code": code, "name": name, "subject_type": subject_type},
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_create_account_and_list(client):
    acc = _create_account(client, "1001", "银行存款", "asset")
    assert acc["normal_direction"] == "debit"
    assert acc["status"] == "active"
    accounts = client.get("/accounts").json()
    assert len(accounts) == 1


def test_post_transaction_api(client):
    _create_account(client, "1001", "银行存款", "asset")
    _create_account(client, "2001", "短期借款", "liability")
    r = client.post(
        "/transactions",
        json={
            "idempotency_key": "api-tx-1",
            "description": "收到借款",
            "entries": [
                {"account_id": 1, "direction": "debit", "amount": "100.00"},
                {"account_id": 2, "direction": "credit", "amount": "100.00"},
            ],
        },
    )
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["status"] == "posted"
    assert len(data["entries"]) == 2
    # 余额（分）
    bal = client.get("/accounts/1/balance").json()
    assert bal["balance_cents"] == 10000


def test_idempotent_post_api(client):
    _create_account(client, "1001", "银行存款", "asset")
    _create_account(client, "2001", "短期借款", "liability")
    body = {
        "idempotency_key": "api-idem",
        "entries": [
            {"account_id": 1, "direction": "debit", "amount": "50.00"},
            {"account_id": 2, "direction": "credit", "amount": "50.00"},
        ],
    }
    r1 = client.post("/transactions", json=body)
    r2 = client.post("/transactions", json=body)
    assert r1.json()["id"] == r2.json()["id"]
    assert len(client.get("/transactions").json()) == 1


def test_reverse_transaction_api(client):
    _create_account(client, "1001", "银行存款", "asset")
    _create_account(client, "2001", "短期借款", "liability")
    client.post(
        "/transactions",
        json={
            "idempotency_key": "api-rev",
            "entries": [
                {"account_id": 1, "direction": "debit", "amount": "100.00"},
                {"account_id": 2, "direction": "credit", "amount": "100.00"},
            ],
        },
    )
    r = client.post("/transactions/api-rev/reverse", json={"reversal_key": "api-rev-rev"})
    assert r.status_code == 201, r.text
    rev = r.json()
    assert rev["reversal_of"] is not None
    # 原交易已冲正
    original = client.get("/transactions/api-rev").json()
    assert original["status"] == "reversed"
    # 余额归零
    assert client.get("/accounts/1/balance").json()["balance_cents"] == 0
    # 流水中能区分两种状态
    flow = client.get("/accounts/1/flow").json()
    statuses = {row["transaction_status"] for row in flow}
    assert statuses == {"posted", "reversed"}


def test_unbalanced_rejected_api(client):
    _create_account(client, "1001", "银行存款", "asset")
    _create_account(client, "2001", "短期借款", "liability")
    r = client.post(
        "/transactions",
        json={
            "idempotency_key": "api-unbal",
            "entries": [
                {"account_id": 1, "direction": "debit", "amount": "100.00"},
                {"account_id": 2, "direction": "credit", "amount": "90.00"},
            ],
        },
    )
    assert r.status_code == 400
    assert "借贷不平" in r.json()["detail"]


def test_invalid_direction_rejected_api(client):
    _create_account(client, "1001", "银行存款", "asset")
    _create_account(client, "2001", "短期借款", "liability")
    r = client.post(
        "/transactions",
        json={
            "idempotency_key": "api-dir",
            "entries": [
                {"account_id": 1, "direction": "debit", "amount": "100.00"},
                {"account_id": 2, "direction": "bad", "amount": "100.00"},
            ],
        },
    )
    assert r.status_code == 400


def test_recover_endpoint(client):
    r = client.post("/recover")
    assert r.status_code == 200
    body = r.json()
    assert body["issues"] == []
