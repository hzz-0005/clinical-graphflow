# V27 纵向 EHR 运行说明

状态：V27 Phase 0–5 工程实现和发布前门禁已完成。本文记录当前实际支持和停止条件；它不把 Synthea 当作真实人群，也不把异常比例描述成可用医学结论。

## 实际支持

公开调查入口仍是 `POST /api/v10/clinical/public-investigations`。请求使用现有 `question`、`subject`、`space="synthetic_ehr"` 和 provider 字段；纵向语义（趋势、按日/周/月、随时间、参考范围、异常比例等）由服务端确定性编译为 `analyze_ehr_observation_trend`。普通 EHR 问题继续使用旧的 `summarize_ehr_cohort → profile_ehr_concepts` 链，不改变其返回语义。

纵向查询的实际能力如下：

- 时间粒度只有 UTC `day`、`week`、`month`；日期开始和结束均包含，周从 UTC 周一开始，月从 UTC 月初开始。
- 有日期时必须同时提供 `start_date` 和 `end_date`；最多 120 个时间桶，超出返回 `window_required`，不会静默截断。没有日期时使用当前服务端选择批次的完整可用观察窗口，同样受 120 桶限制。
- 概念和队列来自受治理文本参数；中文观察别名（例如血糖、血红蛋白、血钙、胆固醇、血小板）只映射到有限的英文观察概念，不创造医学代码或参考范围。
- `unit` 只做 trim 后的精确匹配，不做隐式单位换算；不同单位保持不同序列。
- 坏日期计入 `invalid_time_count`，非数值计入 `non_numeric_count`，单位不匹配计入 `unit_mismatch_count`；这些记录不会被当作 0。
- 每个时间桶按患者去重；小于 `MINIMUM_CELL_SIZE=10` 的 cell 抑制测量字段、患者计数和比例，并标记 `suppressed=true` 与 `minimum_cell_size`。
- 输出只允许时间桶、概念、单位、聚合计数、参考范围状态、抑制状态和缺口计数；不返回 `PATIENT_ID`、`ENCOUNTER_ID`、`payload_json`、原始 SQL/参数或单条 observation value。
- 每次实际查询的审计元数据还记录服务端选中的 `data_version_snapshot`（batch id、content hash、发布时间、记录数和 source manifest）；无匹配概念时也记录该快照，不把“无结果”误写成没有数据版本。

## 批次与可复现快照

018 函数不接受 caller 指定 batch。它在 PostgreSQL 内按 `published_at DESC NULLS LAST, batch_id DESC` 选择最新的 published、`public-%`、Synthea `synthetic_patient` 批次，并将观察与 cohort 限定在该批次内，避免跨批次合并同名患者。

本次门禁查询记录的 active snapshot：

| 字段 | 值 |
| --- | --- |
| `batch_id` | `public-synthea` |
| `content_hash` | `d61417b551e5b0997c33851b339c157421751f0ea68c18ea686ceb1850907c35` |
| `published_at` | `2026-09-14 05:00:26.907354+00` |
| `status` | `published` |
| source | `Synthea` / `synthetic_patient` |

可用以下 owner-side 查询重新确认当前选择条件和快照，不要把旧交接文档中的记录数与新快照混写：

```sql
SELECT batch_id, content_hash, published_at, status,
       quality #>> '{source,source_name}' AS source_name,
       quality #>> '{source,source_class}' AS source_class
FROM clinical_ingestion.import_batches
WHERE status = 'published'
  AND batch_id LIKE 'public-%'
  AND quality #>> '{source,source_name}' = 'Synthea'
  AND quality #>> '{source,source_class}' = 'synthetic_patient'
ORDER BY published_at DESC NULLS LAST, batch_id DESC
LIMIT 1;
```

## 数据库和权限边界

