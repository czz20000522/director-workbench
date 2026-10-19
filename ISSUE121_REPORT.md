# Issue #121 交付报告：MCP 错误/幂等/分页/默认值与文档合同

分支 `codex/issue-121-mcp-contract`（基于 `codex/boundary-integration@b0e7c91`）。本文件由实现代理写入，供 Lead 复核后移除；不包含凭据、服务器地址或本机数据。

## 变更契约

- 变更类型：breaking（同一公开仓库内 producer、consumer、schema、文档、测试一次性更新；无旧路径兼容层）。
- canonical 错误形状：成功 `{ok:true,data}`；失败 `isError=true` 且同次调用带 `{ok:false,error:{code,message,kind,retryable,http_status?,detail?,outcome_unknown?}}`。`kind ∈ {business, argument, transport, outcome_unknown}`。schema 校验失败是唯一无结构化内容的 tool error（SDK 在工具执行前拒绝）。
- canonical 分页：`list_tasks`、`list_shot_versions`、`list_references`、`list_media` 的 `limit`/`offset` 有 schema 范围，工具 `_meta.pagination` 声明合同，响应带 `next_offset`。
- canonical 幂等标注：可重放原键的写入与按素材幂等登记/核对工具 `idempotent_hint=true`；无统一键的写入 `false`。
- 影响面：`mcp_server/*`、远程名单消费方（未改名单，仍 60/67）、`tests/test_mcp_*`、`mcp_server/README.md`、`docs/public-api-guide.md`、Skill 合同快照。
- 回退：`git revert` 本分支三个提交即可回到 `b0e7c91`；不改数据库/作品数据。

## 5 行摘要

1. 新增 `mcp_server/errors.py`：统一错误 union、`isError=true` + 结构化 `{ok:false,error}`、`ToolServer` 守卫，所有工具（含直接返回字典的错误）走同一出口；HTTP business 错误从后端 `detail.code` 取稳定 `code`。
2. 参数/schema 收口：分页范围、幂等键非空/长度/非空白、文本上限、数值范围、`submit_speech.mode` 等枚举在 MCP schema 拒绝，不再依赖后端 422；缺参返回明确 tool error。
3. annotation 按真实能力拆分：14 个可重放/幂等写入置 `idempotent_hint=true`，其余写入保持 false；只读工具 true；分页工具发布 `_meta.pagination`。
4. 文档修正：MCP README、HTTP API 指南、Skill 统一错误/分页/幂等说明，媒体交付改为 `media_ref` + `/api/projects/{project_id}/media/{token}`（`/media-file?path=`、`/media/{asset_name}` 仅本机 UI），工具数 60 远程 / 67 全量，附录补齐 #120 的 6 个工具与 7 个仅本机工具。
5. 新增 `tests/test_mcp_contract.py` 24 个契约测试（错误结构/传输/未知回执、schema 拒绝、分页边界与 meta、幂等 annotation、媒体错误不嵌套、文档工具名/数量/媒体 URL），并更新 schema 快照 hash。

## 主要改动文件

- `mcp_server/errors.py`（新增）：错误统一结构、normalize/finalize、`ToolInputError`、`ToolServer` 注册守卫、`PAGINATION_META`。
- `mcp_server/server.py`：`request`/`fetch` 使用统一错误；路径校验使用稳定 code；annotation 拆分为 `read_only` / `idempotent_write` / `write_record`，注册经过守卫代理。
- `mcp_server/media.py`：`inline_media` 使用统一错误，错误不再被包装进成功 data。
- `mcp_server/tools_creative.py`：`list_tasks` 分页范围与 meta；`preflight`/`create_shot`/`update_shot`/`arrange_shot`/`record_review`/`create_project`/`update_guide_settings` 约束；直接错误改走 `errors.error`。
- `mcp_server/tools_generation.py`：提交/批次/装配/关键帧/语音的键、文本、枚举、数值范围；14 个幂等/核对写入标注；去除被 schema 覆盖的重复 body 校验。
- `mcp_server/tools_media.py`：媒体任务键与数值范围、媒体引用长度/非空白；下载错误正确上抛；`list_media` 分页 meta；媒体任务 `idempotent_hint=true`。
- `mcp_server/tools_reference.py`：参考导入/观察/校订 annotation；`list_references` 分页 Schema 与 meta；超限/文件错误统一结构；`import_reference_from_material`、`reconcile_reference_analysis` 标注幂等。
- `tests/test_mcp_adapter.py`：schema 快照 hash（`d14d3db2…`）。
- `tests/test_mcp_contract.py`（新增）：错误/分页/幂等/文档契约测试。
- `mcp_server/README.md`、`docs/public-api-guide.md`、`.agents/skills/director-workbench/SKILL.md`：合同文档同步（Skill `1.1.0`）。
- `.gitignore`：白名单加入 `mcp_server/errors.py`、`tests/test_mcp_contract.py`、`ISSUE121_REPORT.md`。

