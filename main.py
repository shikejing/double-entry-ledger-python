"""服务入口：组装持久化层、领域服务与 HTTP 应用。

数据库路径可通过环境变量 LEDGER_DB 指定（默认 ./ledger.db）。
重新打开同一文件即完成"重启恢复"：SQLite 依据 WAL/回滚日志自动把未完成
事务回滚、已提交事务保留，账本状态严格停在最后一次成功提交点。
"""

from __future__ import annotations

import os

import uvicorn

from ledger.api import create_app
from ledger.repository import LedgerRepository
from ledger.service import LedgerService

DB_PATH = os.environ.get("LEDGER_DB", "ledger.db")

repository = LedgerRepository(DB_PATH)
service = LedgerService(repository)
app = create_app(service)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8000")))
