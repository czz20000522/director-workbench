# T4 报告：去除 readiness/推荐/智能命名兜底，补工具边界回归（Issue #118）

分支：`codex/t4-boundary`（基于 `codex/boundary-integration`，HEAD 起点 `2deb849`）
未 push、未合并、未启动/停止工作台或 ComfyUI、未跑真实 GPU、未删除用户数据。

## 结论

- readiness/state 不再按“第一个可执行 stage”或唯一 stage 静默选择。没有显式方案时返回结构化 `stage_required`；已登记 workflow/stage 无法解析到本作品已安装方案时返回 `recipe_unresolved`。两种 blocker 都带可选的显式方案事实（`installed_pipeline_stages`、`available_presets`、`installed_stages`）和 `GET .../pipeline` 入口，不写计划、不建任务。
- reusable template 目录删除了无依据的 `recommended` 字段，响应改为事实清单 `available_templates`（`id/kind/title/description/installed/preset`）。前端同步读取新字段，去掉“默认方案/推荐配置”暗示。
- 模型相关命名/文案改为观察与候选：模型 role 改为 `observation_candidates`，`model_route` 使用 `primary_multimodal_observations`，参考工件标题标为“可迁移机制候选/制作草案种子候选”，UI 明示“显式运行、输出模型候选、需人工校订”，不再宣称“统一完成视觉理解/剧情分析/提示词写作”。
- 工具边界：MCP `read_project` 新增 `section:"reusable-templates"`，可直接读取模板事实；`section:"state"` 返回同一 readiness。新增 7 条边界回归，覆盖不自动选 stage、不静默 fallback、不返回推荐、无自动工具入口、`approve_candidate` 必须显式确认、UI 文案不得宣称理解/推荐。
- 平台定位“确定性工具（锄头）”作为硬约束写入 `AGENTS.md` 与公开开发指南；HTTP API/MCP 文档同步。

## 主要改动

### 1) readiness 显式化（提交 `8201d17`）

- `backend/readiness.py`
  - 删除 `stages[0]` 单 stage 兜底；只接受 draft/segment 显式 stage、显式 workflow 解析结果。
  - 新增 `selection_options` 参数；无法解析时输出 `stage_required`/`recipe_unresolved` blocker，附带只读显式方案事实；不再调用 validator。
  - 未选方案时同时移除 `validate`/`submit` 动作。
- `backend/routers/workbench_status.py`
  - 新增 `readiness_selection_options()`：`available_presets`、`installed_stages`、`installed_pipeline_stages`（stage_id/title/workflow 事实）。
  - `project_readiness` 传入选择事实；删除 `selected_stages = {select_stage({}, stages)}` 的隐式选择残留。
- `tests/test_readiness.py`
  - 单 stage 不再自动选中；新增 `stage_required`/`recipe_unresolved` 覆盖；显式 stage 与 workflow 路径保持可用。

### 2) 删除推荐语义（提交 `c66f09d`）

- `backend/app.py`：`reusable_template_catalog` 返回 `available_templates`，删除三个 `recommended: true`；保留中性顺序和事实字段。
- `src/App.tsx`、`src/app/types.ts`：改读 `available_templates`，删除 `recommended` 类型字段；UI 文案改为“事实清单，不代替你选择”“未配置 · 需显式安装方案”。
- `src/QuickStudioPrototype.tsx`：`推荐配置` → `当前配置`。
- `ProductionStep.mode` 的 `'auto'` 分类改为 `'executor'`（仅 UI 阶段分类，不改变 auto 流程）。
- `tests/test_finish_tasks.py`：断言无 `recommended`、字段集合为事实字段。

### 3) 命名、文案与工具边界（提交 `87bb9dc`）

- `backend/routers/reverse_analysis.py`：模型 role → `observation_candidates`。
- `tools/reference_decompose.py`：`model_route` 键 → `primary_multimodal_observations`；工件标题改为“可迁移机制候选/制作草案种子候选”。
- `tools/reference_semantic.py`：提示词改用“可迁移机制候选/制作草案种子候选”表述，仍只要求 JSON 候选。
- `src/ReverseWorkbench.tsx`：模型状态说明、时间轴校订标签、页签、工件面板与提示文案改为“观察/模型候选/人工校订”；删除未使用的 `adaptation_advice` 来源标签。
- `mcp_server/tools_creative.py`：`read_project` 支持 `reusable-templates`，描述明确“事实清单，不含推荐或默认选择”。
- `tests/test_mcp_adapter.py`：更新 MCP schema 快照哈希（新增 section 导致）。
- `AGENTS.md`、`docs/public-development-guide.md`：新增长期硬约束“平台定位：确定性工具（锄头）”。
- `docs/public-api-guide.md`、`mcp_server/README.md`：补充 readiness 阻塞、`available_templates`、MCP `read_project` 显式读取合同与候选命名边界。

### 4) 边界回归与报告（本提交）

