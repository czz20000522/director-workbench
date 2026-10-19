# 装配接点自动连续性提示

`GET /api/projects/{project_id}/assembly/edit` 的响应包含 `continuity`，也可以通过只读的 `GET /api/projects/{project_id}/assembly/continuity` 单独读取。结果始终包含 `advisory: true`，用于帮助人工查看相邻分镜，不能替代接点审核，也不会自动通过、阻止或修改装配。

每个 `continuity.joins[]` 对应一个相邻接点。CPU 检查在已登记的视频上使用 `ffprobe` 读取分辨率、帧率、旋转、宽高比、时长、采样率和声道数；使用 FFmpeg 抽取左段末帧和右段首帧，并在 32×32 RGB 样本上计算平均亮度与平均颜色差异；两侧有音频时测量接点附近 0.25 秒窗口的 RMS 与峰值，提示可能的静音或削波。

这些值是可解释的启发式指标，不是艺术判断。`warnings` 只表示需要人工复核的信号，不能作为自动判分。媒体文件、FFmpeg 或 ffprobe 缺失时，接点返回 `status: "unavailable"`、`available: false` 和 `reason`；接口仍返回 200，原有装配和人工预检流程继续使用自己的规则。

前端在“接点与全片声音”面板显示“自动检查（仅供参考）”。人工审核按钮、`blocking_joins`、revision 校验和装配提交顺序保持原有语义。
