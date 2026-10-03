# 导演工作台 MCP 连接

MCP 是工作台 HTTP 业务的薄适配。账号权限、版本检查、任务、队列和媒体仍由同一后端处理；适配器不导入业务数据库，不直接调用模型，也不启动另一套调度。客户端业务流程见 [HTTP API 指南](../docs/public-api-guide.md)，服务端准备见[公开开发指南](../docs/public-development-guide.md)。

## 远程客户端

普通设备使用维护者提供的私人工作台 URL。先读取 `/.well-known/director-workbench.json` 和 `/agent/start`，再按需读取 `/agent/capabilities?group=creation` 或 `presentation`。这些发现入口不要求登录；详细 OpenAPI、作品、媒体和 MCP 需要本人认证。

1. 用 `POST /api/auth/login` 登录本人账号。登录响应 JSON 只含身份；从可信 HTTP 客户端的 Cookie 容器取得 `director_session`，保存在内存或安全会话容器。
2. 用 `GET /api/auth/me` 核对身份，然后配置同一工作台 URL 下的 `/mcp/`，传输方式为 Streamable HTTP，请求头为 `Authorization: Bearer <本人的会话>`。不要把会话值写进共享配置、提示词或日志。
3. 通过客户端工具发现读取实际 schema；先列本人作品，用实际返回的 ID 接续。会话过期、服务重启或切换账号后重新登录并更新连接身份。

服务器使用已固定的 `mcp==2.2.0`，远程接口为 stateless、JSON 响应，生命周期由私人工作台进程管理。首批支持可配置 Bearer 的 Streamable HTTP 客户端；没有 OAuth 登录发现，只能 OAuth 且不能配置现有 Bearer 的宿主尚未支持。远程客户端无需 Windows Python 或 SSH 管理员凭据。服务仍使用维护者已配置的私人网络或隧道。

**远程工具范围：**当前远程入口仅开放能力分组中的创作/页面工具以及 `read_guide_settings`，由 [agent_access.py](../backend/agent_access.py)过滤。包括列/建作品、读计划、保存分镜、上传、预检、单段提交、原键回执、队列/任务/历史、停止和页面动作。完整 stdio 工具集合不会自动全部暴露到远程入口；其他业务可通过认证 HTTP 使用。能力以当前部署发现结果为准。

`upload_material` 接受素材内容的 base64，解码后单文件至多 16 MiB；较大素材走原 HTTP multipart。远程素材应上传实际内容，不能把客户端路径传给服务器本地路径导入工具。上传不自动把图片绑定为首帧。

## 本机 stdio