- 新增 `tests/test_boundary_regression.py`（7 条）：
  1. `GET .../state` 无显式 stage → `stage_required` + 方案事实，`validate/submit` 不可用，任务表为空；
  2. 已安装方案 + 未安装 workflow → `recipe_unresolved`，已安装方案只作为事实返回；
  3. `/api/models/reverse-analysis` role 为观察候选；
  4. 前端源码不出现“统一完成视觉理解/推荐配置/可直接复用默认方案/平台已理解/平台推荐”，并读取 `available_templates`；
  5. 参考命名不再出现旧 model role/model_route 键；
  6. MCP `read_project` 可读取 `reusable-templates`/`state`；
  7. MCP 工具名无 auto/recommend 入口，`approve_candidate` 必填 `confirm_adoption`。
- `.gitignore` 白名单加入 `tests/test_boundary_regression.py`、`T4_REPORT.md`。

## 实际改动文件

```text
AGENTS.md
backend/app.py
backend/readiness.py
backend/routers/reverse_analysis.py
backend/routers/workbench_status.py
docs/public-api-guide.md
docs/public-development-guide.md
mcp_server/README.md
mcp_server/tools_creative.py
src/App.tsx
src/QuickStudioPrototype.tsx
src/ReverseWorkbench.tsx
src/app/types.ts
tests/test_boundary_regression.py
tests/test_finish_tasks.py
tests/test_mcp_adapter.py
tests/test_readiness.py
tools/reference_decompose.py
tools/reference_semantic.py
.gitignore
T4_REPORT.md
```

## 验证（实际命令与结果）

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| lint | `.\.venv\Scripts\python.exe -m ruff check backend mcp_server tests tools` | PASS（All checks passed） |
| 局部回归 | `pytest tests/test_readiness.py tests/test_finish_tasks.py tests/test_boundary_regression.py -q` | PASS（34 + 4 + 7） |
| 受影响面 | `pytest tests/test_queue_readiness_integration.py tests/test_project_reads.py tests/test_private_workbench.py -q` | PASS（45 passed） |
| 参考/命名/MCP | `pytest tests/test_mcp_adapter.py tests/test_reference_semantic.py tests/test_reference_decompose.py tests/test_finish_tasks.py tests/test_readiness.py -q` | PASS（78 passed；同批 `test_reverse_workbench.py` 5 条旧合同失败见下） |
| 较大目标集 | `pytest <24 个相关测试文件> -q` | PASS 269，FAIL 1（`test_director_workbench_contracts::test_openapi_route_and_schema_snapshot`，基线同样失败） |
| 前端构建 | `npm run build` | PASS（tsc + vite，1.7s） |
| 公开文档/源码门禁 | `.\.venv\Scripts\python.exe tools\check_public_docs.py` | PASS（313 files / 46 links） |
| 公开导出回归 | `pytest tests/test_public_source.py -q` | PASS（含在较大目标集） |
| MCP schema 快照 | `test_mcp_adapter.py` | PASS（哈希已按新 section 更新） |

未运行项（按任务约束）：不运行全量 core/browser；不启动工作台或 ComfyUI。

### 已知基线失败（本分支未引入、未修改）

在未修改的 `codex/boundary-integration`（`2deb849`）上复现同样失败：

1. `tests/test_reverse_workbench.py` 5 条：convert 旧合同仍调用无 `shots` 的接口（T2 参考显式化后的旧测试）。
2. `tests/test_director_workbench_contracts.py::test_openapi_route_and_schema_snapshot`：OpenAPI 快照哈希与基线不符。
3. `tests/test_reference_queue_safety.py` 等参考队列 19 条：旧合同测试。

这些归属测试迁移代理；按 Lead 边界未修改参考/auto 核心测试。

## 残留/未处理

- 参考拆解内部 artifact id `production_seed`、seed 文件名 `production-seed.json` 未改名：`tests/test_reference_semantic.py`、`tests/test_reverse_workbench.py` 固定了这些内部标识，且 Lead 明确要求不碰参考内部标识与参考测试。对外文案已改为“制作草案种子候选”，新模型输出命名（`observations`/`model_candidates`/`draft_seed`）已在文档与 UI 语义中体现。
- `tools/reference_decompose.py` 的 CLI 辅助函数 `recommended_frame_count`（旧拆解 CLI 使用）未改名，同样被上述参考测试固定；产品上传/观察路径已不使用该函数。
- 浏览器验收（`*.browser.mjs`）与全量 core 未运行，按任务约束留给统一门禁。

## 风险与回退

- 风险级别：中。变更为 API/命名破坏性调整（`templates` → `available_templates`、readiness blocker 代码与 options、MCP `read_project` enum、MCP schema 哈希）。
- 回退：`git revert 8201d17 c66f09d 87bb9dc <本提交>`，或回到 `2deb849`。
- 兼容例外：无。未保留旧字段/旧行为别名。

## 提交

```text
8201d17 fix: readiness requires an explicit recipe instead of first stage
c66f09d fix: remove unsupported template recommendation and rename executor step
87bb9dc docs: describe observation candidates and deterministic tool boundary
<final>  test: add deterministic-tool boundary regressions and T4 report
```
