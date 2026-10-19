# 音频编辑与全片母版后期

工作台保留两类 CPU 音频任务：`audio-edit` 用于不超过 120 秒的片段处理；`audio-master` 用于已经登记到当前作品的完整母版，最长 30 分钟且源文件不超过 2 GiB。两者共用 `tasks` 表、CPU 调度、冻结输入、幂等键、停止/待核对/恢复语义和私有媒体路由。

## 全片母版后期

HTTP：`POST /api/projects/{project_id}/audio-master-tasks`。请求必须使用当前作品的 `source_asset_id`，可选 `background_asset_id`；不能提交路径。参数范围如下：

| 参数 | 范围/默认值 |
| --- | --- |
| `target_lufs` | -24..-8，默认 -16 |
| `true_peak_dbtp` | -3..0，默认 -1 |
| `fade_in_seconds`、`fade_out_seconds` | 0..10 秒 |
| `trim_start_seconds` | 0..3600 秒 |
| `trim_duration_seconds` | 0.001..1800 秒；省略表示到源末尾 |
| `background_gain_db` | -60..12 dB，默认 -12 |
| `pad_silence` | 默认 false；必须显式为 true 才允许源或背景不足时补静音 |
| `ducking` | 可选的 `threshold_db`（-60..0）、`ratio`（1..20）、`attack_seconds`（0.001..1）和 `release_seconds`（0.01..2） |

输入仅允许 WAV、MP3、M4A、FLAC；任务创建时通过文件身份校验复制到冻结目录，并在执行前再次校验冻结副本身份。WAV 按原采样率和声道处理；非 WAV 输入由 FFmpeg 明确归一化为 48kHz 双声道 PCM16，实际归一化会写入回执。背景混音要求与归一化后的母版同采样率、同声道，不循环或猜测响度。无 ducking 时按 `background_gain_db` 混合；有 ducking 时母版作为 sidechain，attack/release 以采样级包络生效。处理产生新的 PCM16 WAV 候选，不覆盖输入。

完成回执包含 FFmpeg `loudnorm` JSON 实测的输入/输出真峰值（dBTP）、LUFS-I、LRA、实际采样率、声道、帧数、时长、削波样本数、采用的参数、归一化说明和 FFmpeg 版本；若真峰值超过目标会写入明确告警并保留为待审核候选。任务有全任务 deadline，超时或执行中止会保留目录并进入 `needs_reconcile`。候选登记在当前作品资产清单中，试听后可通过装配声音设置选择为 `complete_master`；任务成功不会自动采用。

MCP 工具为 `submit_audio_master`，本地 stdio 和远程 allowlist 使用同一 HTTP 路由。请求丢失回执时必须使用原幂等键查询任务，不得换键重发。

## 限制

自由多轨编排、循环背景、任意路径输入、自动内容验收仍不在范围内。报告和试听只能作为审核依据；完整母版的对白、音效、音色和尾部连续性仍需人工试听与装配预检。
