"""账务数据模型：枚举与数据类。

金额一律以最小货币单位（分）的整数表示，避免浮点误差。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class SubjectType(str, Enum):
    """会计科目类型。"""

    ASSET = "asset"  # 资产类
    LIABILITY = "liability"  # 负债类
    EQUITY = "equity"  # 权益类
    REVENUE = "revenue"  # 收入类
    EXPENSE = "expense"  # 费用类


class Direction(str, Enum):
    """记账方向。"""

    DEBIT = "debit"  # 借
    CREDIT = "credit"  # 贷


class TransactionStatus(str, Enum):
    """交易状态。"""

    POSTED = "posted"  # 已提交（已入账）
    REVERSED = "reversed"  # 已冲正


class AccountStatus(str, Enum):
    """账户状态。"""

    ACTIVE = "active"  # 正常
    CLOSED = "closed"  # 已停用


# 科目类型 -> 正常余额方向
NORMAL_DIRECTION: dict[SubjectType, Direction] = {
    SubjectType.ASSET: Direction.DEBIT,
    SubjectType.EXPENSE: Direction.DEBIT,
    SubjectType.LIABILITY: Direction.CREDIT,
    SubjectType.EQUITY: Direction.CREDIT,
    SubjectType.REVENUE: Direction.CREDIT,
}


@dataclass(frozen=True)
class Account:
    """账户（会计科目）。"""

    id: int
    code: str
    name: str
    subject_type: SubjectType
    normal_direction: Direction
    status: AccountStatus
    created_at: str


@dataclass(frozen=True)
class Entry:
    """一条分录。"""

    id: int
    transaction_id: int
    account_id: int
    direction: Direction
    amount: int  # 分


@dataclass(frozen=True)
class Transaction:
    """一笔交易（凭证），包含多条分录。"""

    id: int
    idempotency_key: str
    description: Optional[str]
    status: TransactionStatus
    reversal_of: Optional[int]
    reversed_by: Optional[int]
    created_at: str
    entries: tuple[Entry, ...] = ()


@dataclass(frozen=True)
class Balance:
    """账户余额。"""

    account_id: int
    balance: int  # 分，正数表示与正常方向一致
