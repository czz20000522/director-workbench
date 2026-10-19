# CODEX_REPORT

任务：T1 装配逐段显式采用 + 生成对象/批次/workflow 显式（Issue #114/#115）。
分支：`codex/boundary-explicit`。未 push、未合并、未启动工作台/ComfyUI、未执行真实 GPU 生成。

## Phase 提交

1. `c98b848` — `feat: require explicit generation targets and workflows`
   - 单段请求 `asset_id` 必填；批次 `asset_ids` 必须非空并且只使用请求值。
   - 删除单段/批次项目默认 workflow 与默认分镜回退；缺 workflow 返回结构化 `workflow_required` 422 blocker。
   - 同步 HTTP、MCP、readiness、前端批次勾选入口及测试夹具。
2. `7c8adf8` — `feat: gate assembly on explicit adopted candidates`
   - 显式采用记录冻结 task/version、候选文件 path/size/mtime 和 plan revision。
   - 装配 edit/preflight/readiness/正式提交统一阻止未采用、计划过期、版本不一致和候选文件变化。
   - 正式装配只绑定已通过采用门禁的候选；前端逐段显示采用状态和“打开并采用”入口。
   - 新增 `tests/test_assembly_adoption_gate.py`，并加入公开源码白名单。
3. 本次收尾提交：文档、MCP 说明、公开源码白名单及报告。

## 实际改动文件

`backend/app.py`、`backend/readiness.py`、`backend/routers/batch_assembly.py`、`backend/routers/task_entry.py`、`backend/routers/workbench_status.py`、`backend/schemas.py`、`mcp_server/tools_generation.py`、`mcp_server/README.md`、`src/App.tsx`、`src/AssemblyEditPanel.tsx`、`src/app/types.ts`、`src/readiness.ts`、`docs/public-api-guide.md`、`.gitignore`、`tests/support.py`、`tests/test_scoped_submission.py`、`tests/test_scoped_batches.py`、`tests/test_readiness.py`、`tests/test_assembly_edit.py`、`tests/test_assembly_adoption_gate.py`、`CODEX_REPORT.md`。

## 验证

- `.venv\Scripts\python.exe -m pytest -q`（目标 assembly/adoption/task/batch/readiness/MCP 集合）：`135 passed`。
- 追加显式契约回归：`41 passed`。
- `.venv\Scripts\python.exe -m ruff check backend mcp_server ...`：通过。
- `npm run build`：TypeScript 检查与 Vite 构建通过。
- `tools/run_project_tests.py --suite core --check --runtime D:/dw-runtime`：环境/入口检查通过；未启动服务。
- `tools/check_public_docs.py`：311 个公开文件、45 条相对链接通过。
- `tools/export_public_source.py --check`：311 个文件、2,925,181 bytes 通过。

## 约束与回滚

本次按 breaking canonical contract 执行，无兼容 shim、旧字段回退或临时备份。回滚使用 Git revert：先回退收尾提交，再按需回退两个 phase 提交。新测试文件原先受 `.gitignore` 排除，已显式加入公开白名单并强制纳入提交。
