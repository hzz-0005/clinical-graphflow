# InsightFlow Clinical V20 Temporal Operations（Temporal 运行与审批恢复）

## 1. 这一版真正完成了什么

V19 只有 Temporal 的类型边界和 Workflow/Worker 外壳。V20 把它变成一条可以部署的可选运行路径：

```text
HTTP 请求
  → 校验身份、试验、发布批次和数据域
  → Temporal Workflow（只负责协调）
  → execute_clinical_investigation_activity
  → 现有 V17 Graph + MCP + PostgreSQL 只读查询
  → PostgreSQL 保存调查、证据和审批
  → Workflow 等待带签名令牌的人工审批信号
```

Workflow 不接收患者行、SQL 结果或 API 密钥。Activity 的返回值只有工作流编号、调查编号、状态和审批编号；查看证据必须重新走受权限保护的调查查询接口。

## 2. 新增组件

### `temporal_activity.py`

`ClinicalTemporalActivities` 是 Temporal 与现有运行时之间的唯一执行适配器：

- 校验 `TemporalWorkflowInput`；
- 按 `requested_by` 解析当前身份；
- 复用 `/api/v8/clinical/investigations` 使用的 `execute_dynamic`；
- 复用原有 `persist_dynamic` 写入 PostgreSQL；
- 把 Temporal workflow id 与调查 id 对齐，便于审计和恢复；
- 不把证据行放进 Temporal history。

### `temporal_api.py`

V20 新增独立、默认关闭的控制接口：

```text
POST /api/v20/clinical/workflows
POST /api/v20/clinical/approvals/{approval_id}/resume-token
POST /api/v20/clinical/workflows/{workflow_id}/approval
```

V21 在同一前缀下补充 `POST /api/v20/clinical/workflows/{workflow_id}/cancel` 与
`POST /api/v20/clinical/outbox/dispatch`，详见 [`docs/V21_DURABLE_OPERATIONS.md`](V21_DURABLE_OPERATIONS.md)。

第一条接口只启动工作流，不在 HTTP 请求线程执行临床查询。它仍会在启动前检查登录身份、试验访问范围、已发布批次和可用数据域。

恢复审批的顺序是：

1. 管理员为待审批记录申请短期恢复令牌；
2. 令牌绑定 `approval_id + investigation_id + version + expiry`；
3. 管理员提交通过/拒绝、版本、评论和令牌；
4. API 先验证签名和乐观锁版本，再向对应 Workflow 发送 `approval_decision`；
5. V20 的原始路径在信号发送成功后写入审批状态；V21 已改为先把审批状态和信号发件箱放进同一数据库事务，再投递信号，避免 Temporal 暂时不可达时丢失恢复机会。

令牌不是登录凭证，也不是临床结论。密钥只来自环境变量，不能写入数据库、Redis 或 Git。

### `temporal_worker.py`

这是独立 Worker 入口。它连接部署者提供的 Temporal 集群，注册：

- `InsightFlowClinicalWorkflow`；
- `execute_clinical_investigation_activity`。

没有安装 `temporalio`、没有开启配置或没有集群时，Worker 会明确失败，不会伪装成已启用的持久化运行时。

## 3. 配置

默认仍保持同步/内存端口，但运行时使用 Graph：

```dotenv
INSIGHTFLOW_RUNTIME_VERSION=v17
INSIGHTFLOW_RUNTIME_PORTS=memory
INSIGHTFLOW_TEMPORAL_ENABLED=false
```

部署 V20 Temporal 路径时：

```dotenv
INSIGHTFLOW_RUNTIME_VERSION=v17
INSIGHTFLOW_RUNTIME_PORTS=redis
INSIGHTFLOW_TEMPORAL_ENABLED=true
INSIGHTFLOW_TEMPORAL_TARGET=temporal.example.internal:7233
INSIGHTFLOW_TEMPORAL_NAMESPACE=default
INSIGHTFLOW_TEMPORAL_TASK_QUEUE=clinical-v20
INSIGHTFLOW_APPROVAL_RESUME_SECRET=请替换为随机长密钥
INSIGHTFLOW_APPROVAL_RESUME_TTL_SECONDS=900
```

Temporal 入口固定使用 v17；仅同步/动态 Worker 可在有 owner/expiry 的应急窗口显式回退 v16。

安装可选依赖：

```powershell
pip install ".\\backend[durable]"
```

启动可选 Worker 镜像：

```powershell
docker compose --profile temporal build clinical-temporal-worker
docker compose --profile temporal up -d clinical-temporal-worker
```

Compose 只提供 Worker 容器，不自动捆绑 Temporal 集群；集群可以由独立基础设施提供。Redis 仍只保存缓存、检查点、事件和幂等标记，不保存临床事实。

## 4. 手工验收流程

### 启动工作流

```powershell
$headers = @{ "X-InsightFlow-User" = "admin" }
$body = @{
  question = "比较试验中的严重不良事件月份分布"
  trial_id = "TRIAL-CF-101"
  provider = "deepseek"
} | ConvertTo-Json

Invoke-RestMethod `
  -Method Post `
  -Uri http://localhost:18000/api/v20/clinical/workflows `
  -Headers $headers `
  -ContentType "application/json" `
  -Body $body
```

成功返回 `202` 和 `workflow_id`。如果 Temporal 未开启，返回中文 `temporal_disabled`；如果集群不可达，返回 `temporal_unavailable`。

### 审批恢复

先申请令牌：

```powershell
$token = Invoke-RestMethod `
  -Method Post `
  -Uri http://localhost:18000/api/v20/clinical/approvals/{approval_id}/resume-token `
  -Headers $headers
```

再提交审批：

```powershell
$decision = @{
  approval_id = $token.approval_id
  version = $token.version
  approved = $true
  comment = "已复核证据链"
  resume_token = $token.resume_token
} | ConvertTo-Json

Invoke-RestMethod `
  -Method Post `
  -Uri http://localhost:18000/api/v20/clinical/workflows/{workflow_id}/approval `
  -Headers $headers `
  -ContentType "application/json" `
  -Body $decision
```

令牌过期、审批版本变化、工作流编号不匹配或签名错误都会被拒绝；不能靠重复点击绕过乐观锁。

## 5. 测试

不需要 Temporal 集群即可运行契约测试：

```powershell
$env:PYTHONPATH=".;backend"
python -m pytest backend/tests/test_v20_temporal_integration.py `
  backend/tests/test_v19_enterprise_runtime.py `
  backend/tests/test_v18_durable_runtime.py -q
```

测试覆盖：

- Workflow 输入只携带受治理字段；
- Activity 使用 Workflow ID 保存调查，但返回值不包含证据行；
- 未知请求人被拒绝；
- Temporal 未显式开启时启动接口不执行查询；
- 令牌签发、版本绑定、审批信号和发布顺序；
- Redis、V17 Graph、V19 追踪和 Worker 边界回归。

## 6. 当前边界

V20 已经把代码路径接通，但是否“正在使用 Temporal”仍由部署配置决定。V21 已补上事务性 outbox（事务发件箱）、信号重试、重复信号幂等、取消和审批超时；真实集群重启、长流程和运维调度验收继续记录在 [`docs/V21_DURABLE_OPERATIONS.md`](V21_DURABLE_OPERATIONS.md)。

