# CODEX 交付报告

## 范围与提交

本 worktree 在 `codex/perf-repair` 分支完成 B3、B1、B2、B4，未 push、未合并、未启动工作台或 ComfyUI。

## Phase B3：SQLite 基础设施与幂等唯一性

- 提交：`5f8ab08`、`a88ac6a`
- 文件：`backend/task_store.py`（新文件）、`backend/app.py`、`backend/routers/task_entry.py`、`tests/test_submission_idempotency.py`
- 行为变化：无；保留现有幂等回执、批量共享幂等键和任务冻结语义。
- 实现：统一 WAL、`synchronous=NORMAL`、`busy_timeout`；可重复迁移；独立幂等映射表与触发器；旧库重复键保留最早任务作为 canonical 回执，同 batch 任务继续共享键。
- 验证：`ruff check` 通过；`tests/test_submission_idempotency.py` 4 passed，覆盖迁移回填和并发同键只落一条任务。

## Phase B1：读路径缓存与批量 queue 读取

- 提交：`ca25702`
- 文件：`backend/app.py`、`backend/routers/workbench_status.py`、`backend/routers/project_catalog.py`、`backend/routers/task_entry.py`、`backend/task_scheduler.py`
- 行为变化：无；响应字段、状态语义、项目归属和队列位置语义保持不变。
- 实现：按当前主体与 project 组成缓存键；以 plan 文件 mtime、project record revision、task revision token 失效；readiness 和 project state 使用短期进程内缓存；queue 信息一次快照、内存分组，避免逐任务全表扫描。
- 验证：`tests/test_project_reads.py`、`tests/test_queue_readiness_integration.py`、`tests/test_readiness.py` 共 55 passed；`ruff check` 通过。

## Phase B2：调度器锁与网络调用分离

- 提交：`56b92d3`
- 文件：`backend/task_scheduler.py`、`tests/test_task_scheduler.py`
- 行为变化：无；公平轮转、单 GPU/CPU worker 上限、停止/恢复和失败回收保持不变。
- 实现：新增 tick 串行锁；锁内只做快照、依赖判断和条件 claim，资源探测、重校验、等待记录和 runner 启动在锁外执行；二次条件更新防止竞态双派发。
- 验证：scheduler/app/restart 定向套件 45 passed；锁可在慢重校验期间被 API 取得的回归测试通过；`ruff check` 通过。

## Phase B4：执行监控超时与结构化日志

- 提交：`c4b9762`
- 文件：`backend/app.py`、`backend/workbench_config.py`、`tests/test_comfy_execution_reporting.py`
- 行为变化：无；未提交前仍为 failed，提交后超时/不确定结果仍进入 `needs_reconcile`。
- 实现：新增 `DIRECTOR_TASK_EXECUTION_TIMEOUT_SECONDS`（默认 1800 秒）和 `DIRECTOR_TASK_MONITOR_POLL_SECONDS`（默认 2 秒）；统一监控循环的总超时检查；日志仅包含 task_id、project_id、阶段、phase、耗时和错误类别，不写入提示词；超时复用原有回执状态机。
- 验证：Comfy 执行、监控不确定性、缺失执行测试 48 passed；超时回收和日志隐私回归通过；`ruff check` 通过。

## 最终验证

- `ruff check`：通过。
- `npm run build`：通过（TypeScript 与 Vite build）。
- `./run_tests.ps1 -Suite browser`：通过，15 个 browser 驱动全部完成。
- 定向后端回归：上述各 phase 测试全部通过；另有 76 项收尾相关测试 75 passed、1 skipped。
- `tools/run_project_tests.py --suite core`：首次运行在 751 passed、2 skipped 后由 `tests/test_public_source.py` 失败；失败原因仅为新文件 `backend/task_store.py` 尚未加入公开 `.gitignore` 白名单。按任务约定未修改 `.gitignore`，待 Lead 补白名单后重跑。
- `tools/export_public_source.py --check`、`tools/check_public_docs.py`：同一公开白名单原因失败，未伪造通过。

## 未完成项与风险

- Lead 需要把 `backend/task_store.py` 加入公开源码白名单，然后重跑公开源码检查和完整 core。
- 完整 core 排除公开门禁的长跑在接近尾段无输出，已终止；phase 定向测试和 browser suite 均通过，建议白名单修复后由 Lead 在干净运行根重跑完整 core。
- 未进行真实 GPU 生成、部署、重启或生产验收。

## 新文件

- `backend/task_store.py`：SQLite 初始化、迁移和幂等映射基础设施；本次已 force-add，待 Lead 补公开白名单。
- `CODEX_REPORT.md`：本报告。
