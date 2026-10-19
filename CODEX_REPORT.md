# Wave3-Audio-Review 修复报告

日期：2026-10-19
分支：`codex/wave3-audio-integration`

## Phase 提交

1. `9b5f701 fix(audio): harden master measurements and execution`
2. `b4f22d3 fix(audio-ui): persist master submissions and expose ducking controls`
3. 本报告提交：完成记录与验证结果

未 push、未合并、未启停服务、未使用真实 GPU、未删除用户数据。

## 已完成修复

- 使用 FFmpeg `loudnorm` JSON 实测 LUFS-I、LRA 和 true peak；回执区分 sample peak 与 dBTP，并报告目标、是否达标和超目标告警。
- `ducking.attack_seconds` / `release_seconds` 实现为采样级 attack/release 包络，保留参数、快照和测试。
- 冻结输入使用 `secure_files.checked_file` / `copy_checked`，请求记录源身份、大小和冻结副本身份；执行前复核冻结副本，竞态或副本变化会拒绝处理。
- 增加 30 分钟全任务 deadline、逐块取消检查和 FFmpeg 子进程 deadline；超时/执行中止保留任务目录，由路由进入 `needs_reconcile`。
- 明确非 WAV 契约：由 FFmpeg 归一化为 48kHz 双声道 PCM16，并在回执与文档中说明实际归一化；WAV 保留原采样率/声道。
- 完善回执校验：请求参数快照、输出身份、帧数/采样率/声道、时长、有限数值和取值范围、报告元数据均在候选登记前复核。
- 前端持久化全片母版幂等键和草稿，挂载/刷新后按原键恢复回执并继续轮询；补齐背景 ducking、阈值、比例、attack/release 控件和前端校验。

## 实际改动文件

- `backend/audio_master_tasks.py`
- `backend/routers/task_media.py`
- `src/AudioEditPanel.tsx`
- `docs/audio-edit-integration.md`
- `tests/test_audio_master.py`
- `CODEX_REPORT.md`

## 验证

- `ruff check backend tests`：通过。
- 目标 pytest：音频母版、音频编辑 API、静态 UI、安全文件共 43 passed、1 skipped。
- 新增母版测试覆盖 FFmpeg 实测字段、true peak、attack/release、超时保留目录和冻结副本变化。
- `npm run build`：TypeScript 与 Vite 构建通过。
- `node tests/queued_panels.browser.mjs`：通过。
- core gate：1087 passed、7 skipped、42 subtests passed。
- browser gate：通过，报告见 `browser.log`。
- public docs/source gates：通过，源代码语法及 45 个相对链接通过；public source 8 passed。

所有候选仍需通过工作台试听、人工审核和装配预检后采用；本报告不代表作品交付或自动验收。
