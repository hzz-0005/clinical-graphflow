# V25：Runtime Resume & Replay（运行时恢复与重放）

## 这一版解决什么问题

V17 的类型化调查图已经能把问题拆成计划、提出假设、调用受治理工具、记录证据并生成结论。V25 继续改运行时本身：调查不再只有结束时的一份状态，而是在每个关键节点和工具结果之后保存检查点（checkpoint）。

这样，长调查遇到进程重启、Worker 重试或网络超时后，可以从最后一个安全位置继续，不需要重新调用已经完成的工具，也不会为了重试再次消耗 DeepSeek。

## 运行流程（Flow）

```text
问题请求
  ↓
route_question / load_context
  ↓ 每个节点完成后保存 checkpoint
generate_plan（首次生成；恢复时复用已有 plan）
  ↓
validate_plan
  ↓
propose_hypothesis
  ↓
execute_task → 工具结果进入受保护 evidence
  ↓ 保存“已完成任务”游标
verify_coverage → synthesize_report → finish
```

恢复时，runtime factory（运行时工厂）先按 `investigation_id` 读取检查点，并校验四个请求身份字段：

- `question` / 问题正文
- `trial_id` / 试验编号
- `published_batch_id` / 已发布数据版本
- `space` / 调查空间

任意字段不一致都会拒绝恢复（fail closed，失败关闭），不能用一个调查编号借用另一个问题的计划或证据。

## 事件和证据的边界

GraphEvent（运行事件）只是编排遥测，不是临床证据通道。事件中只保留节点、事件类型、任务编号、工具名、数量和状态等最小信息；以下内容会被递归脱敏：

- `question` / `query`（问题和检索词）
- `sql` / `params`（SQL 和参数）
- `rows` / `observation` / `evidence`（结果行、观测和证据正文）
- `arguments` / `summary` / `error`（工具参数、摘要和错误正文）

完整 SQL、结果行和证据仍由原有 PostgreSQL 调查记录负责保存。Redis 或内存端口只承担恢复、事件和幂等协调，不能变成临床事实库，也不能绕过数据域、权限或最小样本抑制。

## 端口（Ports）

`WorkflowCheckpointStore`（检查点存储）和 `RuntimeEventSink`（事件接收器）仍是可替换接口：

- `memory`（内存）：本地开发和单元测试，进程内使用；
- `redis`（Redis）：跨请求、跨 Worker 的恢复与事件流；
- PostgreSQL：仍然是最终临床事实、证据、审批和审计的权威来源。

生产环境要跨进程恢复时，应将 `INSIGHTFLOW_RUNTIME_PORTS=redis`，并设置 `INSIGHTFLOW_REDIS_URL`。V25 没有把运行协调状态误当成证据发布。

## 如何验证（Verification）

```powershell
$env:PYTHONPATH=".;backend"
python -m pytest backend/tests/test_v25_runtime_replay.py -q
```

测试覆盖：

1. 每个节点/工具结果都会留下中途检查点；
2. 事件没有问题正文、SQL、参数、结果行和观测摘要；
3. 从含有已完成任务的检查点恢复时，不再次调用计划模型或工具；
4. runtime factory（运行时工厂）能加载匹配检查点；
5. 问题正文不匹配时拒绝恢复，防止调查状态串用。

## 本版不改变的内容

- 不改变 V8/V16 API 和旧版回退路径；
- 不改变临床工具、MCP gateway、Registry、PlanValidator 或只读 SQL 安全策略；
- 不改变证据编号、10 人最小单元格抑制和人工审批边界；
- 不重复运行已完成的 DeepSeek 题目矩阵；
- 不宣称内存端口具备跨进程持久性，也不宣称 Redis 已替代 PostgreSQL 证据持久化。

## 下一步

下一版再把“调查状态版本”和 PostgreSQL 调查记录的乐观锁（optimistic locking）放进同一个明确事务边界，并用真实 Temporal Worker 重启演练验证：重试、恢复、证据写入和审批不会互相覆盖。

