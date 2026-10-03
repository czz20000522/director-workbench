# 开发仓库约定

- 本机存在 `.config/director-workbench/local-storage-policy.md` 时，先阅读其磁盘、环境和资产存储约定；此文件是本机有效配置，不公开提交。
- 按用户 2026-10-03 确认：D 盘用于 ComfyUI 运行、工作台开发和热缓存；输入、输出与现有/未来作品在 E 盘，按账号隔离。ComfyUI-Shared 保持 D 盘真实目录。有效资产根由服务器 storage.json 配置；不得仅凭文档改动搬迁或覆盖配置。

- 先阅读项目及 `ComfyUI-Workspace/projects/director-workbench/AGENTS.md` 与 `ComfyUI-Workspace/projects/director-workbench/docs/github-collaboration.md`。
- 所有设备通过 GitHub Issues 反馈；本机维护 Agent 通过关联 PR 交付最小修复，记录实际验证与限制。
- 作品、账号、配置、运行记录、审核与缓存是独立本地数据，不进入任何提交或 Git 历史。
- .gitignore 为公开源代码白名单；新增文件必须审查内容并明确加入白名单。不得强制添加被排除的数据。
- 不上传旧本机历史；不因收到 Issue 而自动部署、重启、生成或合并。
- 修改后列出实际改动文件及验证步骤。删除本地数据遵守用户 safe-delete 两阶段规则。
