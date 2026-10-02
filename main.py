"""应用入口：创建账本服务并启动 HTTP 服务。

环境变量：
- LEDGER_DB: SQLite 数据库文件路径，缺省使用内存数据库。
"""

from __future__ import annotations

import os

import uvicorn

from ledger.api import create_app
from ledger.db import connect
from ledger.service import LedgerService


def build_service(db_path: str | None = None) -> LedgerService:
    conn = connect(db_path)
    service = LedgerService(conn)
    # 启动即做一次一致性校验，确保账本与最后一次已提交状态一致
    service.recover()
    return service


service = build_service(os.environ.get("LEDGER_DB"))
app = create_app(service)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8000")))
