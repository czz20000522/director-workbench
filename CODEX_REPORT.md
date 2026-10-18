# MCP server 拆分执行报告

## Phase 1：共享输入模型

- 提交：`90c2bd7 refactor(mcp): extract shared input models`
- 改动文件：`mcp_server/server.py`、`mcp_server/models.py`
- 行为变化：无。原有 `ShotChanges`、`ShotMaterials`、`ScriptScene` 的模型定义保持不变，并由服务器继续导出。
- 验证：`ruff check mcp_server/server.py mcp_server/models.py` 通过；`pytest tests/test_mcp_adapter.py tests/test_remote_mcp.py -q`（16 passed）。

## Phase 2：领域注册器拆分

- 提交：`6ec84e9 refactor(mcp): split tools into domain registrars`
- 改动文件：`mcp_server/server.py`、`mcp_server/tools_creative.py`、`mcp_server/tools_reference.py`、`mcp_server/tools_generation.py`、`mcp_server/tools_media.py`
- 行为变化：无。`server.py` 仅保留连接校验、HTTP 请求夹具和四个领域注册器组装；注册顺序、工具名、描述、输入 schema、返回结构和错误语义保持不变。工具总数为 56。
- 验证：`ruff check mcp_server` 通过；全部 `test_mcp_*.py` 与 `test_remote_mcp.py`（164 passed）。

## Phase 3：schema 快照

- 提交：`7823186 test(mcp): snapshot tool names and schemas`
- 改动文件：`tests/test_mcp_adapter.py`
- 行为变化：无。新增名称顺序和完整工具 schema 的 SHA-256 快照，基线摘要为 `513073b4fd0caa9e4a5e60525811479b63036e4404cc3c21a0d1911f82938fac`。
- 验证：`pytest tests/test_mcp_adapter.py -q`（11 passed）；该测试文件的 `ruff check` 通过。

## Phase 4：最终验证与报告

- 改动文件：`CODEX_REPORT.md`
- 行为变化：无。
- 验证结果：
  - `ruff check`：通过。
  - `npm run build`：通过。
  - `run_tests.ps1 -Suite browser`：除 `tests/quick_generation.browser.mjs` 外通过；该驱动停在登录页，显示“登录已过期，请重新登录”，随后 `page.waitForFunction` 超时。没有改动登录或浏览器测试。
  - `run_tests.ps1 -Suite core`：1048 passed、4 skipped、1 failed。唯一失败是 `tests/test_public_source.py::test_clean_public_checkout_starts_private_app_without_user_works`，因为本次新增文件尚未加入公开白名单。
  - `tools/export_public_source.py --check`：按预期报告待审文件。
  - `tools/check_public_docs.py`：因同一公开白名单阻塞。
  - 任务要求的 `run_tests.ps1 -Suite fast`：当前脚本参数校验不支持 `fast`，仅接受 `all, core, auk, browser`；未修改脚本，已用现有 `core` 和 `browser` 入口验证。

## 未完成项与原因

- 未修改 `.gitignore` 或公开白名单。以下新文件需要 Lead 在公开发布前补白名单：`mcp_server/models.py`、`mcp_server/tools_creative.py`、`mcp_server/tools_generation.py`、`mcp_server/tools_media.py`、`mcp_server/tools_reference.py`。
- 未重跑 `quick_generation`；它需要有效的测试登录状态或对应测试夹具，当前失败发生在 UI 登录门槛之前。

## 风险点

- 新模块当前被公开源检查视为待审路径；在白名单补齐前，公开源导出和依赖该导出的 core 用例会继续失败。
- schema 快照固定了当前 MCP SDK 序列化结果；若 SDK 升级导致合法 schema 序列化变化，应先重新核对工具契约再更新快照。

## 新文件

- `mcp_server/models.py`
- `mcp_server/tools_creative.py`
- `mcp_server/tools_reference.py`
- `mcp_server/tools_generation.py`
- `mcp_server/tools_media.py`
- `CODEX_REPORT.md`