## 错误合同（可复制给客户端）

```json
{"ok": false, "error": {
  "code": "plan_revision_conflict",
  "message": "计划版本已变化",
  "kind": "business",
  "retryable": false,
  "http_status": 409,
  "detail": {"code": "plan_revision_conflict", "message": "计划版本已变化", "retryable": false}
}}
```

- `kind=business`：认证后的业务路由拒绝；`code` 优先取 `detail.code`。
- `kind=argument`：适配器能表达但 schema 表达不了的参数/语义（如 `empty_changes`）。
- `kind=transport`：GET/媒体读取暂时失败，`retryable=true`。
- `kind=outcome_unknown`：非 GET 写入回执未知，`outcome_unknown=true`、`retryable=false`，先核对原回执。
- schema 缺失/类型/范围：SDK tool error（`isError=true`、无 structured content），文本列出被拒字段；不会发 HTTP。

## 验证命令与结果

- `ruff check mcp_server tests/test_mcp_adapter.py tests/test_mcp_contract.py`：PASS（All checks passed）。
- 基线（改动前，短 basetemp）：`pytest tests/test_mcp_adapter.py tests/test_remote_mcp.py tests/test_mcp_media_delivery.py`：PASS 27。
- Phase 1（错误合同）：同一批 + 采用/参考/提交/语音/关键帧/媒体工具/任务控制/脚本/装配/创建/模板/access/assembly-adoption：PASS 177。
- Phase 2（schema/annotation）：同上 166 个（不含 adapter 快照）+ adapter 复跑：PASS 11。
- Phase 4 新增契约测试：PASS 24。
- 最终目标批次（MCP + remote + media delivery + reference/采用 + assembly adoption gate，26 个文件）：**PASS 276，73.36s**（命令见下）。
- 未运行：全量 core/GPU/浏览器/host-tool 套件（按任务要求省去耗时与外部依赖）；未启动/停止工作台或 ComfyUI，未 push/合并。

最终目标批次命令（短 basetemp 规避 Windows 240 字符路径限制；产物在 `D:\t121g`，跑完已清理）：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_mcp_adapter.py tests/test_mcp_contract.py `
  tests/test_remote_mcp.py tests/test_mcp_media_delivery.py tests/test_mcp_adoption.py `
  tests/test_mcp_assembly.py tests/test_mcp_audio_edit.py tests/test_mcp_classification.py `
  tests/test_mcp_create_project.py tests/test_mcp_creative_template.py tests/test_mcp_keyframes.py `
  tests/test_mcp_media_tools.py tests/test_mcp_missing_execution.py tests/test_mcp_reference_analysis.py `
  tests/test_mcp_reference_write.py tests/test_mcp_script.py tests/test_mcp_speech.py `
  tests/test_mcp_submission.py tests/test_mcp_task_controls.py tests/test_mcp_access_config.py `
  tests/test_reference_explicit.py tests/test_reference_revision.py tests/test_reference_queue_safety.py `
  tests/test_reference_semantic.py tests/test_candidate_adoption.py tests/test_assembly_adoption_gate.py `
  -q --basetemp=D:\t121g -p no:cacheprovider
```

## 已知限制与未覆盖

- schema/tool error 的文本由 MCP SDK 生成，只有执行期错误带 `{ok:false,error}`；该边界已在 README/指南/Skill 明确为 union 的第五类。
- `_meta.pagination` 是本仓库合同扩展，非 MCP 标准字段；客户端以 `tools/list` 为准。
- 未改装配连续性、参考显式化、auto 语义、远程工具名单与业务路由；`tests/test_mcp_adapter.py` 快照 hash 是唯一因 schema 收口而更新的既有断言。
- 真实外部 Agent / GPU / 跨设备验收仍为 NOT_RUN，需由 Lead 在合并后安排。
