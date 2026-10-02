"""FastAPI HTTP 接口层。

接口语义
========
* ``POST /accounts``                 创建科目
* ``GET  /accounts``                 列出科目
* ``POST /transactions``             原子记账（按 business_id 幂等）
* ``POST /transactions/{business_id}/reverse``  冲正
* ``GET  /transactions/{business_id}``         查询业务及分录
* ``GET  /entries``                  流水查询（科目/时间范围/是否含已冲正）
* ``GET  /balances/{account_code}``  余额查询

金额一律以字符串形式传输十进制数，避免 JSON 浮点误差。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ledger.errors import (
    AccountNotFoundError,
    AlreadyReversedError,
    DuplicateAccountError,
    DuplicateTransactionError,
    LedgerError,
    ReverseOriginalError,
    TransactionNotFoundError,
)
from ledger.models import (
    Account,
    AccountType,
    Balance,
    Direction,
    Entry,
    EntryInput,
    PostResult,
    Transaction,
    TransactionStatus,
)
from ledger.service import LedgerService


# ---- 请求模型 ----
class EntryIn(BaseModel):
    account_code: str = Field(..., min_length=1)
    direction: Direction
    amount: Decimal = Field(..., gt=0)


class PostIn(BaseModel):
    business_id: str = Field(..., min_length=1)
    description: str = ""
    entries: list[EntryIn] = Field(..., min_length=1)


class ReverseIn(BaseModel):
    reversal_business_id: str = Field(..., min_length=1)
    description: str = ""


class AccountIn(BaseModel):
    code: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    account_type: AccountType


# ---- 序列化 ----
def _account_out(a: Account) -> dict[str, Any]:
    return {
        "code": a.code,
        "name": a.name,
        "account_type": a.account_type.value,
        "created_at": a.created_at.isoformat(),
    }


def _txn_out(t: Transaction) -> dict[str, Any]:
    return {
        "id": t.id,
        "business_id": t.business_id,
        "description": t.description,
        "status": t.status.value,
        "reversal_of_id": t.reversal_of_id,
        "created_at": t.created_at.isoformat(),
    }


def _entry_out(e: Entry) -> dict[str, Any]:
    return {
        "id": e.id,
        "transaction_id": e.transaction_id,
        "sequence": e.sequence,
        "account_code": e.account_code,
        "direction": e.direction.value,
        "amount": str(e.amount),
        "status": e.status.value,
    }


def _balance_out(b: Balance) -> dict[str, Any]:
    return {
        "account_code": b.account_code,
        "debit_total": str(b.debit_total),
        "credit_total": str(b.credit_total),
        "net_debit": str(b.net_debit),
        "balance": str(b.value),
    }


# 错误 -> HTTP 状态码
_STATUS_MAP = {
    DuplicateAccountError: 409,
    DuplicateTransactionError: 409,
    AccountNotFoundError: 404,
    TransactionNotFoundError: 404,
    AlreadyReversedError: 409,
    ReverseOriginalError: 409,
}


def create_app(service: LedgerService) -> FastAPI:
    app = FastAPI(title="复式记账账本服务", version="0.1.0")

    @app.exception_handler(LedgerError)
    async def _handle_ledger_error(_request, exc: LedgerError) -> JSONResponse:
        status_code = _STATUS_MAP.get(type(exc), 400)
        return JSONResponse(
            status_code=status_code,
            content={"error": type(exc).__name__, "message": str(exc)},
        )

    @app.post("/accounts", status_code=201)
    def create_account(body: AccountIn) -> dict[str, Any]:
        account = service.create_account(body.code, body.name, body.account_type)
        return _account_out(account)

    @app.get("/accounts")
    def list_accounts() -> dict[str, Any]:
        return {"accounts": [_account_out(a) for a in service.list_accounts()]}

    @app.post("/transactions", status_code=201)
    def post_transaction(body: PostIn) -> dict[str, Any]:
        entries = [
            EntryInput(account_code=e.account_code, direction=e.direction, amount=e.amount)
            for e in body.entries
        ]
        result: PostResult = service.post_transaction(
            body.business_id, entries, body.description
        )
        return {
            "idempotent_hit": result.idempotent_hit,
            "transaction": _txn_out(result.transaction),
            "entries": [_entry_out(e) for e in result.entries],
        }

    @app.post("/transactions/{business_id}/reverse", status_code=201)
    def reverse_transaction(business_id: str, body: ReverseIn) -> dict[str, Any]:
        result = service.reverse_transaction(
            business_id, body.reversal_business_id, body.description
        )
        return {
            "idempotent_hit": result.idempotent_hit,
            "transaction": _txn_out(result.transaction),
            "entries": [_entry_out(e) for e in result.entries],
        }

    @app.get("/transactions/{business_id}")
    def get_transaction(business_id: str) -> dict[str, Any]:
        txn = service.get_transaction(business_id)
        entries = service.get_entries(business_id)
        return {"transaction": _txn_out(txn), "entries": [_entry_out(e) for e in entries]}

    @app.get("/entries")
    def query_entries(
        account_code: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        include_reversed: bool = False,
    ) -> dict[str, Any]:
        entries = service.query_entries(
            account_code=account_code, start=start, end=end,
            include_reversed=include_reversed,
        )
        return {
            "filters": {
                "account_code": account_code,
                "start": start.isoformat() if start else None,
                "end": end.isoformat() if end else None,
                "include_reversed": include_reversed,
            },
            "entries": [_entry_out(e) for e in entries],
        }

    @app.get("/balances/{account_code}")
    def get_balance(
        account_code: str, include_reversed: bool = False
    ) -> dict[str, Any]:
        return _balance_out(
            service.get_balance(account_code, include_reversed=include_reversed)
        )

    @app.get("/accounts/{account_code}/statement")
    def get_statement(
        account_code: str,
        start: datetime | None = None,
        end: datetime | None = None,
        include_reversed: bool = False,
    ) -> dict[str, Any]:
        """同一快照下的科目余额 + 明细流水，二者严格自洽。"""
        balance, entries = service.get_account_statement(
            account_code, start=start, end=end, include_reversed=include_reversed
        )
        return {
            "balance": _balance_out(balance),
            "entries": [_entry_out(e) for e in entries],
        }

    return app
