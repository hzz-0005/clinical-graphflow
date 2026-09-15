# InsightFlow Clinical V18 Durable Runtime（可恢复运行时）

## 1. 这一步解决什么

V17 已经把调查过程拆成了可以检查的 Graph（流程节点）。V18 再把流程运行时需要的“临时记忆”和“重试边界”接上可替换实现：

- `RedisRuntimeCache`（Redis 缓存）：缓存数据目录等短期信息，过期后可以重新读取 PostgreSQL。
- `RedisWorkflowCheckpointStore`（Redis 检查点）：保存可恢复的 `InvestigationGraphState`，用于进程重启后的恢复。
- `RedisRuntimeEventSink`（Redis 事件流）：按调查编号保存节点事件，方便回放“先提出了什么假设、后调用了什么工具”。
- `RedisIdempotencyStore`（Redis 幂等锁）：只记录请求占用者，避免同一个请求被重复启动；不保存调查结果。
- `TemporalClinicalWorkflowBoundary`（Temporal 工作流边界）：把调查请求和人工审批信号转换成类型化的 Temporal 调用。它不改变临床工具，也不绕过 PostgreSQL 事实库。

一句话：**Redis 负责短期协调，Temporal 负责长时间流程，PostgreSQL 仍负责最终事实、证据、审批和审计。**

## 2. 为什么默认仍是 memory（内存）

本地开发和 V16/V17 回归不应该因为没有 Redis 或 Temporal 服务而无法启动。因此默认配置是：

```dotenv
INSIGHTFLOW_RUNTIME_PORTS=memory
INSIGHTFLOW_RUNTIME_VERSION=v17
```

切换到 Redis 时必须显式配置：

```dotenv
INSIGHTFLOW_RUNTIME_PORTS=redis
INSIGHTFLOW_REDIS_URL=redis://redis:6379/0
INSIGHTFLOW_REDIS_NAMESPACE=insightflow
INSIGHTFLOW_RUNTIME_TTL_SECONDS=3600
```

未知模式会直接报错，不会静默退回内存。这样生产环境不会误以为自己有跨进程恢复能力。

## 3. 数据边界

Redis 里允许出现：

- 数据目录缓存（字段、域和版本元数据）；
- Graph 检查点和节点事件；
- 幂等请求的占用者标记。

Redis 里不应存放：

- 最终临床事实或发布批次的权威数据；
- 未经授权的患者行；
- API 密钥、模型密钥或数据库密码。

证据、结论、审批决定和审计事件仍通过现有 PostgreSQL/企业仓储保存。缓存即使丢失，也只能导致重新读取或重新执行，不能改变事实。

## 4. Temporal 边界现在能做什么

`TemporalClinicalWorkflowBoundary` 只接收两个类型化对象：

- `TemporalWorkflowInput`（工作流输入）：调查编号、问题、试验编号、发布批次和发起人；
- `TemporalApprovalSignal`（审批信号）：审批编号、通过/拒绝、审批人、时间和备注。

当前 V18 先把边界和测试固定下来，实际 Temporal Worker（工作流执行器）放到后续版本。这样以后接入官方 `temporalio` 客户端时，不需要改 Graph、MCP 网关或证据模型。

## 5. 本地验证

只验证内存端口和协议：

```powershell
$env:PYTHONPATH=".;backend"
python -m pytest backend/tests/test_v18_durable_runtime.py backend/tests/test_v17_runtime_api.py -q
```

验证 Redis 配置结构（不启动服务）：

```powershell
docker compose config --quiet
```

本地启动 Redis（可选）：

```powershell
docker compose --profile durable up -d redis
```

后端镜像默认安装 `runtime` extra（可选依赖，包含 `redis` 客户端）。Temporal SDK 单独放在 `temporal` / `durable` extra，待 V19 Worker 实现时再安装：

```powershell
pip install ".\backend[durable]"
```

## 6. 与 V17 的关系

V18 没有改变：

- 临床工具名称、参数和只读 SQL 合同；
- 已发布数据域、权限和小样本抑制；
- `no_data` / `insufficient_data` 只能表示缺口；
- V16 回退开关和既有 API 返回结构。

V17 Graph 在创建时会根据 `INSIGHTFLOW_RUNTIME_PORTS` 获得事件 sink 和 checkpoint store，并在最终状态的 `audit_metadata.runtime_ports` 记录 `memory` 或 `redis`。这让验收者能区分“只在进程内运行”和“已启用跨进程端口”。

## 7. 下一步

V19 已补齐 Temporal Workflow/Worker 外壳、审批恢复令牌和 OpenTelemetry/Logfire 脱敏 span 的安全合同；真实集群 Worker 与真实 DeepSeek 长流程验收仍需单独部署验证。临床事实仍只能来自 PostgreSQL，Redis/Temporal 只负责协调和耐久执行。

