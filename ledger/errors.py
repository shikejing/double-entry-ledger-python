"""账本领域异常定义。"""


class LedgerError(Exception):
    """所有账本业务异常的基类。"""


class AccountNotFoundError(LedgerError):
    """账户不存在。"""


class AccountClosedError(LedgerError):
    """账户已停用，不能再记账。"""


class AccountExistsError(LedgerError):
    """账户代码已存在。"""


class InvalidDirectionError(LedgerError):
    """借贷方向非法。"""


class InvalidAmountError(LedgerError):
    """金额非法（非正数或小数位过多）。"""


class UnbalancedTransactionError(LedgerError):
    """借贷不平：借方合计与贷方合计不相等。"""


class InsufficientEntriesError(LedgerError):
    """分录条数不足，一笔业务至少包含两条分录。"""


class BalanceViolationError(LedgerError):
    """余额与科目正常方向不一致（出现反向透支余额）。"""


class TransactionNotFoundError(LedgerError):
    """交易不存在。"""


class AlreadyReversedError(LedgerError):
    """交易已被冲正，不能重复冲正。"""


class ReversalError(LedgerError):
    """冲正业务非法（如冲正交易自身不合法）。"""


class IdempotencyKeyConflictError(LedgerError):
    """幂等键冲突：同一业务标识对应了不同的业务内容。"""
