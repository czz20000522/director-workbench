# Wave3-Audio-Review-2 修复报告

日期：2026-10-19
分支：`codex/wave3-audio-integration`
范围：仅当前 worktree；未 push、未合并、未启停服务、未使用真实 GPU、未删除用户数据。

## 完成内容

- 后端 loudnorm 回执现在要求完整且有限数值字段；缺失、非法、非有限值会失败，明确的 `-inf` 只按静音指标记录。回执校验严格验证真峰值目标、告警和静音元数据关系。
- FFmpeg 处理、版本查询、写回执和最终成功返回统一受 deadline/cancel 检查保护；超时会终止子进程并保留任务目录，由上层进入 `needs_reconcile`。
- 创建、冻结和执行统一允许 `.wav/.mp3/.m4a/.flac`；非 WAV 通过 FFmpeg 归一化为 48kHz 双声道 PCM16，规则同步到前端、MCP 和文档。
- 音频编辑与全片母版前端停止在 `needs_reconcile` 轮询，提供“核对结果”和“确认执行已结束”操作；核对得到终态后才清除持久化 pending 幂等键。
- 增加缺失/非法 loudnorm JSON、非零 FFmpeg、版本查询 deadline、严格真峰值、格式 allowlist、静音指标和 needs-reconcile 恢复测试；更新 MCP schema 快照。

## Phase 提交

1. `210aa32 fix(audio-master): reject incomplete metrics and bound execution`
2. `ea2c71a fix(audio-ui): reconcile timed out master submissions`
3. `798ad5f docs(audio): align MCP master normalization contract`

## 验证

通过：

- `.venv/Scripts/python.exe -m ruff check .`
- `npm run build`
- `pytest tests/test_public_source.py tests/test_mcp_adapter.py tests/test_audio_master.py tests/test_audio_edit_api.py`：49 passed
- `pytest tests/test_scheduler_restart.py -k accepted-response-lost`：1 passed
- `node tests/queued_panels.browser.mjs`：通过，覆盖母版 `needs_reconcile` 核对并清理 pending

core 全量首跑为 1095 passed、7 skipped，另有 1 个既有 scheduler restart 用例因测试驱动回执线程 `JSONDecodeError: Extra data` 超时；同一用例单独重跑通过。browser 全量未宣称通过：短运行根模式下既有 `guide_joins.browser.mjs` 硬断言配置 runtime 根，默认数据根模式下既有 `automatic_creation`、`enhancement`、`quick_generation` fixture/素材隔离失败；其余 browser 驱动通过。上述失败均未触及本次修改文件。

## 实际改动文件

- `backend/audio_master_tasks.py`
- `tests/test_audio_master.py`
- `src/AudioEditPanel.tsx`
- `tests/queued_panels.browser.mjs`
- `mcp_server/tools_media.py`
- `tests/test_mcp_adapter.py`
- `docs/audio-edit-integration.md`
- `CODEX_REPORT.md`
