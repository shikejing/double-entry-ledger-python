# 复式记账账本服务（Double-Entry Ledger）

一套覆盖「记账 → 对账」完整链路的复式记账账本服务，保证账务数据的一致性。基于 FastAPI + SQLite 实现，无外部服务依赖，便于部署与测试。

## 一、账务模型

### 1. 核心概念

- **账户（Account）**：即会计科目，如「银行存款」「短期借款」。每个账户有唯一代码、名称、科目类型与正常余额方向。
- **科目类型（SubjectType）**：资产 `asset`、负债 `liability`、权益 `equity`、收入 `revenue`、费用 `expense`。
- **记账方向（Direction）**：借 `debit`、贷 `credit`。
- **交易（Transaction）**：一笔业务（凭证），通过幂等键 `idempotency_key` 唯一标识，包含多条分录。
- **分录（Entry）**：一条 `(账户, 方向, 金额)` 记录。分录采用**追加式日志（append-only journal）**，是账务数据的唯一事实来源；余额由分录实时聚合得到。

### 2. 正常余额方向

科目类型决定余额的正常方向，系统据此校验「借贷方向、科目类型与金额」三者一致：

| 科目类型 | 正常方向 | 余额含义 |
| --- | --- | --- |
| 资产 asset | 借 debit | 借方余额为正 |
| 费用 expense | 借 debit | 借方余额为正 |
| 负债 liability | 贷 credit | 贷方余额为正 |
| 权益 equity | 贷 credit | 贷方余额为正 |
| 收入 revenue | 贷 credit | 贷方余额为正 |

记账后余额不得出现与正常方向相反的透支余额（例如资产类账户不能出现贷方余额），否则该笔分录被拒绝。

### 3. 金额表示

金额在数据库与服务层一律以**最小货币单位（分）的整数**存储与计算，避免浮点误差。HTTP 接口层接收十进制金额（元，最多两位小数），由服务转换为分。

### 4. 状态模型

- 交易状态：`posted`（已提交/已入账）、`reversed`（已冲正）。
- 账户状态：`active`（正常）、`closed`（已停用，停用后不能再记账）。

## 二、一致性保证

| 需求 | 实现方式 |
| --- | --- |
| ① 原子性 | 一笔交易的所有分录与校验在**同一个 SQLite 事务**内完成，整体成功或整体失败，不存在只写入一半的情况。 |
| ② 幂等提交 | `idempotency_key` 上有唯一约束。重复提交返回先前结果，不重复入账；并发同键提交由数据库唯一约束串行，只产生一笔交易。 |
| ③ 并发提交 | 写事务使用 `BEGIN IMMEDIATE` 立即获取写锁，配合 `busy_timeout` 使并发写串行化；分录追加写入、余额聚合得到，**从根本上避免丢失更新**。任意时刻读取到的余额都对应某个已提交的完整账目状态。 |
| ④ 冲正 | 已入账交易只能通过一笔方向相反的**新交易**抵消（`reversal_of` 指向原交易），原交易状态置为 `reversed`；原分录追加保留、不可修改或删除，两笔交易状态可追溯。 |
| ⑤ 余额与流水查询 | 余额由分录聚合；流水分录级展示并区分 `posted`/`reversed` 状态，支持按账户与时间范围过滤。 |
| ⑥ 创建校验 | 账户与科目创建时校验科目类型、方向、金额一致性；非法分录返回可解释错误（见错误说明）。 |
| ⑦ 重启恢复 | SQLite 已提交事务持久化；服务启动即执行 `recover()` 一致性校验（借贷平衡、无孤儿分录、冲正链接一致），确保账本与最后一次已提交状态一致。 |

## 三、接口语义

服务启动后默认监听 `127.0.0.1:8000`（可通过 `PORT` 环境变量修改），数据库路径由 `LEDGER_DB` 环境变量指定（缺省为内存数据库）。

### 账户

- `POST /accounts`：创建账户。字段 `code`、`name`、`subject_type`。正常方向由科目类型自动决定。
- `GET /accounts`：列出全部账户。
- `GET /accounts/{account_id}`：查询单个账户。

### 记账

- `POST /transactions`：提交一笔交易。
  - 字段：`idempotency_key`（幂等键）、`description`（可选）、`entries`（至少两条）。
  - 每条分录：`account_id`、`direction`（debit/credit）、`amount`（十进制正数，最多两位小数）。
  - 借贷必须相等，账户必须存在且为 `active`，记账后不得出现反向透支余额。
  - **幂等**：同一 `idempotency_key` 重复提交返回先前结果。
- `GET /transactions`：查询交易列表，支持 `account_id`、`start_time`、`end_time`、`status` 过滤。
- `GET /transactions/{idempotency_key}`：按幂等键查询单笔交易（含分录）。

### 冲正

- `POST /transactions/{idempotency_key}/reverse`：冲正一笔已入账交易。
  - 字段：`reversal_key`（冲正交易的幂等键）、`description`（可选）。
  - 冲正生成一笔方向相反的新交易，原交易状态置为 `reversed`，原分录保留。
  - **幂等**：重复冲正返回同一冲正结果。

### 余额与流水

- `GET /accounts/{account_id}/balance`：查询账户当前余额（分）。
- `GET /accounts/{account_id}/flow`：查询账户流水（分录级），每条记录含 `transaction_status`（posted/reversed），支持 `start_time`、`end_time` 过滤。

### 恢复

- `POST /recover`：执行一致性校验并返回报告。

## 四、错误说明

所有业务错误返回 HTTP 400，`detail` 字段包含可解释的错误信息：

| 错误 | 含义 |
| --- | --- |
| `AccountNotFoundError` | 账户不存在 |
| `AccountClosedError` | 账户已停用，不能记账 |
| `AccountExistsError` | 科目代码已存在 |
| `InvalidDirectionError` | 记账方向非法（非 debit/credit） |
| `InvalidAmountError` | 金额非法（非正数、非整数分、小数位过多） |
| `UnbalancedTransactionError` | 借贷不平，借方合计与贷方合计不相等 |
| `InsufficientEntriesError` | 分录条数不足（一笔交易至少两条） |
| `BalanceViolationError` | 记账后余额与科目正常方向相反（反向透支） |
| `TransactionNotFoundError` | 交易不存在 |
| `AlreadyReversedError` | 交易已冲正，不能重复冲正 |

## 五、本地运行与测试

```bash
# 安装依赖
uv sync

# 运行测试
uv run pytest

# 启动服务（文件数据库，重启后数据保留）
LEDGER_DB=./ledger.db PORT=8000 uv run python main.py
```

## 六、目录结构

```
ledger/
  __init__.py
  errors.py        # 领域异常
  models.py        # 账务模型：枚举与数据类
  db.py            # SQLite 连接、Schema、手动事务
  repository.py    # 数据访问层
  service.py       # 核心业务逻辑（记账、冲正、查询、恢复）
  api.py           # HTTP 接口层
tests/
  test_ledger.py   # 服务层测试（幂等、并发、冲正、恢复、校验）
  test_api.py      # HTTP 端到端测试
main.py            # 应用入口
```