- `warehouse/models/clinical_staging/stg_public_ehr_observations.sql` 规范化 OBSERVATION 的观察时间、概念、原始单位、受约束 numeric 值和解析状态；该 staging 保留内部患者/就诊键，不属于 planner-facing public mart。
- `database/init/017_ehr_observation_acl.sql` 撤销 `insightflow_reader` 和 `PUBLIC` 对患者键 staging 的直接读取权限。
- `database/init/018_ehr_observation_trend.sql` 提供固定签名 `analytics_clinical_core.analyze_ehr_observation_trend(text,text,text,date,date,text,text,integer)`。函数 owner 为 `insightflow`，`SECURITY DEFINER`，固定 `search_path=pg_catalog`；只授予 `insightflow_reader` EXECUTE，PUBLIC 不得 EXECUTE。
- 同一迁移还提供 `analytics_clinical_core.get_ehr_observation_snapshot()`，以相同 published Synthea 选择条件返回非患者级 manifest/hash 元数据；reader 同样只有 EXECUTE。
- `scripts/apply_v27_migrations.ps1` 按 017 → 018 幂等执行，并检查函数签名、owner、definer、search path 和 reader/PUBLIC/staging ACL。迁移失败停止，不自动做破坏性回滚。

## Reference catalog 门禁

V27 现在提供受保护的 `analytics_clinical_core.ehr_reference_ranges` catalog 表，包含版本、概念、单位、上下限、有效期、来源和审核元数据。数据库迁移和 ACL 已就绪，但当前 Synthea OBSERVATION v1 仍没有随数据发布 reference low/high 或 abnormal flag，因此 catalog 默认不加载未经审核的医学常数。

没有唯一、已发布且有效期匹配的 `(concept_code, unit, catalog_version)` 时，V27 固定返回 `reference_range_status=unknown|unit_mismatch|unavailable`，`abnormal_patient_rate` 为 `NULL`；`unknown`、空结果和 suppressed cell 都不能解释为 0、正常、未见异常或临床结论。

不得从样例值、min/max、单位常识或外部模型补写医学参考范围。只有 owner 加载带来源、版本和审核记录的 catalog 后，异常分类能力才会对匹配概念打开；加载说明见 [`V27_REFERENCE_RANGE_CATALOG.md`](V27_REFERENCE_RANGE_CATALOG.md)。

## 验证状态

- V25/V26/V27 targeted：42 collected，39 passed，3 expected skipped。
- `backend/tests` 全量：exit 0。
- dbt build/test：15/15。
- 018 migration：最新版本两次幂等 apply 通过；V27 migration script 同时通过 aggregate/snapshot 函数和 ACL 检查。
- Docker backend、clinical-worker、PostgreSQL、Redis、Temporal、frontend：healthy；`/ready`：HTTP 200。
- longitudinal API smoke：通过，并在响应审计元数据中返回 `public-synthea` snapshot；`test_v27_ehr_privacy.py`：6 项通过。
- 未执行 DeepSeek 实机题：纵向能力尚未加入 `PUBLIC_TOOLS` 外部模型 allowlist，确定性路由会绕过 external planner/synthesis。该限制是有意的协议边界，不能把 fake/API smoke 写成 DeepSeek 验收。

## 限制和关闭方式

当前没有虚构的 `V27_EHR_ENABLED` 环境变量或独立运行时开关。关闭/恢复应使用已有、可审计的边界：

1. 数据库紧急关闭：撤销 reader 的固定函数执行权限。恢复前重新执行 `scripts/apply_v27_migrations.ps1` 并复核 ACL；不要直接开放 staging SELECT。

   ```sql
   REVOKE EXECUTE ON FUNCTION analytics_clinical_core.analyze_ehr_observation_trend(
       text, text, text, date, date, text, text, integer
   ) FROM insightflow_reader;
   ```

2. 应用路由关闭：部署不含 V27 longitudinal route 的上一版 `public_investigation.py`，并保留 017/018 的审计记录；不要通过删除表、删除函数或修改旧 EHR mart 做回滚。
3. 参考范围关闭：不要传入未经审计的 `reference_catalog_version`，保持 `unknown`/`NULL` fail-closed 语义。

恢复后必须重跑 V27 定向隐私/路由测试、ACL 检查和 API smoke，再考虑进入下一版本。V27 未做 DeepSeek 实机问题矩阵，也不扩展 CLAIM/费用域或降低 10 人阈值。

