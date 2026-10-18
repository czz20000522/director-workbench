# CODEX 任务报告

任务：公开文档与 README 刷新（与当前实现对齐）  
分支：`codex/docs-refresh`  
提交：`ea6da71`（D1）、本报告提交（D2）

## Phase D1：公开文档刷新

- 改动文件：`README.md`、`docs/public-development-guide.md`、`docs/public-api-guide.md`。
- 行为变化：无。只补充当前实现的公开说明。
- 内容：记录 `backend/app.py` 与 15 个 `backend/routers/` 模块的边界、路由注册顺序与兼容导出；补充 `tests/conftest.py` 和 `tests/support.py` 的测试组织；说明 `src/api/client.ts` 的共享请求/错误/媒体 URL 能力及前端按需加载；补充 fast core、browser 入口和 CI 四个 job；记录上传限制、登录失败限速与 `/api/status` 的机器路径脱敏。
- 新增测试：无（本任务仅更新文档）。
- 提交：`ea6da71 docs: align public guides with current implementation`。

## Phase D2：验证与交付报告

- 改动文件：`CODEX_REPORT.md`。
- 行为变化：无。
- 新增测试：无。
- 验证结果：
  - `.\.venv\Scripts\python.exe tools\check_public_docs.py`：通过，293 个文件、45 条相对链接。
  - `.\.venv\Scripts\python.exe tools\export_public_source.py --check`：通过，检查 293 个文件。
  - `.\.venv\Scripts\ruff.exe check`：通过。
  - `npm run build`：通过，TypeScript 检查和 Vite 生产构建均成功。
  - `.\run_tests.ps1 -Suite browser`：通过，15 个 browser 驱动全部通过。
  - fast core 等价命令 `.\.venv\Scripts\python.exe -m pytest tests tools/test_build_video_finish_workflow.py --ignore=tests/test_generate_auk_flash.py -q --basetemp <隔离临时目录> -m "not host_tool and not slow and not windows"`：989 passed、4 skipped、59 deselected、42 subtests passed。

## 未完成项与原因

没有未完成的文档项。当前 `run_tests.ps1` 的合法 suite 是 `all`、`core`、`auk`、`browser`，没有名为 `fast` 的 PowerShell suite；因此按 CI 完全相同的 pytest 标记过滤执行并在开发指南中明确说明。AuK 专用环境和完整 `all` 套件不属于本次文档刷新必跑范围。

## 风险点

- 文档中的部署地址、账号、作品、路径和凭据均保持脱敏；实际部署、模型就绪、跨设备访问和 GPU 产物仍需按维护流程单独验收。
- 本次未修改代码、接口、路由顺序、`.gitignore` 或测试断言；文档描述以当前工作树实现和 CI 配置为依据。
- 报告文件按任务要求用 `git add -f` 提交，但当前公开白名单尚未包含 CODEX_REPORT.md；因此报告提交后的 `check_public_docs.py` 与 `export_public_source.py --check` 会按设计返回 Review required。Lead 补充白名单后需重跑两项门禁；报告加入前的验证结果已在上文记录。

## 新文件

- `CODEX_REPORT.md`：本任务的 phase、验证和风险记录。