stdio 供服务器本机客户端或主动诊断使用，需要项目 Python 环境及独立运行的私人 HTTP 服务。Python 依赖已包含 MCP，也可按同一依赖入口安装：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-mcp.txt
```

以下配置只含公开部署布局和 loopback 示例。按客户端机制替换实际解释器和脚本位置；不加入密码或会话值：

```json
{
  "mcpServers": {
    "director-workbench": {
      "command": "D:/Comfy-Desktop/ComfyUI-Workspace/projects/director-workbench/.venv/Scripts/python.exe",
      "args": ["D:/Comfy-Desktop/ComfyUI-Workspace/projects/director-workbench/mcp_server/server.py"],
      "env": {"DIRECTOR_WORKBENCH_URL": "http://127.0.0.1:4100"}
    }
  }
}
```

本机适配器只连接 loopback HTTP 地址。先按上面的账号登录流程获取本人会话，再通过可信客户端的启动环境向子进程传入 `DIRECTOR_WORKBENCH_SESSION`。只有 URL 没有会话时，私人业务返回 401。会话失效或账号切换后重新登录并重启 stdio 连接。

完整工具通过 stdio 的工具发现获取。常用能力如下；参数和允许动作以当前 schema 为准，不从名称推断授权：

| 能力 | 工具示例 |
| --- | --- |
| 作品、计划、队列与指南 | `list_projects`、`read_project`、`read_queue`、`read_guide_settings` |
| 剧本、分镜与预检 | `read_script`、`save_script`、`create_shot`、`update_shot`、`arrange_shot`、`preflight` |
| 单段/批次生成与原回执 | `submit_shot`、`submit_batch`、`get_submission_receipt`、`get_task` |
| 停止、核对与恢复 | `stop_generation`、`reconcile_generation`、`resume_generation` |
| 候选历史和显式采用 | `list_shot_versions`、`restore_shot_version`、`record_review`、`approve_candidate` |
| 图片、声音与音频编辑 | `get_keyframe_capability`、`submit_keyframe`、`get_speech_capability`、`submit_speech`、`submit_audio_edit` |
| 参考分析与人工校订 | `import_reference`、`read_reference`、`start_reference_analysis`、`correct_reference_artifact` |
| 装配接点、声音和成片 | `read_assembly_edit`、`review_assembly_join`、`set_assembly_sound`、`preflight_assembly_edit`、`submit_assembly` |
| 媒体加工和检查 | `get_media_capability`、`create_silence`、`inspect_video`、`transcribe_audio`、`export_silent_video` |

`import_reference` 读取 stdio 进程所在机器的绝对音视频路径，再 multipart 上传；不是读取远程客户机的文件。创作、媒体加工和审核应通过工作台入口完成，客户端不临时直跑 FFmpeg/Whisper 代替产品能力。

## 回执、版本与操作边界

- 业务结果为 `{ok: true, data: ...}` 或 `{ok: false, error: ...}`；MCP 传输成功不代表业务成功。HTTP 失败保留 `http_status` 和 `detail`，参数 schema 错误由 SDK 返回。
- 写入使用工具要求的当前版本：计划、剧本、参考分析和审核各有 revision。409 后重读比较，不自动提高版本覆盖。MCP 与工作台随同发布，不探测/静默回退旧软件流程。
- 提交生成前保存请求键。相同作品、键和参数返回原任务；不同参数冲突。写入超时可能标记 `outcome_unknown`，先查询原回执或保存内容，不盲目重发。创建、上传和新增分镜没有通用幂等保证，先读取核对。
- `stop_requested` 不等于停止完成；`reconcile_generation` 核对原执行，不自动重试。`resume_generation` 可能返回原成功任务或创建新尝试，使用实际返回 ID 接续。`resolve_missing_execution` 要求先核实旧执行已结束、检查输出并提供说明，不能仅凭超时调用。
- `approve_candidate` 需要真实候选、当前计划/审核版本、非空说明和严格布尔 `confirm_adoption=true`。任务成功或技术检查不自动批准画面、音色、表演、采用或交付；明确采用也不表示用户亲自验收。
- 页面工具只向本人允许引导的标签页发送表现动作；等待实际回执。`pending` 是待呈现，未呈现不能声称已点击或播放。表现层不重复业务保存或生成，手工接管/刷新不重放旧动作。

可分发创作 Skill 位于 [director-workbench/SKILL.md](../.agents/skills/director-workbench/SKILL.md)，当前服务也通过 `/agent/skill/SKILL.md` 提供同一内容。它描述业务使用方法，不含凭据或服务器部署地址，不自动安装到用户全局目录。

## 验证与反馈

协议回归见 [test_mcp_adapter.py](../tests/test_mcp_adapter.py)、[test_agent_access.py](../tests/test_agent_access.py)和各业务 `test_mcp_*`。完整离线检查使用[公开开发指南](../docs/public-development-guide.md)中的 `run_tests.ps1`，声音执行器使用自己的 AuK 环境。

客户端接入应分别验证发现、本人登录、工具 schema、本人数据读取、权限拒绝和无生成预检；实际部署、干净跨设备客户端、GPU 输出及内容验收分别记录。代码/离线测试不能证明所有宿主或制作组合可用。问题状态及剩余验收保存在 [GitHub Issues](https://github.com/czz20000522/director-workbench/issues)，维护流程见 [GitHub 协作约定](../docs/github-collaboration.md)。

当前远程 MCP 还保留 SDK 的 loopback Host/Origin 防护。建议客户端经已配置的 SSH 隧道使用 localhost 地址；HTTP 页面能直连 tailnet 不代表 `/mcp/` 接受该 Host。直连 tailnet 域名或反向代理需另行配置并验收允许的 Host/Origin；不能通过关闭这项防护解决 421/403，也不能把 HTTP 的 Origins 参数当作 MCP Host 配置。
