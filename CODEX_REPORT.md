# Wave3-D 执行报告

分支：`codex/wave3-security`  
基线：`dbdf98f`  
执行结果：全部 phase 完成；未 push、未合并、未启动/停止工作台或 ComfyUI。

## Phase 1 — 登录失败持久化

提交：`da6f170 Persist login failure throttling in SQLite`

- 在复用 `task_store` 的 SQLite 数据库中增加 `login_failure_events` 表及索引。
- `PrivateAuth` 按小写账号与来源隔离失败事件，使用 300 秒窗口、过期清理、单键事件上限和来源键上限。
- 私人模式把后端 `DB_PATH` 传入认证层；数据库写入/迁移异常只记录异常类型并回退内存限流，不打印密码或凭据。
- 新增重启持久化、来源隔离、并发记录、过期恢复和日志脱敏测试。

## Phase 2 — TOCTOU 防护与文档

提交：`e8141f9 Harden file operations against path replacement`

- 新增 `backend/secure_files.py`：在 `no_links` 基础上比较文件设备、inode、大小、修改时间、模式；目录使用稳定的设备/inode/mode 身份。
- 上传使用临时同级文件，复核父目录、原目标和替换后的文件身份；竞态返回 409。
- 媒体、工作流和素材下载使用身份校验后的打开句柄，保留 Range/HEAD 行为。
- 删除计划和应用前复核目录链路，safe-delete 继续负责完整快照和应用时的最终一致性检查。
- 更新公开开发指南说明检查边界、409 重试方式和部署写权限要求。

## Phase 3 — 公开白名单与最终验证

本 phase 提交包含：

- `.gitignore` 明确放行新增的安全模块和测试文件。
- 修正 Windows 文件时间精度下的替换测试。
- 本报告。

## 验证

- `ruff check`：通过。
- 目标安全回归：通过（认证、优化认证、私人工作台、路径和新增安全文件测试）。
- `npm run build`：通过。
- `run_tests.ps1 -Suite browser`：通过。
- `run_tests.ps1 -Suite core`：通过，`1064 passed, 4 skipped, 42 subtests passed`。
- `tools/export_public_source.py --check`：通过，301 个文件。
- `tools/check_public_docs.py`：通过，301 个文件、45 个相对链接。

最终实际改动文件：`.gitignore`、`backend/private_auth.py`、`backend/private_mode.py`、`backend/task_store.py`、`backend/app.py`、`backend/private_deletion.py`、`backend/routers/events_media.py`、`backend/routers/material_library.py`、`backend/user_context.py`、`backend/secure_files.py`、`tests/test_private_auth.py`、`tests/test_secure_files.py`、`docs/public-development-guide.md`、`CODEX_REPORT.md`。
