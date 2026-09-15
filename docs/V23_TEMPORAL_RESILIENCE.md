# InsightFlow Clinical V23 Temporal Resilience（长流程可恢复性）

## 1. 这一版解决什么

V22 已经能把调查交给真实 Temporal Server（长流程服务）。V23 解决的是“长流程跑到一半时，如何知道它在哪、如何避免重复执行”：

```text
工作流启动
  → Activity（实际调查）执行
  → PostgreSQL 已提交但返回暂时丢失
  → Temporal 重试 Activity
  → 发现同一个 investigation_id 已存在
  → 直接复用已保存结果，不再次调用 LLM、不重复写证据
```

同时，前端或运维程序可以通过状态接口查询编排进度。这个接口只返回工作流元数据，不返回问题、SQL、患者行或证据内容；完整调查结果仍从受治理的调查接口读取。

## 2. 新增的能力

### 2.1 Durable status query（耐久状态查询）

`TemporalClinicalWorkflowBoundary.query_status()` 优先调用 Workflow Query（工作流查询）`status`。如果工作流已经结束或查询被服务端拒绝，则回退到 Temporal Execution Description（执行描述）。返回字段包括：

- `workflow_id`：工作流编号；
- `status`：`running`、`pending_approval`、`approved`、`rejected`、`cancelled` 或 Temporal 的结束状态；
- `clinical_status`：Activity 产生的临床调查状态；
- `publication_status`：调查发布状态；
- `approval_id`：待审批记录编号（如果存在）；
- `source`：状态来自 `query` 还是 `describe`。

HTTP 入口：

```text
GET /api/v20/clinical/workflows/{workflow_id}/status
```

调用者必须具备调查读取权限。非管理员还必须能从企业调查仓库读取该调查，避免通过工作流编号探测其他人的任务。

### 2.2 Activity retry idempotency（活动重试幂等）

Temporal 的 Activity 允许因网络超时、Worker 重启等原因重试。V23 在 Activity 开始执行前，先用当前身份查询 `investigation_id`：

- 已存在且可见：只组装小型 `TemporalActivityResult`；
- 不存在：才调用 Graph、执行只读查询并写入 PostgreSQL。

这样即使第一次尝试已经提交证据，第二次尝试也不会再次调用 DeepSeek 或插入同一个 UUID。证据的唯一事实源仍然是 PostgreSQL。

### 2.3 Retry policy（重试策略）边界

网络或服务暂时不可达仍然允许 Temporal 重试；请求身份非法等 `TemporalActivityError` 被列为不可重试错误。重复重试无法修复的输入错误不会浪费三轮调查预算。

## 3. 如何验收

### 3.1 代码级测试

```powershell
$env:PYTHONPATH=".;backend"
python -m pytest `
  backend/tests/test_v23_temporal_resilience.py `
  backend/tests/test_v22_temporal_operations.py `
  backend/tests/test_v21_temporal_operations.py `
  backend/tests/test_v20_temporal_integration.py -ra --tb=short
```

V23 的核心检查是：

1. 状态查询只返回元数据；
2. 已持久化调查的 Activity 重试不会再次执行 LLM；
3. 状态接口需要读取权限；
4. V20–V22 的启动、取消、发件箱合同继续通过。

### 3.2 真实 Temporal 状态查询

启动 V22 的 Temporal profile 后，开启 API 的 Temporal 控制面：

```powershell
$env:INSIGHTFLOW_TEMPORAL_ENABLED="true"
$env:INSIGHTFLOW_RUNTIME_VERSION="v17"
$env:INSIGHTFLOW_RUNTIME_PORTS="redis"
$env:INSIGHTFLOW_TEMPORAL_TARGET="temporal:7233"
$env:INSIGHTFLOW_TEMPORAL_TASK_QUEUE="clinical-v20"
docker compose up -d backend
```

查询一个已有工作流：

```powershell
Invoke-RestMethod `
  -Uri http://localhost:18000/api/v20/clinical/workflows/<workflow_id>/status `
  -Headers @{ "X-InsightFlow-User" = "admin" }
```

返回示例：

```json
{
  "workflow_id": "…",
  "status": "cancelled",
  "clinical_status": "pending_approval",
  "publication_status": "pending_approval",
  "approval_id": "…",
  "source": "query"
}
```

`question`、`evidence`、`rows` 不会出现在这个响应里。需要查看完整证据链时，使用原有调查读取接口，并继续经过企业权限和查看者脱敏规则。

验收完毕后恢复默认同步 API：

```powershell
Remove-Item Env:INSIGHTFLOW_TEMPORAL_ENABLED,Env:INSIGHTFLOW_RUNTIME_VERSION,Env:INSIGHTFLOW_RUNTIME_PORTS,Env:INSIGHTFLOW_TEMPORAL_TARGET,Env:INSIGHTFLOW_TEMPORAL_TASK_QUEUE -ErrorAction SilentlyContinue
docker compose up -d backend
```

## 4. 本版没有改变的边界

- Temporal 仍只负责长流程、重试、等待审批和取消，不保存临床事实；
- LLM 仍不能生成自由 SQL，工具和字段继续由 Registry、PlanValidator 与 SQL 安全层控制；
- 默认 `INSIGHTFLOW_TEMPORAL_ENABLED=false`，V16/V17 同步接口不需要 Temporal；
- V23 还没有宣称 DeepSeek 长流程准确率或生产故障恢复已经完成，那属于下一版的实机评测；
- 不会因为工作流状态查询而放宽数据域、试验范围或最小样本抑制。

## 5. 后续阶段

V24 的 DeepSeek 陌生问题实机评测已经作为本地验收基线完成。当前框架继续进入 V25：Graph 在每个节点和工具结果后保存检查点，事件流经过脱敏，并允许从含有已完成任务的状态恢复而不重复调用模型和工具。详见 [`V25_RUNTIME_RESUME_REPLAY.md`](V25_RUNTIME_RESUME_REPLAY.md)。

