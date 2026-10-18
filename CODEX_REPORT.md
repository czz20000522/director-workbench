# CODEX_REPORT

## 任务

Wave2 安全加固与配置统一（worktree：`codex/security-hardening`）。未启动工作台或 ComfyUI，未执行真实 GPU 生成，未 push、未合并。

## Phase 提交

- S1 `d1c7074`：浏览器安全头加固。
- S2 `43e0412`：登录失败缓存过期清理与键数上限。
- S3 `395f8a0`：未捕获异常脱敏、traceback 日志和执行脚本路径约束。
- S4 `b797f45`：保留 4188 协调启动默认值并补 4100/4188 文档与测试。

## 实际改动文件

- `backend/private_auth.py`
- `backend/app.py`
- `backend/routers/task_entry.py`
- `tests/test_optimization_auth_hardening.py`
- `tests/test_private_startup.py`
- `docs/public-development-guide.md`
- `CODEX_REPORT.md`

## 行为变化

- 所有响应保留原有三个安全头，并增加 CSP、Permissions-Policy、Cross-Origin-Opener-Policy；CSP 允许当前 Vite 同源模块/CSS、媒体预览和代理/WebSocket 连接。
- 登录失败记录会清理窗口外键，并按 `LOGIN_FAILURE_MAX_KEYS=4096` 保留最新键；原有 5 次/300 秒 429 语义不变。
- 未处理异常写入 `uvicorn.error` 完整 traceback，API 只返回稳定的 `服务器内部错误，请稍后重试`，不泄露路径或堆栈；HTTPException 业务 detail 保持原行为。
- RENDERER、装配构建器和高清构建器动态加载前执行允许根目录包含校验；合法的项目目录和代码根目录构建器仍可用，越界路径被拒绝。
- `start_backend.ps1`、`start_private_workbench.ps1`、`start_workbench.ps1` 默认仍为 4100；`start-director-services.ps1` 的 4188 有意保留，对应既有客户端 14188 到服务端 4188 SSH 转发，可用 `-WorkbenchPort 4100` 覆盖。

## 新增/调整测试

- 安全头断言、异常响应脱敏与日志、登录失败过期/上限、执行脚本越界路径。
- 启动脚本默认端口和 14188→4188 约定的静态回归测试。

## 验证结果

- `ruff check`：通过。
- `python tools/check_public_docs.py`：通过（299 files，45 links）。
- `npm run build`：通过（Vite production build）。
- `run_tests.ps1 -Suite browser -Check`：通过。
- `run_tests.ps1 -Suite browser`：通过，15 个 browser drivers 全绿。
- `run_tests.ps1 -Suite core`：`1056 passed, 4 skipped, 42 subtests passed`。
- `python tools/export_public_source.py --check`：通过（299 files）。

## 未完成项与风险

- CSP 的 `connect-src` 保留 `http:`、`https:`、`ws:`、`wss:`，以兼容局域网、反向代理和 Vite 开发代理；部署者仍应通过 Origins 白名单限制服务访问。
- 4 个 core skip 来自主机工具/标记条件，未因本次改动新增。

## 新文件列表

- `CODEX_REPORT.md`（被根 `.gitignore` 的公开白名单规则忽略，已按任务要求强制加入本提交）。
