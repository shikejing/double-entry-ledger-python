# 复式记账账本服务（double-entry-ledger）

一套用 Python + FastAPI + SQLite 实现的复式记账（Double-Entry Bookkeeping）账本服务，
覆盖 **建科目 → 记账 → 幂等提交 → 并发记账 → 冲正 → 余额/流水查询 → 崩溃重启恢复**
的完整链路，并通过自动化测试保证账务数据一致性。

- 金额一律使用 `Decimal`（传输为字符串），按"分"（`0.01`）规范化，杜绝浮点误差。
- 持久化基于单文件 SQLite：WAL 模式、写事务 `BEGIN IMMEDIATE` 串行化、
  MVCC 快照读；不依赖外部数据库即可获得完整 ACID 保证。

---

## 1. 账务模型

### 1.1 科目（Account）

| 字段 | 说明 |
| --- | --- |
| `code` | 科目代码，主键，非空唯一 |
| `name` | 科目名称 |
| `account_type` | 科目类型：`asset` 资产 / `liability` 负债 / `equity` 权益 / `income` 收入 / `expense` 费用 |
| `created_at` | 创建时间（UTC ISO8601） |

科目类型决定其**正常余额方向**：

| 类型 | 正常余额方向 |
| --- | --- |
| asset（资产）、expense（费用） | 借方 debit |
| liability（负债）、equity（权益）、income（收入） | 贷方 credit |

### 1.2 业务与分录（Transaction / Entry）

- **业务（Transaction）** 是一次原子记账单元，由调用方给出唯一的 `business_id`。
- **分录（Entry）** 隶属于业务，每条分录含 `account_code`、`direction`（debit/credit）、
  `amount`（正数），并按提交顺序带 `sequence`。
- 业务头与全部分录在**同一个数据库事务**内写入，要么全部成功，要么全部失败，
  物理上不存在"只写了一半"的业务。

### 1.3 业务状态

| 状态 | 含义 |
| --- | --- |
| `posted` | 已提交，参与余额 |
| `reversed` | 已被冲正；原始记录保留，仅状态迁移，金额/方向/时间永不修改或删除 |

冲正业务自身状态为 `posted`，但通过 `reversal_of_id` 指向被冲正的原业务。

### 1.4 冲正模型（重要语义）

冲正不是删除或修改原始单据，而是**新增一笔方向完全相反的红冲业务**：

1. 冲正分录 = 原业务分录逐条反向（借↔贷），金额逐分相等；
2. 原业务整体置为 `reversed`（业务头与每条分录的状态列同时迁移）；
3. 冲正业务以 `reversal_of_id` 引用原业务，形成双向可追溯链；
4. **有效集**定义为 `status = posted 且 reversal_of_id IS NULL`：
   - 原业务（reversed）与其冲正业务（posted 但 `reversal_of_id` 非空）**成对排除**；
   - 因此有效余额严格停留在冲正发生前的状态之外，净影响为 0；
   - `include_reversed=true` 时可查见双方全部流水（审计视角），此时两者净额相抵，
     但借贷双方的累计发生额会如实包含整段历史。
5. 已冲正业务不可再次冲正；冲正业务本身不可被二次冲正（不允许级联红冲）。

### 1.5 余额表达

每个科目的余额同时给出四种口径：

```
debit_total  借方累计发生额
credit_total 贷方累计发生额
net_debit    = debit_total - credit_total（借方净额，可能为负）
balance      按科目正常方向取符号后的余额
```

- 资产/费用：`balance = net_debit`
- 负债/权益/收入：`balance = -net_debit`

余额不维护计数器，而是对已提交分录实时 `SUM`（事件溯源）：
任何一次余额读取都对应某个**已完整提交的账目状态**，不存在"读改写"式丢失更新。

---

## 2. 一致性与并发机制

| 需求 | 机制 |
| --- | --- |
| 原子性（多条分录整体成败） | 业务头 + 全部分录在同一 `BEGIN IMMEDIATE` 事务写入 |
| 幂等提交 | `transactions.business_id` UNIQUE；事务内二次查重，重复提交返回先前结果（`idempotent_hit=true`），不重复入账 |
| 同号不同单防护 | 相同 `business_id` 但分录内容不同时返回 `409 DuplicateTransactionError` |
| 并发无丢失更新 | WAL 下写事务全局串行化；余额由流水聚合，无余额计数器竞争 |
| 读一致性 | 每次查询（含余额、对账单）在单个 MVCC 快照事务内完成，`余额 ≡ 同快照流水中借贷净额` |
| 冲正一致性 | 原业务状态迁移 + 冲正单据写入同事务；事务内复查消除并发重复冲正竞态 |
| 崩溃/重启恢复 | SQLite WAL + 回滚日志：未 COMMIT 的事务在下次打开时自动回滚，已 COMMIT 的全部保留 |

`LedgerRepository` 默认 `busy_timeout=5000ms`，并发写者在锁上排队而不是立即失败。

---

## 3. 校验规则（非法分录会被拒绝并给出可解释错误）

- 业务必须至少含一条分录；
- 每条分录：科目代码非空、科目必须已存在、方向必须是 `debit`/`credit`、
  金额必须是有限正数（`> 0`），统一规范化到分；
