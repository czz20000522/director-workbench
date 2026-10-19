# Codex 报告：彻底废除 auto 创作模式（Issue #116）

分支：`codex/auto-removal`（基于 `codex/boundary-integration`，含 #114/#115）
未 push、未合并、未启动工作台或 ComfyUI、未跑真实 GPU、未删除用户数据。

## 结论

`creation_mode='auto'`、自动 `resolve_automatic_shot` 写回、自动 preset 安装/写 manifest、自动 fallback 和 `pipeline_stage_id='auto'` 已删除。创建项目必须显式传 `creation_mode='advanced'`；保存分镜必须显式给出 workflow / 已安装 preset，或使用作品已明确安装的方案；没有方案时返回结构化 `422` 与可选项，不写计划。不支持的画幅/首尾帧/声音/创作分区组合直接阻断，不再换配方。

## 主要改动

### 后端

- `backend/schemas.py`
  - `ProjectCreateRequest.creation_mode` 改为必填 `Literal['advanced']`，无默认值；旧客户端缺字段或传 `auto` 得到 `422`。
  - 分镜创建/修改新增 `preset_id`；`ShotGenerationRequest` 不再接受 `mode`（`extra='forbid'`）。
- `backend/shot_creation.py`
  - 删除 `resolve()`、`LIMITS`、`fallback_note` 等自动选择/fallback 逻辑。
  - 新增只读的 `resolve_plan`、`creation_options`、`normalize_generation`、`preset_for_workflow` 等：只把显式选择变成 workflow，并校验组合；不安装 preset、不写计划。
- `backend/routers/plan_editing.py`
  - 创建分镜不再调用 `resolve_automatic_shot`，不再写 `resolved_generation`。
  - 缺计划返回 `creation_plan_required`/`preset_not_installed` + `options`，不写计划。
  - 显式 preset/workflow 按作品已安装方案解析；非管理员可选择任意已安装方案，不能传任意工作流。
  - 参考拆解产生的无方案草稿仍可修改 prompt/场景；一旦提交 generation/workflow 才要求方案。
- `backend/app.py`
  - `configure_production_preset` 去掉 `automatic` 分支；`install_production_preset` 是唯一显式安装入口。
  - 删除 `resolve_automatic_shot`；删除创建项目时写 `default_creation_mode`，改存显式 `creation_mode`。
  - `select_preset_execution` 不再接受 `'auto'`，返回明确 `explicit_stage_required`。
  - 任务快照不再写/复制 `resolved_generation`。
- `backend/routers/task_entry.py`、`batch_assembly.py`、`workbench_status.py`
  - 删除 auto 分镜/auto stage 特判；显式 `auto` 返回 `422 explicit_stage_required`。
  - 预检不再从分镜隐式选择 stage；readiness 不再无 workflow 时取第一个可用 stage。
- `backend/guide.py`、`backend/workbench_status.py`
  - 删除 `creation_mode='auto'` 的指南分支与 readiness 字段。

### MCP

- `create_project` 必须显式传 `creation_mode='advanced'`。
- `preflight` 必须显式传 `stage_id`；`auto` 直接报错。
- `create_shot` 新增 `workflow`/`preset_id`；`update_shot` 的 `ShotChanges` 新增 `workflow`/`preset_id`。
- `GenerationSettings.mode` 删除。
- MCP schema 快照哈希更新。

### 前端

- 删除自动创作入口、自动方案切换按钮、`autoCreation` 分支和简化自动分镜面板。
- 创建项目与快速模板创建都显式发送 `creation_mode:'advanced'`。
- 添加分镜与快速视频表单新增必选“制作方案”下拉；无已安装方案时直接显示显式安装 picker。
- 生成/预检只使用当前分镜已保存的 workflow 或用户显式选择的 stage，不再默认取第一个视频步骤。
- 项目/分镜类型去掉 `default_creation_mode` 与 `generation.mode`。

### 文档与测试

- `docs/public-api-guide.md`、`mcp_server/README.md` 更新为显式合同。
- `tests/test_automatic_creation.py` → `tests/test_explicit_creation.py`，覆盖：
  - 缺 `creation_mode`/缺方案不写计划；
  - 显式 auto stage 被拒绝；
  - 不支持组合阻断、不 fallback；
  - 显式文本/首尾帧/锁定对白方案正常预检与提交；
  - 无可选方案、preset 未安装的阻塞；
  - 恢复旧候选不再保留后续 generation 设置；
  - 配方身份修复仍保留。
- `tests/automatic_creation.browser.mjs` → `tests/explicit_creation.browser.mjs`，驱动改为“缺方案阻塞 → 显式安装 → 显式保存 → 显式 stage 提交”。
- 既有测试按显式合同安装/传入方案；`tests/support.py` 的测试夹具显式安装 `h3-guided`（仅测试设置，产品不代选）。

## 验证

按任务要求逐项执行：

