# InsightFlow Clinical V17 Runtime（V17 运行时学习说明）

## 1. 这一版解决什么

V16 已能把临床问题交给动态调查循环，但模型输出仍主要通过现有的运行时协议进入循环。V17 把中间过程明确成几个可检查的对象：

- `InvestigationPlan`（调查计划）：问题拆成哪些必答项、每项需要哪些任务。
- `AnalysisTask`（分析任务）：操作类型、语义指标、语义维度、能力和依赖；不包含 SQL。
- `InvestigationGraphState`（图状态）：问题、计划、假设、观测、证据、覆盖判断和事件。
- `ToolCallRequest` / `ToolObservation`（工具请求 / 观测）：每次查询都带任务编号和假设编号。
- `PlanAnswer`（报告草稿）：逐项回答问题，并引用真实证据或明确缺口。

因此，系统的核心不是“把提示词发给模型”，而是让模型只能填写受治理的结构，然后让程序校验并执行这些结构。

## 2. 一次 V17 调查怎么走

```text
route_question（路由问题）
  ↓
load_context（加载数据目录与可用工具）
  ↓
generate_plan（PydanticAI 生成计划）
  ↓
validate_plan（能力、指标、维度、数据域校验）
  ↓
propose_hypothesis（查询前提出假设）
  ↓
execute_task（MCP 网关校验后调用一个工具）
  ↓
interpret_observation（把结果转成中性观测）
  ↓
verify_coverage（逐项检查是否有证据或缺口）
  ├─ 有证据 → synthesize_report（逐项生成报告）
  └─ 有缺口 → 诚实结束，不把缺失当成反证
  ↓
finish（记录事件、审批状态和兼容的旧状态）
```

实际执行由 `pydantic_graph.Graph` 驱动。节点返回类型决定允许的下一节点；图外层仍使用旧的 `InvestigationState` 保存和返回，因此现有 API、审批和证据序列化不需要同时迁移。

## 3. PydanticAI 在哪里

`PydanticAIClinicalRuntimeLLM` 为每类输出创建一个带 `output_type` 的 Agent：

- 计划 → `InvestigationPlan`
- 动作 → `AgentDecision`
- 计划修订 → `PlanRevision`
- 结论 → `PlanAnswer`

模型不能直接返回自由 SQL。即使模型文本里出现未知工具、SQL 或额外字段，Pydantic 校验也会在查询前拒绝它。DeepSeek、OpenAI、GLM、Kimi 和自定义网关使用 OpenAI-compatible model（兼容模型）适配器；Claude 使用 Anthropic model（原生 Claude 适配器）。

依赖写在 `backend/pyproject.toml`：`pydantic-ai-slim[openai,anthropic]==2.43.0`，并锁定同版本 `pydantic-graph`。测试使用 PydanticAI 的 `TestModel`，不会发网络请求。

## 4. MCP-compatible Gateway（MCP 兼容网关）

当前网关是进程内实现，不是另起一个 MCP 服务。它做四件事：

1. 只列出当前发布数据域允许的工具。
2. 再次校验模型传入的工具名和参数。
3. 调用现有 `ClinicalToolRegistry`，不新增第二套权限系统。
4. 对工具返回的 SQL 用现有 `SqlPolicy` 检查，并统一成 `MCPToolResult`。

这样以后换成真正的 MCP server（MCP 服务）时，Graph 只需替换 Gateway，不需要改临床证据模型。

## 5. 如何切换和回退

`.env`：

```dotenv
# 默认 Graph 路径
INSIGHTFLOW_RUNTIME_VERSION=v17

# 仅在有 owner/expiry 的应急回退窗口内显式启用 V16
# INSIGHTFLOW_RUNTIME_VERSION=v16
```

切换后重建服务：

```powershell
docker compose up -d --build backend clinical-worker frontend
```

V17 和 V16 共用 `/api/v8/clinical/investigations`。区别只在返回的审计元数据：

```json
{
  "audit_metadata": {
    "runtime_version": "17",
    "runtime_graph": "typed_clinical_investigation_graph",
    "graph_engine": "pydantic_graph"
  }
}
```

如果 V17 计划校验、工具参数或模型输出失败，接口沿用现有 `422 dynamic_investigation_invalid`；V16 仅能通过带 owner、reason、未来 expiry 的应急窗口显式启用（详见 `docs/V16_EMERGENCY_FALLBACK.md`），不会因异常自动回退。V17 不会在密钥缺失时悄悄调用 fake provider；只有显式选择 `provider=fake` 的评测请求才使用确定性假模型。

## 6. 运行验证

本地单元测试：

```powershell
$env:PYTHONPATH=".;backend"
python -m pytest backend/tests/test_pydantic_ai_runtime.py backend/tests/test_v17_graph.py backend/tests/test_v17_runtime_api.py backend/tests/test_v17_runtime_ports.py -q
```

V17 held-out contract eval（保留题目合同评测）：

```powershell
$env:PYTHONPATH=".;backend"
$env:PYTHONIOENCODING="utf-8"
python scripts/run_v17_runtime_eval.py --provider fake
```

这项评测检查路由是否正确、工具是否来自注册表、问题必答项是否有证据覆盖、缺口边界是否诚实；它不是把 fake planner 的语言能力冒充 DeepSeek 能力。真实 DeepSeek 评测必须在密钥可用、数据版本明确后单独执行。

## 7. V18 已接入的端口与 V19 仍待完成的部分

V17 先把状态和事件契约固定下来：

- `RuntimeCache` 只负责非权威缓存，不能替代 PostgreSQL 事实。
- `WorkflowCheckpointStore` 只保存可恢复的图状态，不能发布临床结论。
- `RuntimeEventSink` 保存不可变 GraphEvent，后续可接审计流。
- `RuntimeTelemetry` 当前默认 `NoopTelemetry`，不会把患者行数据发送到外部系统。

V18 已提供 Redis 缓存、检查点、事件和幂等锁的可选实现，并固定 Temporal 的类型化启动/审批边界；默认仍使用内存端口，避免本地回归依赖外部服务。V19 再接入真正的 Temporal Workflow/Worker、暂停/恢复、审批令牌和 OpenTelemetry/Logfire 脱敏追踪。无论何时接入，都不能绕过发布批次、数据域、权限、SQL 安全和小样本抑制。

