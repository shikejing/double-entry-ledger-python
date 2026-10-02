"""账本服务错误类型定义。"""


class LedgerError(Exception):
    """所有账本错误的基类。"""


class AccountNotFoundError(LedgerError):
    """引用了不存在的科目。"""


class DuplicateAccountError(LedgerError):
    """科目代码重复。"""


class DuplicateTransactionError(LedgerError):
    """业务标识已被另一笔业务占用。"""

    def __init__(self, business_id: str, existing_business_id: str):
        super().__init__(
            f"业务标识 {business_id!r} 已存在"
            + ("" if existing_business_id == business_id else f"（冲突标识：{existing_business_id!r}）")
        )
        self.business_id = business_id
        self.existing_business_id = existing_business_id


class UnbalancedEntryError(LedgerError):
    """借贷金额不平衡。"""


class InvalidEntryError(LedgerError):
    """分录非法（方向/金额/科目类型不一致等）。"""


class EmptyTransactionError(LedgerError):
    """业务不包含任何分录。"""


class TransactionNotFoundError(LedgerError):
    """业务不存在。"""


class AlreadyReversedError(LedgerError):
    """业务已被冲正，不可再次冲正。"""


class ReverseOriginalError(LedgerError):
    """冲正目标本身是冲正业务，不允许级联冲正。"""


class ReversalMismatchError(LedgerError):
    """冲正分录与原始分录不一致。"""
