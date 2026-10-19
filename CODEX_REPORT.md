# Wave3-Fix3 修复报告

## 变更

- Phase 1（提交 `9a082ce`）：`PrivateAuth.login_allowed()` 在 SQLite `BEGIN IMMEDIATE` 事务中完成过期清理、配额检查、失败令牌预占和键上限裁剪；并发请求只能获得剩余令牌。数据库不可用时回退到同一进程内的有界内存预占；成功登录清理对应账号与来源的记录；失败日志不包含密码或令牌。新增跨认证实例并发测试，并更新持久化、来源隔离和过期测试以覆盖预占语义。
- Phase 2（提交 `7dd1873`）：移除主内容区域的无条件 `outline: none`，改用清晰的 `:focus-visible` 轮廓；浏览器回归增加 skip link 聚焦、Enter 跳转和轮廓存在断言。

## 验证

- Ruff：通过（全仓 `ruff check`）。
- 目标 pytest：20 passed（`tests/test_private_auth.py tests/test_optimization_auth_hardening.py`）。
- npm：`npm run build` 通过（TypeScript 检查和 Vite 构建均通过）。
- Browser：`run_tests.ps1 -Suite browser` 通过，全部浏览器驱动通过，包含新增 skip link 断言；仅使用隔离夹具和 mock/离线请求，无 GPU。
- Core：`run_tests.ps1 -Suite core` 通过，1070 passed、5 skipped、42 subtests passed；保留 1 个既有 Starlette 弃用警告。
- Public gates：`tools/export_public_source.py --check` 通过（307 个公开文件）；`tools/check_public_docs.py` 通过（45 个相对链接）。
- 工作树在报告提交前已完成差异检查；本次未 push、未合并、未启动服务、未执行真实 GPU。

## 未完成事项

未发现与本次修复相关的失败。core 中的 5 个 skipped 为项目既有环境标记测试，不属于本次改动范围。
