# InsightFlow Clinical V19 Enterprise Runtime（企业化运行时）

## 1. 这一步解决什么

V19 把“能暂停、能追踪、能安全恢复”所需的边界补齐：

- `OpenTelemetryRuntimeTelemetry`（OpenTelemetry 追踪）：只允许记录供应商、节点、工具、状态和耗时等运行元数据；问题原文、患者行和 SQL 结果不会进入 span。
- `ApprovalResumeClaims`（审批恢复声明）：用 HMAC（带密钥的摘要）把审批编号、调查编号和审批版本绑定在短时令牌里。
- `InsightFlowClinicalWorkflow`（Temporal 工作流定义）：把一次调查作为可等待审批信号的工作流；实际查询仍由 activity（活动）执行，不在 Temporal Workflow 内直接访问数据库。
- `build_temporal_worker`（Worker 工厂）：只有安装 `temporalio` 后才创建 Worker，未安装时明确报错。

这不是把 LLM 再包一层：Graph 仍决定节点和工具，Temporal 只负责长时间运行与恢复，审批令牌只负责证明“这个决定对应当前版本”。

## 2. 追踪为什么必须脱敏

可观测性是为了回答：哪一步慢、哪个工具失败、哪次模型调用耗时高。它不是第二个证据库。因此 V19 使用固定白名单：

```text
provider / runtime / node / tool / task_id / hypothesis_id
status / result_count / duration_ms / graph_engine
```

任何 `question`、`rows`、自由字典或过长文本都会被丢弃。Logfire 若在部署侧作为 OpenTelemetry exporter（导出器）配置，也只能收到这组脱敏属性。

## 3. 审批恢复令牌

令牌不是登录凭证，也不是临床结论。它只证明：

```text
approval_id + investigation_id + approval_version + expiry
```

验证时必须同时提供当前审批编号、调查编号和版本。审批版本改变、令牌过期、签名不一致或调查编号不匹配都会失败。密钥只从部署环境读取，不能写进数据库、Redis 或 Git。

## 4. Temporal 工作流边界

工作流只协调两类消息：

1. `execute_clinical_investigation_activity`：调用现有运行时，经过发布批次、数据域、权限、MCP 网关和 PostgreSQL 只读查询。
2. `approval_decision` signal（审批信号）：收到人工决定后，携带当前版本的令牌恢复等待中的调查。

Workflow 本身不执行 SQL、不持有 API key、不接收患者行。真实 Worker 需要在部署环境安装 `temporalio` 并注册 activity；本地默认仍使用 V18 的内存/Redis端口。

## 5. 配置

```dotenv
INSIGHTFLOW_TELEMETRY=noop
# 可选：opentelemetry 或 logfire（由部署侧配置 exporter）
# INSIGHTFLOW_TELEMETRY=opentelemetry

INSIGHTFLOW_TEMPORAL_TARGET=localhost:7233
INSIGHTFLOW_TEMPORAL_NAMESPACE=default
INSIGHTFLOW_TEMPORAL_TASK_QUEUE=clinical-v19
```

安装 Temporal SDK：

```powershell
pip install ".\backend[temporal]"
```

当前 Docker 默认只安装 Redis runtime extra；Temporal 服务和 Worker 不会在普通 `docker compose up` 时自动启动，避免本地开发被重型外部服务绑死。

## 6. 验证

```powershell
$env:PYTHONPATH=".;backend"
python -m pytest backend/tests/test_v19_enterprise_runtime.py -q
```

测试覆盖：

- 追踪白名单会过滤问题文本和患者行；
- 审批令牌签名、过期和版本绑定；
- 未安装 SDK 时 Worker 工厂失败得清楚，而不是伪装成已连接 Temporal；
- V17 API 仍保持相同响应结构，并记录 `telemetry_mode`。

## 7. 尚未宣称完成的部分

V19 目前完成的是生产边界和安全合同，不宣称已经连接真实 Temporal 集群或完成真实 DeepSeek 长流程验收。下一步需要：

- 注册实际 activity/Worker，并对重启、重试、取消和审批恢复做集成测试；
- 将审批令牌接到现有 `ApprovalMachine` 的 HTTP/Temporal 恢复路径；
- 运行真实模型的留出题目评测，比较工具选择、证据覆盖和成本。

