# ISSUE1_REPORT — 长视频装配：跨分镜连续性检查 + 统一音轨显式化

- Repo: `director-workbench-issue1`（branch `codex/issue-1-long-video`，base `codex/boundary-integration@2985740`）
- Issue: #1（DW-011），关联既有合同 #114（显式采用门禁）、#115（生成对象显式）、#117（参考显式）、#118（无兜底/无推荐）
- 状态：代码与离线验证完成；真实 GPU 装配/成片听看验收 `NOT_RUN`（本代理不启动工作台/ComfyUI、不跑真实 GPU、不 push/合并）

## 变更契约

### 1) 跨分镜连续性检查（只读）

- 新增 `backend/assembly_continuity_check.py`：只消费工作台已保存的显式记录，输出 `checks[]`（`code`/`category`/`severity`/`message`/`object`/`evidence`/`action`）与同内容的 `blockers[]`。
  - 输入：计划显式分镜顺序与起止时间、`explicit-adoption` 采用记录、版本绑定接点审核、`assembly_edit.sound`（或作品级 legacy 全片音轨）、分镜显式参考绑定。
  - 阻断（blocker）：`segments_required`、`segment_order_invalid`、`segment_timing_mismatch`、`video_required`、`video_duration_stale`、`adoption_required`/`adoption_plan_stale`/`adoption_candidate_changed`/`adoption_version_stale`、`join_review_required`/`join_review_stale`/`join_redo_required`/`join_video_required`、`sound_policy_required`/`sound_policy_invalid`。
  - 提醒（warning，不阻断）：`join_review_untracked`（旧版未建立 `assembly_edit` 的兼容提醒）、`reference_unavailable`/`reference_current_difference`/`reference_current_unmeasured`、`sound_duration_unverified`、`previous_assembly_stale`、`segment_duration_missing`。
  - 报告 `read_only: true`、`automatic_actions: false`；不写计划、不修复、不选候选、不审核、不采用；证据只含分镜/接点 ID 和媒体事实，不含绝对路径。
- `GET /api/projects/{project_id}/assembly/continuity-check` 返回该报告；`/assembly/edit` 与 `/assembly/preflight` 响应内嵌同一 `continuity_check`。
- MCP 新增只读工具 `check_assembly_continuity(project_id)`，并加入远程 MCP 允许清单。

### 2) 统一音轨显式选择与装配前校验

- `sound.selected` + `sound.selection_source` 明确区分“已显式保存/作品登记”与“未选择”。未选择时预检与 `POST .../assembly` 返回 `sound_policy_required` 阻断，不再静默回退 `segment_native`。
- `segment_native` / `complete_master` / `mute` 仍按既有语义进入构建器；`mute` 沿用构建器既有“移除音轨输入”路径。
- 作品级 legacy 全片音轨补充文件存在性校验；无效/短母版仍按原规则拒绝。
- 装配提交改为结构化 4xx：`assembly_continuity_blocked`（含 blockers）或 `assembly_snapshot_invalid`，不再把音轨/取片输入错误包成 500。

### 3) 保持不变

- #114 采用门禁语义不变：`assembly_adoption_blockers` 代码与判定未改，提交时仍先按 `assembly_adoption_required` 阻断。
- 未新增 auto/默认 stage，未改 `mcp_server/tools_reference.py`、参考显式化、auto 流程或测试影响面工具。
- 旧版未建立 `assembly_edit` 的项目接点仍是兼容状态（报告为 warning）；一旦保存音轨或接点记录，按既有 `blocking_joins` 语义阻断。

### 4) 前端

- `AssemblyEditPanel` 新增“跨分镜连续性检查（只读）”区块：通过/阻断计数、检查项分类、阻断原因、显式操作（查看接点、打开并采用、打开分镜核对参考、选择音轨方式、重新检查）。
- 音轨下拉新增“未选择（装配会被阻断）”显式状态；未选择时显示阻断提示，保存按钮在未选择时不可提交。
- `tests/assembly_edit.browser.mjs` 离线 Mock 覆盖新面板文本、未选音轨提示、接点审核、母版选择与无 GPU 预检。

## 提交（逐 phase）

| commit | 内容 |
| --- | --- |
| `63725a1` | `feat: add read-only cross-shot continuity check and explicit sound gate` |
| `7ae4bb4` | `feat: expose cross-shot continuity check through MCP, HTTP docs and contracts` |
| `9a9d2ab` | `feat: show cross-shot continuity check and explicit sound choice in assembly panel` |
| 第 4 个提交 | 回归测试与本报（`tests/test_assembly_continuity_check.py`、受影响断言、`ISSUE1_REPORT.md`） |

## 实际改动文件