- 一笔业务**必须同时存在借方与贷方**，且借方合计 = 贷方合计，否则
  `UnbalancedEntryError`（错误信息中给出双方合计金额）；
- 科目代码/名称非空，科目类型必须是五类之一；科目代码重复返回 `409`；
- 科目类型与借/贷方向通过"正常余额方向"和发生额共同表达，余额按类型取符号。

错误均为 `ledger.errors.LedgerError` 的子类，HTTP 层映射为
`400`（校验类）/`404`（不存在）/`409`（冲突）/`422`（请求体格式），
响应体形如：

```json
{ "error": "UnbalancedEntryError", "message": "借贷不平衡：借方合计 10.00 != 贷方合计 9.99" }
```

---

## 4. HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/accounts` | 创建科目 |
| GET  | `/accounts` | 列出全部科目 |
| POST | `/transactions` | 原子记账（按 `business_id` 幂等） |
| POST | `/transactions/{business_id}/reverse` | 冲正业务 |
| GET  | `/transactions/{business_id}` | 查询业务头 + 全部分录（含状态） |
| GET  | `/entries` | 流水查询：`account_code`、`start`、`end`、`include_reversed` |
| GET  | `/balances/{account_code}` | 科目余额，可选 `include_reversed` |
| GET  | `/accounts/{account_code}/statement` | 同一快照下的余额 + 明细对账单 |

金额字段在 JSON 中均为字符串（如 `"100.00"`）。

### 记账请求示例

```json
{
  "business_id": "sale-20261002-001",
  "description": "销售收款",
  "entries": [
    {"account_code": "1001", "direction": "debit",  "amount": "100.00"},
    {"account_code": "4001", "direction": "credit", "amount": "100.00"}
  ]
}
```

响应：

```json
{
  "idempotent_hit": false,
  "transaction": { "id": 1, "business_id": "sale-20261002-001", "status": "posted", "...": "..." },
  "entries": [ {"account_code": "1001", "direction": "debit", "amount": "100.00", "status": "posted"} ]
}
```

使用同一 `business_id` 与同样的分录再次提交：HTTP 201，`idempotent_hit=true`，
返回的是**先前那笔业务**，余额不发生第二次变化。

### 冲正请求示例

```json
{ "reversal_business_id": "sale-20261002-001-void", "description": "红冲" }
```

---

## 5. 运行

```bash
uv sync
# 启动 HTTP 服务（数据库路径可用 LEDGER_DB 覆盖，默认 ./ledger.db）
uv run python main.py
# 交互式文档
# http://127.0.0.1:8000/docs
```

也可直接在代码中使用：

```python
from ledger.repository import LedgerRepository
from ledger.service import LedgerService
from ledger.models import AccountType, Direction, EntryInput
from decimal import Decimal

svc = LedgerService(LedgerRepository("ledger.db"))
svc.create_account("1001", "现金", AccountType.ASSET)
svc.create_account("4001", "主营业务收入", AccountType.INCOME)
svc.post_transaction("biz-1", [
    EntryInput("1001", Direction.DEBIT, Decimal("100.00")),
    EntryInput("4001", Direction.CREDIT, Decimal("100.00")),
])
print(svc.get_balance("1001"))
```

"重启恢复"即重新打开同一个数据库文件：SQLite 会依据 WAL/回滚日志自动把
崩溃时未完成的事务回滚、已提交事务保留，账本严格停在最后一次成功提交点，
不会出现悬空或重复的分录。

---

## 6. 测试

```bash
uv run pytest
```

共 60 个用例，覆盖：

| 文件 | 覆盖点 |
| --- | --- |
| `tests/test_accounts.py` | 科目创建校验、借/贷方向、科目类型、金额、借贷平衡等非法分录拒绝 |
| `tests/test_posting.py` | 原子记账、五类科目余额方向、复合分录、失败不留痕、时间范围过滤 |
| `tests/test_idempotency.py` | 重复提交只入账一次、同号不同单拒绝、冲正后幂等语义、标识冲突 |
| `tests/test_concurrency.py` | 8 线程并发记账无丢失更新、同标识并发仅一次成功、并发冲正仅一次成功、读一致性 |
| `tests/test_reversal.py` | 反向分录、余额抵消、原始记录保留、双向追溯、禁止重复/级联冲正 |
| `tests/test_queries.py` | posted/reversed 状态区分、科目/时间过滤、含冲正口径、对账单自洽 |
| `tests/test_recovery.py` | 重启后状态一致、失败回滚、崩溃中途无悬空行、幂等跨重启、WAL 恢复 |
| `tests/test_api.py` | FastAPI 端到端生命周期、错误状态码、过滤、对账单 |

---

## 7. 代码结构

```
ledger/
  errors.py       # 全部可解释错误类型
  models.py       # 枚举与不可变领域模型（Account/Entry/Transaction/Balance/PostResult）
  repository.py   # SQLite 持久化：建表、WAL、显式事务/快照、原子写入、聚合查询
  service.py      # 领域服务：校验、原子记账、幂等、冲正、查询
  api.py          # FastAPI 接口与序列化、错误 -> HTTP 状态码映射
main.py           # 组装入口（LEDGER_DB 指定数据库文件）
tests/            # 自动化测试
```
