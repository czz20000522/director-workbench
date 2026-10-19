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

输入会在任务创建时复制到冻结目录，并在执行前再次校验采样率和声道。背景混音要求与母版同采样率、同声道，不自动重采样、循环或猜测响度。无 ducking 时按 `background_gain_db` 混合；有 ducking 时母版作为 sidechain，按限制参数压低背景。处理产生新的 PCM16 WAV 候选，不覆盖输入。

完成回执包含实际采样率、声道、帧数、时长、输入峰值、输出真峰值（dBTP）、LUFS-I、LRA、削波样本数、采用的参数和 FFmpeg 版本。候选登记在当前作品资产清单中，试听后可通过装配声音设置选择为 `complete_master`；任务成功不会自动采用。

MCP 工具为 `submit_audio_master`，本地 stdio 和远程 allowlist 使用同一 HTTP 路由。请求丢失回执时必须使用原幂等键查询任务，不得换键重发。

## 限制

自由多轨编排、自动重采样、循环背景、任意路径输入、自动响度猜测和自动内容验收仍不在范围内。报告和试听只能作为审核依据；完整母版的对白、音效、音色和尾部连续性仍需人工试听与装配预检。