1. `ruff check backend mcp_server tests tools` → All checks passed.
2. 目标 pytest：
   `test_explicit_creation.py`, `test_adult_profiles.py`, `test_production_presets.py`,
   `test_first_frame_preset.py`, `test_locked_audio_preset.py`, `test_mcp_adapter.py`,
   `test_mcp_create_project.py`, `test_mcp_submission.py`, `test_mcp_script.py`,
   `test_mcp_reference_write.py`, `test_remote_mcp.py`, `test_private_workbench.py`
   → **181 passed**.
3. `npm run build` → passed（tsc + vite）。
4. 浏览器（短 runtime 根，避免 Windows 240 字符限制）：
   `tests/explicit_creation.browser.mjs` → passed。验证了缺方案不写计划、显式安装一次、任务提交使用 `motion-text-native-landscape-generation` 而非 `auto`、快照无 `resolved_generation`。
   `tests/quick_generation.browser.mjs` → 未通过，阻塞在既有 #114 采用门禁：新增 S02 后计划 revision 变化，S01 之前的显式采用变为 stale，装配页显示“尚未有带候选身份的明确采用记录”。该驱动在基础分支同样依赖这条行为；本任务未改装配/采用流程。需要后续单独决定“计划编辑后是否自动保留/要求重新采用”。
5. 核心全量（排除 `tests/test_secure_files.py`）：
   `pytest tests tools/test_build_video_finish_workflow.py --ignore=tests/test_generate_auk_flash.py --ignore=tests/test_secure_files.py`
   → **1068 passed, 7 skipped, 42 subtests passed**。
   说明：`tests/test_secure_files.py` 的 anyio `VerifiedFileResponse` 用例在 `D:`/当前环境会挂起；已在未修改的基线提交 `feecd68` 上复现同样挂起，属于既有环境/测试问题，不是本分支引入。单独运行前 5 个同步用例通过，异步流式响应用例挂起。
6. `run_tests.ps1 -Suite core -Check` → `core=passed`。
7. 公开源码门禁 `test_public_source` 在核心全量中通过（`.gitignore` 白名单已同步重命名文件）。

## 残留/后续

- 仓库中仍出现的 `'auto'` 仅有两类：媒体语言枚举 `zh/en/auto`，以及显式拒绝分支 `explicit_stage_required`；不再有创作模式默认或自动方案选择。
- `tests/quick_generation.browser.mjs` 的采用 stale 失败属于 #114 采用门禁与计划 revision 的交互，建议在后续 Issue 中明确“编辑计划后采用记录是否保留”。
- 未处理装配/参考流程本身的改动（按任务约束）。

## 实际改动文件

后端：`backend/app.py`、`backend/guide.py`、`backend/schemas.py`、`backend/shot_creation.py`、`backend/workbench_config.py`、`backend/routers/batch_assembly.py`、`backend/routers/plan_editing.py`、`backend/routers/task_entry.py`、`backend/routers/workbench_status.py`

MCP：`mcp_server/README.md`、`mcp_server/creation_inputs.py`、`mcp_server/models.py`、`mcp_server/tools_creative.py`

前端：`src/App.tsx`、`src/app/types.ts`、`src/data.ts`、`src/generationStage.ts`

文档：`docs/public-api-guide.md`

测试/工具：`.gitignore`、`tools/run_project_tests.py`、`tests/test_explicit_creation.py`（原 `test_automatic_creation.py`）、`tests/explicit_creation.browser.mjs`（原 `automatic_creation.browser.mjs`）、`tests/explicit_creation_bridge.py`（原 `automatic_creation_bridge.py`）、`tests/quick_generation.browser.mjs`、`tests/quick_generation_bridge.py`、`tests/support.py`，以及 `tests/test_adult_profiles.py`、`test_assembly_edit.py`、`test_candidate_adoption.py`、`test_candidate_review_invalidation.py`、`test_creative_inspection.py`、`test_director_workbench_contracts.py`、`test_enhancement.py`、`test_first_frame_preset.py`、`test_guide_joins.py`、`test_locked_audio_preset.py`、`test_material_reuse.py`、`test_mcp_adapter.py`、`test_mcp_create_project.py`、`test_mcp_creative_template.py`、`test_mcp_script.py`、`test_plan_assembly_stale.py`、`test_plan_revision.py`、`test_private_workbench.py`、`test_production_presets.py`、`test_project_create_selection.py`、`test_project_reads.py`、`test_queue_readiness_integration.py`、`test_reliability_audit.py`、`test_remote_mcp.py`、`test_scoped_assembly.py`、`test_scoped_batches.py`、`test_scoped_creative.py`、`test_scoped_records.py`、`test_scoped_submission.py`、`test_scoped_task_actions.py`、`test_screenplay.py`。

## 提交

```text
4a7cf3e feat: remove automatic creation plans and require explicit selection
014b265 feat(mcp): require explicit creation mode, plan, and stage
976e338 feat(web): require an explicit production plan before shots
1b95808 test: cover explicit creation plans and remove auto coverage
6d48f80 docs: document explicit creation plans and remove auto mode
```
