# CODEX_REPORT

任务：Wave3-B2 高级全片音频后期
分支：`codex/wave3-audio`

## Phase 提交

- Phase 1：`e284032` — 新增 `audio_master` CPU 任务、冻结输入、30 分钟/2 GiB 限制、PCM16 WAV 输出、响度/峰值/削波回执、作品候选登记，并接入共享 CPU 调度和待核对语义。
- Phase 2：`58c613c` — 背景混音与受限 ducking 测试、文档、公开源代码白名单。
- Phase 3：`2dcd6da`、`ee86c27`、`636c00f` — HTTP/MCP/远程 allowlist、AudioEditPanel 表单/进度/试听/选择完整母版、OpenAPI/MCP 契约和文档；修正模块拆分代理调用契约。

## 实际改动

新增 `backend/audio_master_tasks.py`、`docs/audio-edit-integration.md`、`tests/test_audio_master.py`；更新后端路由/模式/调度/任务核对、`mcp_server/tools_media.py`、远程 MCP allowlist、AudioEditPanel/AssemblyEditPanel、公开 API/MCP 文档及契约测试。

## 验证

- ruff：通过。
- 音频/媒体/MCP/API 针对性测试：119 passed，2 skipped。
- 额外全片母版测试：4 passed；模块拆分测试：3 passed。
- core：1073 passed，5 skipped。
- browser：passed。
- `npm run build`：通过。
- `export_public_source.py --check`：310 files checked。
- `check_public_docs.py`：310 files、45 relative links passed。

## 剩余限制与未做项

- 长音频上限为 30 分钟或 2 GiB，输出为单/双声道 PCM16 WAV；背景必须是当前作品已登记音频且采样率/声道一致。未实现自动重采样、循环背景、自由多轨、复杂自动化或任意路径输入。
- 非 WAV 输入依赖本机 FFmpeg 解码；响度报告使用确定性测量与应用，仍需人工试听和装配预检确认尾部、对白、音色和表演。
- dubbing 仍沿用既有短时声音生成/音频任务边界；本任务未实现 30 分钟级配音生成、多人声对齐、自动对白切分、长时口型同步或自动验收。
- 未启动服务、未使用真实 GPU、未删除用户数据、未 push、未合并。
