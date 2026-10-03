# 参与维护

先阅读根 [AGENTS.md](AGENTS.md)、[项目约定](ComfyUI-Workspace/projects/director-workbench/AGENTS.md)与 [GitHub 协作约定](ComfyUI-Workspace/projects/director-workbench/docs/github-collaboration.md)。环境、架构和测试入口见[公开开发指南](ComfyUI-Workspace/projects/director-workbench/docs/public-development-guide.md)。

1. 搜索或提交脱敏 Issue，记录复现步骤、预期行为和可检查的验收条件。
2. 使用 `codex/issue-<编号>-<说明>` 分支完成最小修复，检查实际 diff，运行与改动相关的测试。
3. 创建关联 PR，说明原因、修改、实际验证及限制；全部验收满足才用 `Fixes #编号`，部分交付用 `Refs #编号`。
4. 在 Issue 记录已有实现、实际验证、剩余工作与验收条件。合并、服务部署和跨设备/GPU 验收分别记录；等待产品验收的问题保持开放。

根 `.gitignore` 是逐文件白名单。新增源文件或公开文档须先审查，再明确加入白名单；被排除的作品、账号、配置、任务数据库、运行/审核记录和缓存不能强制添加。公开文档的本地链接必须指向同一提交中的公开文件，避免依赖本机私人说明。凭据、设备地址与私人素材不可写入 Issue、PR、测试样本或提交。
