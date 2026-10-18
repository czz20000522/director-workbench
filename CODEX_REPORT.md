# CODEX 执行报告

## 结果

任务基于 `9af6a40` 在 `codex/frontend-perf` worktree 完成。没有 push、合并、部署、后端改动或真实 GPU 生成。正常 UI 流程行为保持不变；新增内容集中在派生数据缓存、草稿落盘和任务/媒体失败恢复。

## Phase

| Phase | 提交 | 改动文件 | 行为变化 | 验证 |
| --- | --- | --- | --- | --- |
| 1 | `0ab2a15` | `src/App.tsx`、`src/app/hooks.ts`、`src/main.tsx`、错误边界组件（后在 Phase 4 内联） | 无 | `npm run build` |
| 2 | `65f4da0` | `src/App.tsx`、`src/app/hooks.ts` | 无 | `npm run build`；`client_restore.browser.mjs` 通过 |
| 3 | `d6fa260` | `src/App.tsx`、`src/app/hooks.ts` | 无 | `npm run build`；恢复驱动通过 |
| 4 | `633d32e` | `src/main.tsx`，移除 ignored 的 `src/components/ErrorBoundary.tsx` | 无 | 公开源代码与文档检查、public source 测试通过 |
| 5 | `1d84700` | `src/app/hooks.ts` | 无 | `npm run build`；完整 browser/core 已通过 |

Phase 1 将 `baseAssets`、计划素材、素材库、时间线和当前 Pipeline 等派生数组改为精确依赖的 `useMemo`；草稿写入改为 400ms 节流，并在 `pagehide`、页面隐藏和卸载时 flush；顶层错误边界显示中文重试入口并保留控制台异常日志。

Phase 2 抽出 `usePlayback` 与 `useProjectTasks`。播放初始化、连续播放、媒体错误和播放门控由播放 hook 管理；任务 hook 管理 HTTP 快照、任务/装配状态、SSE `onerror` 指数退避重连和批次状态所需的任务集合。

Phase 3 抽出 `useProjectSession` 与 `usePlan`，保留项目归属检查、计划 revision、装配结果和服务器状态刷新契约。

Phase 4 将错误边界内联到已纳入公开白名单的 `src/main.tsx`，避免新增 ignored 文件阻断公开源代码导出。

Phase 5 在 SSE 之外保留 15 秒一次的任务 HTTP 快照轮询，作为网络流断开时的兜底。

## 验证

- `npm run build`：通过，`tsc --noEmit` 与 Vite 构建均通过。
- `.\.venv\Scripts\ruff.exe check`：通过。
- `.\.venv\Scripts\python.exe tools\export_public_source.py --check`：通过，293 个文件、44 条相对链接。
- `.\.venv\Scripts\python.exe tools\check_public_docs.py`：通过。
- `.\.venv\Scripts\python.exe -m pytest tests/test_public_source.py -q`：8 passed、1 warning。
- `.\run_tests.ps1 -Suite browser`：通过，15 个 browser driver，报告 `browser=passed`。
- `.\run_tests.ps1 -Suite core`：1048 passed、4 skipped、1 warning、42 subtests passed。

没有新增测试文件；既有 browser 驱动覆盖草稿刷新持久化、任务恢复、媒体播放、错误门控和连续播放。

## 未完成项

无。

## 风险点

- 任务轮询间隔为 15 秒，SSE 正常时不会改变实时更新路径；SSE 断开时最多等待一个轮询周期。
- SSE 重连采用 500ms 起步、最多 10 秒的指数退避；页面卸载时会关闭连接和定时器。
- 错误边界重试会重新挂载工作台子树；异常本身仍写入浏览器控制台，业务草稿落盘仍由现有缓存机制负责。

## 实际改动文件

- `src/App.tsx`
- `src/app/hooks.ts`
- `src/main.tsx`
- `CODEX_REPORT.md`

最终没有保留新增源码文件；报告文件本身因仓库公开白名单规则使用了强制加入提交。
