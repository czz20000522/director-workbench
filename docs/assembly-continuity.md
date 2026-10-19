# 装配接点自动连续性提示

`GET /api/projects/{project_id}/assembly/edit` 的响应包含 `continuity`。结果始终包含 `advisory: true`，用于帮助人工查看相邻分镜，不能替代接点审核，也不会自动通过、阻止或修改装配。

每个 `continuity.joins[]` 对应一个相邻接点。CPU 检查在已登记的视频上使用 `ffprobe` 读取分辨率、帧率、旋转、宽高比、时长、采样率和声道数；使用 FFmpeg 抽取左段末帧和右段首帧，并在 32×32 RGB 样本上计算平均亮度与平均颜色差异；两侧有音频时测量接点附近 0.25 秒窗口的 RMS 与峰值，提示可能的静音或削波。

这些值是可解释的启发式指标，不是艺术判断。`warnings` 只表示需要人工复核的信号，不能作为自动判分。媒体文件、FFmpeg 或 ffprobe 缺失时，接点返回 `status: "unavailable"`、`available: false` 和 `reason`；接口仍返回 200，原有装配和人工预检流程继续使用自己的规则。

前端在“接点与全片声音”面板显示“自动检查（仅供参考）”。人工审核按钮、`blocking_joins`、revision 校验和装配提交顺序保持原有语义。

# 跨分镜连续性检查（显式输入）

`GET /api/projects/{project_id}/assembly/continuity-check`（MCP `check_assembly_continuity`）返回 `read_only: true` 报告；`/assembly/edit` 与装配预检响应内嵌同一 `continuity_check` 字段。输入只来自工作台已保存的显式记录：

- 计划中分镜的显式顺序与起止时间；
- 逐分镜的 `explicit-adoption` 采用记录（#114 门禁语义不变）；
- 逐接点的当前版本审核记录和签名（顺序或视频版本变化会使旧结论过期）；
- `assembly_edit.sound` 中显式保存的 `segment_native`／`complete_master`／`mute`，或作品清单登记的全片音轨；
- 分镜显式绑定的参考素材（`reference_asset_id` 或 `source_analysis`）。

报告包含 `checks[]`（`code`、`category`、`severity`、`message`、`object`、`evidence`、`action`）与同内容的 `blockers[]`。阻断项包括：缺失明确采用、采用版本/文件/分镜内容变化、当前视频缺失或时长过期、显式顺序与保存起止时间不一致、接点未审/过期/需重做、音轨策略未显式选择或无效。参考/当前差异、旧成片已过期、非 WAV 母版时长未验证属于 `warning`；检查不使用艺术判断，也不会自动修复、自动选择候选、自动审核或自动采用。

未显式选择音轨方式时，装配预检与装配提交返回 `sound_policy_required` 阻断，不会静默回退到 `segment_native`。旧版项目若从未建立 `assembly_edit` 记录，未审接点报告为 `join_review_untracked` 提醒以保留既有装配兼容；一旦保存音轨或接点记录，接点审核按原 `blocking_joins` 语义成为阻断。装配提交在接点、采用、视频与音轨任一阻断时返回结构化 4xx 且不创建任务。
