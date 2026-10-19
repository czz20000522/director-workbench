# Wave3-Fix-Review 修复报告

## Phase 提交

- `6eb4fdf` — 修复登录失败预占与失败确认重复计数。
- `903a7c7` — 修复素材原子复制、同大小源替换和登记竞态。
- `b52bdfd` — 修复单 Range/multipart Range 短读的响应终止与连接关闭。
- `20a0c7d` — 修复 assembly 保存响应的 readiness 声明。

## 实际改动

- `backend/private_auth.py`：增加预占令牌确认状态；HTTP 失败回调确认已有令牌，成功登录清理内存和 SQLite 记录。
- `backend/secure_files.py`：复制写入同目录临时文件，最终身份/大小检查通过后原子替换；短读显式终止 ASGI body。
- `backend/routers/material_library.py`：使用内容比较选择新目标名，并始终走原子复制，避免同大小旧内容被登记。
- `backend/app.py`、`backend/routers/batch_assembly.py`：保存视图返回 `readiness_complete=false` 且 `ready=false`；GET 编辑视图和 preflight 才计算完整 readiness。
- 测试覆盖 HTTP 登录 5/6 次限制、成功清理、重启持久化、并发/来源隔离、复制竞态、同大小替换、Range 截断和母版删除后的保存响应。

## 验证

- Ruff：通过（全仓库规则）。
- 目标回归：`92 passed, 3 skipped`。
- Core：`1088 passed, 7 skipped, 42 subtests passed`。
- Browser：完整套件第二次运行通过；首次 `enhancement.browser.mjs` 瞬态超时，单独复现及整套重跑均通过。
- Frontend：`npm run build` 通过。
- Public gates：`export_public_source.py --check` 和 `check_public_docs.py` 通过。
- `run_tests.ps1 -Suite core -Check`：通过。

未执行 push、合并、服务启停或真实 GPU 生成。
