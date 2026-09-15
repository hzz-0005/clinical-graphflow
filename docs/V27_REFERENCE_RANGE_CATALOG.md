# V27 参考范围 catalog

V27 现在包含受保护的 `analytics_clinical_core.ehr_reference_ranges` catalog。它只保存非患者级的、版本化参考范围元数据；纵向聚合函数通过 `SECURITY DEFINER` 读取它，`insightflow_reader` 不能直接读取或修改 catalog。

## 记录要求

每条已发布记录必须同时提供：

- `catalog_version`、`concept_code`、`unit`
- `low`、`high`，边界策略固定为 `inclusive_normal`（等于边界值视为正常）
- `population_context`（当前函数使用 `general`）
- `source`，以及 `source_uri` 或 `source_version`
- `review_status='published'`、`reviewed_by`、`reviewed_at`
- `effective_from`，可选 `effective_to`

同一版本、概念、单位和人群上下文只能有一条记录。冲突版本或冲突上下限不会被聚合器选择，而是返回 `reference_range_status='unavailable'`。

## 加载方式

catalog 由数据库 owner（`insightflow`）加载，不能由应用 reader 身份写入。可复制 [`examples/ehr_reference_catalog.example.yml`](../examples/ehr_reference_catalog.example.yml)，填入审核后的来源后运行：

```powershell
$env:ENTERPRISE_DATABASE_URL = 'postgresql://insightflow:<owner-password>@localhost:55432/insightflow'
python scripts/load_ehr_reference_catalog.py .\my-reviewed-catalog.yml --dry-run
python scripts/load_ehr_reference_catalog.py .\my-reviewed-catalog.yml
```

示例 SQL 仅展示接口，不提供未经审核的医学数值：

```sql
INSERT INTO analytics_clinical_core.ehr_reference_ranges (
    catalog_version, concept_code, unit, low, high,
    boundary_policy, population_context, source, source_uri, source_version,
    review_status, effective_from, reviewed_by, reviewed_at
) VALUES (
    'approved-source-v1', 'LOINC-CODE', 'UNIT', 0, 1,
    'inclusive_normal', 'general', 'Approved source name',
    'https://approved-source.example/range', 'approved-source-v1',
    'published', DATE '2026-01-01', 'reviewer-id', now()
);
```

上面的数值和 URL 是占位示例，不得直接用于临床判断。当前仓库的 Synthea `OBSERVATION` v1 没有提供 reference low/high，因此没有在迁移中臆造医学常数；没有匹配记录时仍返回 `unknown`/`NULL`。

## 查询语义

`analyze_ehr_observation_trend` 按 `(concept_code, unit, catalog_version)` 精确匹配已发布、有效期覆盖时间桶的记录。只有唯一且无冲突的范围才会计算异常患者比例；单位不匹配、版本不匹配、范围冲突和缺失都保持 fail-closed。低于 10 名患者的 cell 仍按既有隐私阈值抑制。

