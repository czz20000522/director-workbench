# 显式化合同测试迁移报告

基座：`codex/boundary-integration`（HEAD `2deb849`），分支 `codex/test-migration`。
任务：把 T1/T2/T3 合并后仍失败的 30 个 core 测试迁移到显式合同；不放宽断言，除非被验证的行为确已废除。

## 复现

```
.venv\Scripts\python.exe -m pytest tests/test_backend_module_split.py \
  tests/test_candidate_adoption.py tests/test_director_workbench_contracts.py \
  tests/test_plan_assembly_stale.py tests/test_reference_queue_safety.py \
  tests/test_reference_revision.py tests/test_reverse_workbench.py \
  tests/test_scheduler_app.py -q
# 30 failed, 91 passed
```

## 失败分类与处理

| 类别 | 结论 | 处理 |
| --- | --- | --- |
| `test_backend_module_split`（2） | **真实回归**：T2 新增的显式观察辅助函数/路由只定义在 `routers/reverse_analysis.py`，未按拆分约定从 `backend.app` 再导出，且路由内部裸调用，绕过 `setattr(backend, ...)` 补丁 | 修生产代码：`app.py` 再导出 `_reference_source`/`_append_observation`/`_has_explicit_frame_observation`/4 个观察路由；路由内改为 `backend.<名称>` |
| `test_candidate_adoption[wrong_receipt]`（1） | **真实回归**：`2deb849` 为让快速 UI 采用通过，删掉了“成功回执必须点名当前候选”的校验（非 media 任务完全跳过回执检查） | 修生产代码：恢复回执成员校验；仅容忍缺少 `status` 的旧回执（浏览器 fixture 形态），有 `status` 时仍拒绝失败执行。浏览器快速流程已实测通过 |
| OpenAPI 快照（1） | 合同变化是显式化的预期结果 | 对比旧 digest：仅新增 4 条 `observations/{range,sample,audio,asr}` POST 路由与 5 个请求 schema，无删除/改名；更新 SHA-256 |
| `test_plan_assembly_stale[convert]`（1） | 旧 fixture 未提交分镜，被新合同 422 | 改传显式 `shots`（含范围与字段）；仍验证转换使 assembly/review 失效 |
| `test_reference_queue_safety`（17） | fixture 仍走“上传自动创建 decomposition 任务”的旧流程（`import_task_id` 已删除）；这是旧行为，不是安全断言失效 | 上传 fixture 改为只登记；decomposition 用例直接构造遗留 `needs_reconcile` 任务行（升级后仍受支持的恢复路径），回执/恢复断言原样保留；semantic 用例改走显式帧观察门禁后启动 |
| `test_reference_revision` 过期转换（1） | 缺 `shots` 导致 422 先于 revision 冲突检查 | 两次转换都提交显式 `shots`；仍验证 stale 409、零写入、`source_revision` |
| `test_reverse_workbench`（6） | 4 条转换用例依赖“时间线自动推导镜头/performance/reference_frame/reviewed”；1 条语义用例断言模型结果覆盖已人工确认摘要；均属已废除的隐式自动行为 | 转换改用 `segment_id + 范围 + fields` 显式提交；断言 `reference_frame` 不再被推断、`source_note_status` 为 `pending_review`；模型结果断言落到 `model_candidate` 且 `user_status=unreviewed`，人工摘要不被覆盖 |
| `test_scheduler_app` 上传参考（1） | 旧测试断言上传持久化排队 decomposition 任务 | 改名并改断言：202 只登记、`observations/timeline` 为空、无 task 行、无 GPU 执行、调度器空转 |

`test_candidate_adoption.py` 本身未改测试（其断言仍有效，靠生产修复转绿）。

## 生产代码改动（最小）

- `backend/routers/reverse_analysis.py`：9 处裸调用改为 `backend.<名称>`，保持模块拆分后的 monkeypatch 语义。
- `backend/app.py`：补 7 个再导出；恢复采用门禁的成功回执校验（见上）。

## 提交

```
e69b186 fix(reverse-analysis): keep split-module contract and adoption receipt gate
dcae06f test(contracts): refresh OpenAPI snapshot for explicit reference observations
d12c675 test(reference): submit explicit shots and keep model output as candidate
346adf1 test(reference): exercise legacy decomposition receipts without auto upload
10c7042 test(scheduler): assert uploaded references register without a task
```

## 验证

```
.venv\Scripts\ruff.exe check <changed files>                  # All checks passed!
.venv\Scripts\python.exe -m pytest <8 个原失败文件> -q          # 121 passed
.venv\Scripts\python.exe -m pytest <参考/采用/MCP 相关 11 文件> -q  # 147 passed
node tests/quick_generation.browser.mjs <python> <runtime>     # PASS（含候选采用/恢复/装配）
```

未运行全量 core（另有并行代理处理挂起问题），未改动 `tools/select_tests.py`、`tools/run_project_tests.py`、`run_tests.ps1`、`pyproject.toml`、`requirements-dev.txt`。
