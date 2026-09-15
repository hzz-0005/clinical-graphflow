# G3 Graph canary / soak gate

代码层的 G1/G2 已完成，V17 是同步 API、动态 Worker 和 Graph-only Temporal 的默认路径。
G3 不能以单元测试替代生产观测：必须完成至少 7 个自然日或一个完整业务发布周期（取较
长者）的 canary/soak，并记录以下指标后才可退役 V16 正常开关。

## Canary matrix

- 正常有数据、无数据 gap、计划校验失败、工具失败；
- 审批暂停/批准/拒绝、重复 request、双 Worker claim、Worker 重启；
- Temporal Activity 重试、workflow 重启、outbox reclaim、旧 Graph checkpoint/旧调查行；
- PostgreSQL canonical snapshot、projection、evidence、approval 的版本一致性；
- API/事件/日志隐私扫描，以及 `/ready`、Docker health、迁移幂等性。

## Release record

每次 canary 至少记录开始/结束时间、owner、样本范围、Graph node 失败率、P95、CAS 冲突率、
重试率、重复 evidence/approval 数、审批恢复成功率和隐私告警数。任何数据丢失/重复、版本倒退、
runtime 分叉、隐私告警或错误率超过基线都立即停止并按
[`V16_EMERGENCY_FALLBACK.md`](V16_EMERGENCY_FALLBACK.md) 回退。

运行时 `/api/v4/clinical/operations` 返回上述指标的受限快照：`graph_nodes` 按固定节点名统计
成功/失败和累计 duration，`integrity` 提供 CAS 冲突、重试、重复请求/claim、审批信号和隐私
事件计数。指标标签只允许固定低基数值，不包含问题文本、SQL、患者标识或结果行。

当前已完成本地 Docker canary、真实 PostgreSQL Graph snapshot+approval transaction、
V25/V26/Graph targeted 和 backend 全量回归；发布周期 soak 尚未开始，因此 G3 仍保持未完成。

### 本地短 soak 记录（2026-09-15）

- owner：local development gate；范围：本机 Docker Compose、合成 `TRIAL-CF-101`、fake provider、UUID 隔离测试行。
- 严格入口：`--require-postgres --strict --repeat 3`；19 个检查全部通过、0 skipped。Temporal 检查在显式启用配置下只验证 profile 服务健康与 target/namespace/task queue，不伪造 workflow API 通过。
- 真实容器 Job：`/api/v8/clinical/jobs` 入队后由 `clinical-worker` 以 V17 Graph 完成，`succeeded`、attempt=1；调查、审批、证据、审计、Job 行均已清理并复核为 0。
- 运行指标端点返回固定节点耗时/失败和 integrity 计数；隐私扫描未发现问题正文、SQL、患者标识或结果行。
- 该记录是本地短 soak 和故障演练，不满足“至少 7 个自然日或一个完整业务发布周期”的时间门禁；因此不能据此退役 V16 回退开关。

## 可复现门禁入口

`scripts/run_graph_canary.py` 将上述自动化部分固定为一个可审计矩阵：

```powershell
# 仅运行内存/隔离门禁（没有 Docker 或 PostgreSQL 时）
python scripts/run_graph_canary.py --skip-docker --json

# 完整本地门禁；PostgreSQL 测试只接受 compose 的本机目标，且测试用 UUID 行并在 finally 中清理
$env:ENTERPRISE_DATABASE_URL = "postgresql://...@localhost:55432/insightflow"
python scripts/run_graph_canary.py --require-postgres --json-output .tmp/graph-canary.json

# 发布门禁：任何 Docker/PostgreSQL 跳过项都会使退出码非零
python scripts/run_graph_canary.py --require-postgres --strict --json-output .tmp/graph-canary.json

# 若本次发布包含 Temporal profile，先在隔离环境显式启用它；否则 strict 会保留
# `temporal_service_gate` 的跳过并故意失败，避免把未演练的 Temporal 当作通过。
$env:INSIGHTFLOW_TEMPORAL_ENABLED = "true"
python scripts/run_graph_canary.py --require-postgres --strict --json-output .tmp/graph-canary-temporal.json

# 连续多轮作为短 soak；完整发布周期仍需在部署环境中记录
python scripts/run_graph_canary.py --require-postgres --repeat 3
```

入口只读取 Docker `ps/config` 与 `/health`、`/ready`，然后调用固定的 V17 Graph、默认运行时、
canonical/CAS/approval/privacy 和 PostgreSQL transaction 测试；不执行迁移、不删除非测试行，
也不会切换 V16。默认输出人读摘要，`--json` 输出机器可读摘要；远程 PostgreSQL 目标必须显式
使用 `--allow-remote-postgres`，以避免误把生产库当作 canary 数据库。

