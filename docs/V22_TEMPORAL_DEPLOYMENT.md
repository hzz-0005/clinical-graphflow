# InsightFlow Clinical V22 Temporal Deployment（Temporal 部署与自动投递）

## 1. 这一版解决什么

V21 已经把审批决定写入 PostgreSQL 的事务发件箱（Transactional Outbox，事务发件箱），但还需要管理员手工调用接口重试。V22 增加一个独立的 `clinical-temporal-outbox` 进程，按固定间隔自动取出待发送事件并投递给 Temporal：

```text
审批决定
  → PostgreSQL 同一事务写入审批状态 + 发件箱事件
  → clinical-temporal-outbox 定时读取 pending（待发送）事件
  → Temporal Signal（工作流信号）
  → 成功：sent（已发送）并按审批语义发布调查
  → 失败：pending 重试；超过上限后 dead（死信）
```

它只处理 `approval_id`、版本号、审批人和时间等元数据，不读取患者行、SQL、证据内容或恢复令牌。证据仍然只保存在现有 PostgreSQL 调查记录中。

本版还提供一个可选的本地 Temporal 开发集群，方便在没有外部基础设施时验证真实 Workflow（工作流）和 Activity（活动）边界：

```text
temporal-postgres（Temporal 自己的数据库）
        ↓
temporal（Temporal Server） ← temporal-ui（可选管理界面）
        ↓
clinical-temporal-worker（注册 Workflow + Activity）
        ↓
PostgreSQL / Redis / 现有 V17 Graph

clinical-temporal-outbox（审批信号自动投递器） ──→ temporal
```

## 2. 本地启动

先保证主项目的 `.env` 已存在，并设置数据库密码。Temporal 相关服务放在 `temporal` profile（可选服务组）中，不会改变默认 V16/V17 的启动方式。

```powershell
$env:INSIGHTFLOW_TEMPORAL_ENABLED="true"
$env:INSIGHTFLOW_RUNTIME_VERSION="v17"
$env:INSIGHTFLOW_RUNTIME_PORTS="redis"
$env:INSIGHTFLOW_TEMPORAL_TARGET="temporal:7233"
$env:INSIGHTFLOW_TEMPORAL_TASK_QUEUE="clinical-v20"

docker compose --profile temporal up -d `
  redis temporal-postgres temporal `
  clinical-temporal-worker clinical-temporal-outbox
```

查看服务状态：

```powershell
docker compose --profile temporal ps
```

应该至少看到：

- `temporal-postgres`：`healthy（健康）`；
- `temporal`：`healthy（健康）`；
- `redis`：`healthy（健康）`；
- `clinical-temporal-worker`：`Up（运行中）`；
- `clinical-temporal-outbox`：`Up（运行中）`。

Temporal gRPC（工作流通信端口）映射到主机 `localhost:57233`。如需打开 Temporal 管理界面：

```powershell
docker compose --profile temporal up -d temporal-ui
```

然后访问 `http://localhost:58080`。管理界面只展示工作流元数据，不是临床证据查询界面。

## 3. 配置说明

`.env.example` 中新增：

```dotenv
INSIGHTFLOW_TEMPORAL_ENABLED=false
INSIGHTFLOW_TEMPORAL_OUTBOX_MAX_ATTEMPTS=5
INSIGHTFLOW_TEMPORAL_OUTBOX_POLL_SECONDS=5
INSIGHTFLOW_TEMPORAL_OUTBOX_BATCH_SIZE=20
TEMPORAL_VERSION=1.29.1
TEMPORAL_UI_VERSION=2.34.0
```

含义：

| 配置 | 含义 |
| --- | --- |
| `INSIGHTFLOW_TEMPORAL_ENABLED` | 是否允许启动 Temporal API、Worker 和自动投递器；默认关闭 |
| `INSIGHTFLOW_TEMPORAL_OUTBOX_POLL_SECONDS` | 自动投递器两次轮询之间的秒数 |
| `INSIGHTFLOW_TEMPORAL_OUTBOX_BATCH_SIZE` | 每轮最多处理的发件箱事件数 |
| `INSIGHTFLOW_TEMPORAL_OUTBOX_MAX_ATTEMPTS` | 一个事件最多尝试投递的次数 |
| `TEMPORAL_VERSION` | 本地 Temporal Server 镜像版本 |
| `TEMPORAL_UI_VERSION` | 本地 Temporal UI 镜像版本 |

`backend` API 镜像和 Temporal Worker 镜像都包含 `temporalio` SDK；只有把
`INSIGHTFLOW_TEMPORAL_ENABLED` 设为 `true` 时，API 才会连接 Temporal，默认配置仍然不会启动
工作流客户端。这样启用控制面时不会出现“镜像里没有 SDK”的部署错误。

