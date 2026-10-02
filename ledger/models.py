"""账本领域模型与枚举（纯数据定义，不含行为）。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum


class AccountType(str, Enum):
    """会计科目类型。"""

    ASSET = "asset"           # 资产
    LIABILITY = "liability"   # 负债
    EQUITY = "equity"         # 所有者权益
    INCOME = "income"         # 收入
    EXPENSE = "expense"       # 费用


class Direction(str, Enum):
    """分录方向。"""

    DEBIT = "debit"    # 借
    CREDIT = "credit"  # 贷


class TransactionStatus(str, Enum):
    """业务（一组分录的原子记账单元）状态。"""

    POSTED = "posted"      # 已提交（有效）
    REVERSED = "reversed"  # 已被冲正（原始记录保留，余额不再受其影响）


# 各科目类型的正常余额方向：资产/费用为借方，负债/权益/收入为贷方
NORMAL_BALANCE_SIDE: dict[AccountType, Direction] = {
    AccountType.ASSET: Direction.DEBIT,
    AccountType.EXPENSE: Direction.DEBIT,
    AccountType.LIABILITY: Direction.CREDIT,
    AccountType.EQUITY: Direction.CREDIT,
    AccountType.INCOME: Direction.CREDIT,
}


@dataclass(frozen=True)
class Account:
    code: str
    name: str
    account_type: AccountType
    created_at: datetime


@dataclass(frozen=True)
class EntryInput:
    """提交记账时的单条分录输入。"""

    account_code: str
    direction: Direction
    amount: Decimal


@dataclass(frozen=True)
class Entry:
    """已落库的分录流水。"""

    id: int
    transaction_id: int
    account_code: str
    direction: Direction
    amount: Decimal
    sequence: int
    status: TransactionStatus  # 与所属业务状态保持一致


@dataclass(frozen=True)
class Transaction:
    """一笔业务（原子记账单元）。"""

    id: int
    business_id: str
    description: str
    status: TransactionStatus
    created_at: datetime
    reversal_of_id: int | None  # 若为冲正业务，指向被冲正业务 id


@dataclass(frozen=True)
class Balance:
    """科目余额：以借/贷两方累计额及净额表达。"""

    account_code: str
    debit_total: Decimal
    credit_total: Decimal
    net_debit: Decimal   # 借-贷（可能为负，表示实际为贷方余额）
    value: Decimal       # 按科目正常方向取符号后的余额


@dataclass(frozen=True)
class PostResult:
    """记账/幂等命中的返回结果。"""

    transaction: Transaction
    entries: tuple[Entry, ...]
    idempotent_hit: bool  # True 表示本次为重复提交，返回的是先前结果
