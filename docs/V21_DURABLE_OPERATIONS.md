# InsightFlow Clinical V21 Durable Operations（可重试的长流程运行）

## 1. 这一版解决什么

V20 已经可以把调查交给 Temporal（长流程编排平台），但审批信号送达失败时，审批记录和工作流状态可能短暂不同步。V21 补上这条生产边界：

```text
审批决定
  → 审批状态与 Temporal Signal Outbox（信号事务发件箱）同一事务写入
  → 尝试发送 approval_decision
  → 成功：标记 sent，并按需发布调查
  → 失败：保留 pending，按重试预算继续投递
  → 超过预算：标记 dead，保留错误供人工处理
```

发件箱只保存审批元数据，不保存恢复令牌、问题原文、SQL、证据行或患者记录。每次重新投递都会从部署密钥生成一个短期令牌；第一次请求带来的令牌只在内存中使用，不会落库。

## 2. 运行时行为

### 重试与幂等

- `TemporalOutboxEvent`（发件箱事件）用 `approval_id + expected_version` 防止同一个审批版本重复入队。
- 默认最多 5 次投递；每次失败记录 `attempts` 和截断后的 `last_error`。
- Workflow 收到同一个 `approval_id + version` 的重复信号时视为幂等重放；收到不同版本则拒绝，避免旧审批覆盖新审批。
- Activity 设置 30 分钟执行上限、2 小时总调度上限和 3 次指数退避重试。查询和证据仍由现有 Graph、MCP 网关与 PostgreSQL 负责。

### 取消与审批超时

- `cancel_investigation` 是受权限保护的人工取消信号。等待审批时收到取消，Workflow 返回 `cancelled`，不会伪造审批结果。
- 审批等待最多 7 天，超时返回 `approval_timeout`，保留已产生的调查证据。
- 取消不会删除数据库里的调查或审计记录；它只停止继续等待审批。

## 3. 新接口

仍然默认关闭，路径保持 `/api/v20/clinical` 以兼容 V20 客户端：

```text
POST /api/v20/clinical/workflows
POST /api/v20/clinical/approvals/{approval_id}/resume-token
POST /api/v20/clinical/workflows/{workflow_id}/approval
POST /api/v20/clinical/workflows/{workflow_id}/cancel
POST /api/v20/clinical/outbox/dispatch
```

审批接口的成功响应为 `signal_sent`。Temporal 暂时不可达时，在重试预算内返回 `202 + signal_pending`：审批决定已经写入数据库，发件箱事件可由管理员重试；超过预算则返回 `503 + signal_dead`，要求人工处理死信。重试接口只需要管理员身份，不需要重新向审批人索取令牌。

取消请求示例：

```powershell
$body = @{ reason = "撤回本次调查" } | ConvertTo-Json
Invoke-RestMethod `
  -Method Post `
  -Uri http://localhost:18000/api/v20/clinical/workflows/{workflow_id}/cancel `
  -Headers @{ "X-InsightFlow-User" = "admin" } `
  -ContentType "application/json" `
  -Body $body
```

管理员手工重试发件箱：

```powershell
$body = @{ limit = 20 } | ConvertTo-Json
Invoke-RestMethod `
  -Method Post `
  -Uri http://localhost:18000/api/v20/clinical/outbox/dispatch `
  -Headers @{ "X-InsightFlow-User" = "admin" } `
  -ContentType "application/json" `
  -Body $body
```

## 4. 数据库迁移

新增 `database/init/014_temporal_outbox.sql`，建立 `enterprise.temporal_signal_outbox` 和待处理索引。新建 PostgreSQL 卷会自动执行初始化脚本；已有卷需要由数据库管理员单独执行该文件，不能靠重启容器假定迁移已经发生。

生产 `Runtime` 使用 `PostgresTemporalSignalOutbox`。内存实现只用于测试和明确的开发模式，不应被当作跨进程可靠队列。

## 5. 配置与启动

```dotenv
INSIGHTFLOW_TEMPORAL_ENABLED=true
INSIGHTFLOW_TEMPORAL_TARGET=temporal.example.internal:7233
INSIGHTFLOW_TEMPORAL_NAMESPACE=default
INSIGHTFLOW_TEMPORAL_TASK_QUEUE=clinical-v20
INSIGHTFLOW_APPROVAL_RESUME_SECRET=请替换为随机长密钥
INSIGHTFLOW_APPROVAL_RESUME_TTL_SECONDS=900
INSIGHTFLOW_TEMPORAL_OUTBOX_MAX_ATTEMPTS=5
```

启动可选 Worker：

```powershell
docker compose --profile temporal build clinical-temporal-worker
docker compose --profile temporal up -d clinical-temporal-worker
```

V21 的发件箱重试仍保留受保护的 HTTP 运维入口；V22 已增加独立的 `clinical-temporal-outbox` 定时投递器，并提供可选的本地 Temporal 开发集群。启动方式和真实部署验收见 [`docs/V22_TEMPORAL_DEPLOYMENT.md`](V22_TEMPORAL_DEPLOYMENT.md)。

## 6. 验证范围

```powershell
$env:PYTHONPATH=".;backend"
python -m pytest backend/tests/test_v21_temporal_operations.py `
  backend/tests/test_v20_temporal_integration.py `
  backend/tests/test_v19_enterprise_runtime.py `
  backend/tests/test_v18_durable_runtime.py -q
```

测试覆盖：

- 发件箱不落 bearer token（持有者令牌）；
- 失败重试与 dead-letter（死信）边界；
- 审批状态与发件箱的原子提交接口；
- 临时 Temporal 故障返回可重试的 `202`；
- 重试后信号发送与调查发布；
- 取消信号、版本字段和边界转发；
- V18/V19/V20 既有契约不回归。

## 7. 仍未宣称完成

V21 完成了事务发件箱、重试、取消和超时的代码边界；V22 已在本机真实 Temporal Server + Worker + PostgreSQL + Redis 上验证工作流启动、Activity 执行、审批等待和取消，并把发件箱轮询进程接入 Compose；V23 又增加状态查询和 Activity 重试幂等。生产集群重启、Activity 失败恢复和 DeepSeek 长流程仍属于后续 V24 部署验收。

