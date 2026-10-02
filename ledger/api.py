"""HTTP 接口层（FastAPI）。

金额在接口层以十进制字符串/数字接收，统一转换为分（整数）后交给服务层，
避免浮点误差。
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .errors import LedgerError
from .models import Direction, SubjectType, TransactionStatus
from .service import LedgerService


def _to_cents(amount: Decimal) -> int:
    """把十进制金额转换为分（整数），最多两位小数，且必须为正。"""
    try:
        value = Decimal(amount)
    except (InvalidOperation, TypeError):
        raise HTTPException(status_code=400, detail=f"非法金额: {amount!r}")
    if value <= 0:
        raise HTTPException(status_code=400, detail=f"金额必须为正数: {value}")
    cents = value * 100
    if cents != cents.to_integral_value():
        raise HTTPException(status_code=400, detail=f"金额最多支持两位小数: {value}")
    return int(cents)


class EntryIn(BaseModel):
    account_id: int = Field(..., description="账户 id")
    direction: str = Field(..., description="借 debit / 贷 credit")
    amount: Decimal = Field(..., description="金额（元），正数，最多两位小数")


class TransactionIn(BaseModel):
    idempotency_key: str = Field(..., description="业务唯一标识，幂等键")
    description: Optional[str] = None
    entries: list[EntryIn] = Field(..., min_length=2, description="分录列表，至少两条，借贷相等")


class ReverseIn(BaseModel):
    reversal_key: str = Field(..., description="冲正交易的幂等键")
    description: Optional[str] = None


class AccountIn(BaseModel):
    code: str
    name: str
    subject_type: str = Field(..., description="asset/liability/equity/revenue/expense")


def _parse_direction(value: str) -> Direction:
    try:
        return Direction(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"非法方向: {value!r}，应为 debit 或 credit")


def _parse_subject_type(value: str) -> SubjectType:
    try:
        return SubjectType(value)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"非法科目类型: {value!r}，应为 asset/liability/equity/revenue/expense",
        )


def _parse_status(value: Optional[str]) -> Optional[TransactionStatus]:
    if value is None:
        return None
    try:
        return TransactionStatus(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"非法状态: {value!r}，应为 posted 或 reversed")


def _tx_to_dict(tx) -> dict:
    return {
        "id": tx.id,
        "idempotency_key": tx.idempotency_key,
        "description": tx.description,
        "status": tx.status.value,
        "reversal_of": tx.reversal_of,
        "reversed_by": tx.reversed_by,
        "created_at": tx.created_at,
        "entries": [
            {
                "id": e.id,
                "account_id": e.account_id,
                "direction": e.direction.value,
                "amount": e.amount,
            }
            for e in tx.entries
        ],
    }


def create_app(service: LedgerService) -> FastAPI:
    app = FastAPI(title="复式记账账本服务", version="0.1.0")

    @app.exception_handler(LedgerError)
    async def ledger_error_handler(request, exc: LedgerError):
        raise HTTPException(status_code=400, detail=str(exc))

    @app.get("/")
    def root():
        return {"service": "double-entry-ledger", "version": "0.1.0"}

    @app.post("/accounts", status_code=201)
    def create_account(body: AccountIn):
        account = service.create_account(
            code=body.code, name=body.name, subject_type=_parse_subject_type(body.subject_type)
        )
        return {
            "id": account.id,
            "code": account.code,
            "name": account.name,
            "subject_type": account.subject_type.value,
            "normal_direction": account.normal_direction.value,
            "status": account.status.value,
            "created_at": account.created_at,
        }

    @app.get("/accounts")
    def list_accounts():
        return [
            {
                "id": a.id,
                "code": a.code,
                "name": a.name,
                "subject_type": a.subject_type.value,
                "normal_direction": a.normal_direction.value,
                "status": a.status.value,
                "created_at": a.created_at,
            }
            for a in service.list_accounts()
        ]

    @app.get("/accounts/{account_id}")
    def get_account(account_id: int):
        a = service.get_account(account_id)
        return {
            "id": a.id,
            "code": a.code,
            "name": a.name,
            "subject_type": a.subject_type.value,
            "normal_direction": a.normal_direction.value,
            "status": a.status.value,
            "created_at": a.created_at,
        }

    @app.post("/transactions", status_code=201)
    def post_transaction(body: TransactionIn):
        entries = [
            (e.account_id, _parse_direction(e.direction), _to_cents(e.amount)) for e in body.entries
        ]
        tx = service.post_transaction(
            idempotency_key=body.idempotency_key, description=body.description, entries=entries
        )
        return _tx_to_dict(tx)

    @app.get("/transactions")
    def list_transactions(
        account_id: Optional[int] = None,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        status: Optional[str] = None,
    ):
        txs = service.list_transactions(
            account_id=account_id,
            start_time=start_time,
            end_time=end_time,
            status=_parse_status(status),
        )
        return [_tx_to_dict(t) for t in txs]

    @app.get("/transactions/{idempotency_key}")
    def get_transaction(idempotency_key: str):
        return _tx_to_dict(service.get_transaction(idempotency_key))

    @app.post("/transactions/{idempotency_key}/reverse", status_code=201)
    def reverse_transaction(idempotency_key: str, body: ReverseIn):
        tx = service.reverse_transaction(
            original_key=idempotency_key,
            reversal_key=body.reversal_key,
            description=body.description,
        )
        return _tx_to_dict(tx)

    @app.get("/accounts/{account_id}/balance")
    def get_balance(account_id: int):
        balance = service.get_balance(account_id)
        return {"account_id": balance.account_id, "balance_cents": balance.balance}

    @app.get("/accounts/{account_id}/flow")
    def list_flow(
        account_id: int,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
    ):
        return service.list_flow(
            account_id=account_id, start_time=start_time, end_time=end_time
        )

    @app.post("/recover")
    def recover():
        return service.recover()

    return app