后端/合同：`backend/assembly_continuity_check.py`（新增）、`backend/app.py`、`backend/routers/batch_assembly.py`、`backend/remote_mcp.py`、`mcp_server/tools_generation.py`、`mcp_server/README.md`、`docs/assembly-continuity.md`、`docs/public-api-guide.md`、`.gitignore`（新增文件白名单）。
前端：`src/AssemblyEditPanel.tsx`、`src/AssemblyEditPanel.css`。
测试：`tests/test_assembly_continuity_check.py`（新增）、`tests/test_assembly_edit.py`、`tests/test_guide_joins.py`、`tests/test_queue_readiness_integration.py`、`tests/test_reliability_audit.py`、`tests/test_mcp_adapter.py`、`tests/test_remote_mcp.py`、`tests/test_director_workbench_contracts.py`、`tests/assembly_edit.browser.mjs`。

## 验证（命令与结果）

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| Ruff | `.venv\Scripts\python.exe -m ruff check backend\assembly_continuity_check.py backend\app.py backend\routers\batch_assembly.py backend\remote_mcp.py mcp_server\tools_generation.py tests\test_assembly_continuity_check.py tests\test_assembly_edit.py tests\test_guide_joins.py tests\test_queue_readiness_integration.py tests\test_reliability_audit.py tests\test_mcp_adapter.py tests\test_remote_mcp.py tests\test_director_workbench_contracts.py` | PASS |
| 前端类型+构建 | `npm run build` | PASS（`tsc --noEmit` + vite build） |
| 聚合回归 | `.venv\Scripts\python.exe -m pytest tests/test_assembly_edit.py tests/test_assembly_continuity_check.py tests/test_assembly_adoption_gate.py tests/test_assembly_builder.py tests/test_mcp_assembly.py tests/test_mcp_adapter.py tests/test_director_workbench_contracts.py tests/test_remote_mcp.py tests/test_guide_joins.py tests/test_reliability_audit.py tests/test_scoped_assembly.py tests/test_plan_assembly_stale.py tests/test_readiness.py tests/test_segment_versions.py tests/test_candidate_review_invalidation.py tests/test_creative_inspection.py tests/test_backend_module_split.py "tests/test_queue_readiness_integration.py::test_missing_assembly_builder_blocks_public_preflight_state_and_submission" -q --basetemp=runtime\pytest-issue1-main2` | PASS：182 passed |
| 新增检查回归 | 含在上条：缺采用、接点未审/过期、音轨未选/无效、顺序/时长变化、缺视频、参考差异/不可测、read-only 无写入、MCP/HTTP 报告一致、显式选择后的通过路径与提交成功 | PASS：13 passed |
| 离线浏览器 | `node tests/assembly_edit.browser.mjs`（全部 HTTP Mock，无真实服务/GPU） | PASS：`Assembly edit browser: join review, registered master selection and no-GPU preflight passed` |
| OpenAPI/MCP digest | 重新计算并更新 `tests/test_director_workbench_contracts.py`、`tests/test_mcp_adapter.py` 期望值；对应测试在上条聚合中通过 | PASS |

### 已确认的既有失败（非本次改动）

`tests/test_queue_readiness_integration.py` 全文件当前还有 4 个与本改动无关的既有失败（上传素材返回 403 “素材不属于当前工作区”）：`test_busy_queue_receipt_limits_idempotency_cancel_and_user_isolation`、`test_global_queue_quota_is_atomic_and_original_receipts_survive_limit`、`test_http_batch_and_mcp_middle_arrival_rotate_at_task_boundary`、`test_real_mcp_reads_and_writes_same_readiness_guide_and_queue`。已将改动 stash 后在 base `2985740` 复现 `test_busy_queue_receipt_limits_idempotency_cancel_and_user_isolation` 同样失败，判定为环境/既有问题；本次只运行该文件中直接相关的 `test_missing_assembly_builder_blocks_public_preflight_state_and_submission`（PASS）。

### NOT_RUN / 剩余限制

- `NOT_RUN`：真实 GPU 装配、成片时长/音画同步核对、逐接点听看、声音替换后的最终听感验收（本代理按要求不启动工作台/ComfyUI、不跑真实 GPU）。
- `NOT_RUN`：全量 `core` / `browser` suite（按任务由其他代理/CI 负责）。
- 未部署、未 push、未合并；`dist` 等本地构建产物未提交。
- 参考/当前差异只报告可测媒体事实（宽高/帧率/时长），不是画面艺术判断；不可测时明确标记 `reference_current_unmeasured`，不宣称一致。
- 旧版未建立 `assembly_edit` 的项目，未审接点仍为 warning 以保留既有装配兼容；若后续要求该场景也阻断，需要单独确认兼容策略。

## Lead 收尾提示

`ISSUE1_REPORT.md` 与其 `.gitignore` 白名单行（`!/ISSUE1_REPORT.md`）可按约定在最终合并前移除。
