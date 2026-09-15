# Contributing

感谢你关注 InsightFlow Clinical Graph。这个仓库面向临床研究数据工程和受治理分析演示，所有贡献都应保持可审计、可复现，并尊重最小数据暴露原则。

## 开始开发

1. 复制 `.env.example` 为 `.env`，只填入本机开发凭据。
2. 使用 Docker Compose 启动 PostgreSQL、后端和前端：

   ```powershell
   docker compose up -d --build postgres backend clinical-worker frontend
   ```

3. 运行 `scripts/verify.ps1`，再用 `python -m compileall -q backend/app data_generator` 和 `npm run build` 做快速自检。

公开仓库刻意不包含本地测试夹具、评测题库和运行数据；提交前请在自己的开发工作区完成完整回归。

## 提交变更

- 一个 pull request 只解决一个主题，并说明影响的数据域、Graph 节点和迁移。
- 不提交 `.env`、API key、数据库口令、患者数据、`.data/`、缓存或本地归档。
- 数据库变更必须可重复执行，并保留权限、版本和审计边界。
- 不添加自由 Text-to-SQL、绕过工具注册表的查询，或降低最小披露阈值的逻辑。
- 医学参考范围只能来自带来源、版本和审核记录的 catalog；不要从样例值推断医学常数。

## Pull request 检查清单

- [ ] README 或相关文档已同步。
- [ ] `git diff --check` 通过。
- [ ] Python 编译和前端生产构建通过。
- [ ] 迁移可重复执行，且没有修改 V0–V3 的兼容契约。
- [ ] 结果不会暴露患者标识、原始 SQL 或模型密钥。

