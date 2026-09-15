# V26 调查版本事务与并发安全

V26 把调查状态从“无条件写入”改成可重试、可检测冲突的版本快照。

## 版本语义

- `InvestigationState.state_version` 是状态快照版本；新调查从 `1` 开始。
- `EnterpriseInvestigation.version` 是 PostgreSQL 行版本，与状态快照保持一致。
- 更新必须带 `expected_version`，服务端用旧版本做 compare-and-swap；版本不一致返回 `version_conflict`，不会覆盖较新的状态。
- 发布也会递增行版本和状态版本，避免发布动作把状态回退到旧快照。

## 重试语义

`request_id` 是调查写入的幂等键。相同请求再次到达时直接返回已经提交的调查，不重新写证据或审计事件；即使重试过程重新生成了内存中的调查 UUID，审批也会重新绑定到已持久化的调查 UUID。

PostgreSQL 通过 `uq_investigations_request_id` 和行级锁处理两个 Worker 同时首次写入的竞态。证据在状态 CAS 成功后、同一事务内同步写入；同一请求不会追加第二份证据。

读取 V0-V25 的旧行时，如果 `state_json` 尚未包含 `state_version`，repository 会用持久化行版本归一化返回模型；request_id 重放仍先经过调查空间可见性检查，不会跨权限范围泄露调查。

## 调查与审批原子性

临床动态运行时优先调用 `save_investigation_and_approval`。调查行、证据、审计事件和审批请求在一个 PostgreSQL 事务中提交；任一环节失败，事务整体回滚，不留下幽灵调查或审批。每个调查只允许一个审批生命周期，避免 Worker/API 重试创建第二个审批请求。

版本冲突和幂等冲突分别映射为可读的 `version_conflict` / `idempotency_conflict`（HTTP 409）。

## 验证

离线/内存回归：

```powershell
$env:PYTHONPATH='.;backend'
python -m pytest backend/tests/test_v26_investigation_transactions.py -q
```

真实 PostgreSQL（设置 `ENTERPRISE_DATABASE_URL` 后）：

```powershell
python -m pytest backend/tests/test_v26_postgres_integration.py -q
```

集成夹具只使用 UUID 调查并在 `finally` 中删除自己创建的记录；未配置数据库时测试会跳过，不伪装成通过。

## Temporal 重启演练

在本地 Temporal profile 中用 fake provider 启动调查至 `pending_approval`，重启 `clinical-temporal-worker`，等待 workflow query 从短暂的 `describe/running` 恢复为 `pending_approval`。重启前后 PostgreSQL 均保持同一调查版本、1 条证据和 1 条审批；演练调查随后取消并清理。

## Outbox 并发领取与失败矩阵

`database/init/016_temporal_outbox_claims.sql` 为待投递事件增加短租约。PostgreSQL worker 使用
`FOR UPDATE SKIP LOCKED` 原子 claim；进程在发送中崩溃时，租约过期后可由另一 worker reclaim，旧 worker
不能再把事件标记为 sent 或发布调查。内存实现也用进程内锁保持同一语义。

审批版本不匹配是确定性失败，直接进入 `dead`，不消耗重试预算；Temporal/网络连接错误仍保持
`pending` 并按 `INSIGHTFLOW_TEMPORAL_OUTBOX_MAX_ATTEMPTS` 重试。Activity 边界 payload 校验错误包装为
`TemporalActivityError`，由 Temporal retry policy 视为不可重试。

## 脱敏运行事件与重试观测

`GET /api/v20/clinical/workflows/{workflow_id}/events` 只返回 sequence、node、event_type、时间戳和
经过二次脱敏的有限 payload。问题正文、SQL、参数、证据、结果行和异常正文不会进入该响应；调查事实仍
通过原有受保护调查接口读取。`GET /api/v4/clinical/operations` 的运行指标增加
`retries_total` 与 `retry_duration_ms_total`，并保留 `temporal_outbox:<status>` 计数。

## 迁移执行

已有 PostgreSQL volume 不会因为容器重启自动执行新 init 文件。部署前运行：

```powershell
.\scripts\apply_v26_migrations.ps1
```

脚本按 015 → 016 顺序执行，所有 `psql` 调用启用 `ON_ERROR_STOP=1`，随后验证调查版本、请求幂等和
outbox claim 列。任一语句或结构检查失败即停止，脚本不执行破坏性自动回滚；管理员应保留失败现场并
按数据库变更流程处理。