生产环境要把 `INSIGHTFLOW_TEMPORAL_TARGET` 改成受管 Temporal 集群地址，并使用真正的密钥托管，不要使用仓库里的本地默认密码或本地 auto-setup（自动初始化）镜像。官方 Compose 示例适合开发验收，生产应使用正式 Temporal Server/受管集群：[Temporal Docker Compose examples](https://github.com/temporalio/docker-compose)。

## 4. 如何启动一次真实 Workflow

主机上的普通 Python 环境不一定安装 `temporalio`，因此最简单的验收方式是在 Worker 容器内运行客户端。下面的脚本会：

1. 启动一个真实的 `InsightFlowClinicalWorkflow`；
2. 让 Activity 复用现有 V17 Graph、MCP 网关、Redis 和 PostgreSQL；
3. 等待 Activity 创建调查和审批记录；
4. 发送取消信号，验证工作流可以从审批等待中安全结束。

```powershell
$script = @'
import asyncio
from uuid import uuid4
from temporalio.client import Client
from app.clinical.temporal_workflow import InsightFlowClinicalWorkflow

async def main():
    client = await Client.connect("temporal:7233", namespace="default")
    workflow_id = str(uuid4())
    payload = {
        "investigation_id": workflow_id,
        "question": "为什么本试验的 Week-12 治疗效果下降？",
        "trial_id": "TRIAL-CF-101",
        "requested_by": "admin",
        "provider": "fake",
        "available_domains": ["ADSL", "ADEFF", "DM", "AE", "EX"],
        "runtime_version": "v17",
    }
    handle = await client.start_workflow(
        InsightFlowClinicalWorkflow.run,
        payload,
        id=workflow_id,
        task_queue="clinical-v20",
    )
    print("WorkflowStarted", workflow_id)
    await asyncio.sleep(20)
    await handle.signal("cancel_investigation", {
        "requested_by": "admin",
        "requested_at": "2026-09-14T08:00:00+00:00",
        "reason": "V22 smoke cleanup",
    })
    result = await handle.result()
    print("WorkflowResult", result)

asyncio.run(main())
'@
$script | docker exec -i insightflow-clinical-clinical-temporal-worker-1 python -
```

查询某个工作流：

```powershell
docker exec insightflow-clinical-temporal-1 `
  temporal workflow describe `
  --workflow-id <workflow_id> `
  --namespace default
```

正常的取消验收应看到：`Status COMPLETED`，结果中的 `status` 为 `cancelled（已取消）`，并且 `publication_status` 仍为 `pending_approval（等待审批）`。这证明取消没有伪造“已批准”，也没有删除 PostgreSQL 调查记录。

## 5. 自动发件箱验收

`clinical-temporal-outbox` 正常运行时没有待发送事件也不会打印临床内容；它会持续轮询 PostgreSQL。管理员仍可以在故障排查时使用 V21 的受保护手工接口：

```powershell
$body = @{ limit = 20 } | ConvertTo-Json
Invoke-RestMethod `
  -Method Post `
  -Uri http://localhost:18000/api/v20/clinical/outbox/dispatch `
  -Headers @{ "X-InsightFlow-User" = "admin" } `
  -ContentType "application/json" `
  -Body $body
```

自动投递器和手工接口使用同一个 `TemporalOutboxDispatcher`（发件箱分发器），因此重试次数、死信和重复审批版本幂等规则不会产生两套行为。

## 6. 本版修复的真实问题

- Temporal Worker 注册的 Activity 补上 SDK 要求的 `@activity.defn`（活动定义装饰器）；此前真实 Worker 会在启动时拒绝该 Activity。
- Worker 和发件箱服务显式依赖健康的 Redis；此前容器虽然启动，运行时却无法解析 `redis` 主机名。
- Temporal 工作流编号与数据库 `uuid` 主键边界对齐：`X-Request-ID` 可以是人类可读的相关性编号，但不能直接当 PostgreSQL `uuid`。非 UUID 请求头现在会生成新的 UUID，合法 UUID 会被保留。
- 确定性 `fake` 规划器不再把第一个目录字段（例如 `actual_dose`）硬塞给不支持该指标的指标发现能力；它现在只选择 capability spec（能力声明）和目录都支持的 measure/dimension。

这些问题不是单元测试凭空猜出来的，而是在真实 Temporal Server + Worker + PostgreSQL + Redis 验收时暴露并修复的。

## 7. 测试

V22 代码级测试不依赖 Temporal Server：

```powershell
$env:PYTHONPATH=".;backend"
python -m pytest `
  backend/tests/test_v22_temporal_operations.py `
  backend/tests/test_v21_temporal_operations.py `
  backend/tests/test_v20_temporal_integration.py `
  backend/tests/test_v19_enterprise_runtime.py -ra --tb=short
```

Compose 静态配置检查：

```powershell
docker compose config --quiet
docker compose --profile temporal config --quiet
```

V22 真实部署验收的边界是“工作流能启动、Activity 能复用 V17、能进入审批等待、取消信号能结束、发件箱进程能运行”。V23 进一步补上了状态查询和 Activity 重试幂等；这两版都不等于已经完成 DeepSeek 长流程、生产集群故障演练或临床结论正确性验收，那些属于后续 V24 的部署和评测工作。

## 8. 与原有项目的关系

V22 不改变默认同步 API，也不要求普通开发者启动 Temporal：

```text
默认：PostgreSQL + FastAPI + React + V17 Graph
可选：Redis + Temporal Worker + Temporal Server + Outbox Dispatcher
```

Temporal 只是长时间运行、审批等待、取消和重启恢复的外层编排器；指标定义、工具选择、参数校验、只读 SQL、证据链和中文结论仍由现有 InsightFlow Clinical Runtime 负责。

