# HTTP API 与 Agent 接入

客户端使用维护者提供的私人工作台 URL 和本人账号，不需要服务器源码、Windows 路径、SSH 管理员凭据或 ComfyUI 地址。服务端部署见[开发指南](public-development-guide.md)，MCP 配置见[连接说明](../mcp_server/README.md)。

本指南描述公开代码的接入合同；实际部署版本、模型就绪和产品验收以当前服务返回值及 [GitHub Issues](https://github.com/czz20000522/director-workbench/issues) 为准。先获取 `/.well-known/director-workbench.json` 和 `/agent/start`，按需读取 `/agent/capabilities?group=creation` 或 `presentation`。这些是不含私人业务数据的发现入口。详细 `/openapi.json`、作品、任务、媒体和 MCP 需要登录。

## 认证与权限

`POST /api/auth/login` 接受 `Content-Type: application/json`，JSON 只能含 `username` 和 `password`。服务端在响应 `Set-Cookie` 设置 `director_session`；登录 JSON 返回身份和过期时间，**不返回 token 字段**。浏览器 Cookie 为 HttpOnly，由页面管理；非浏览器客户端从自己的可信 Cookie 容器取出会话，并以 `Authorization: Bearer <本人的会话>` 调用。凭据仅存于内存或客户端安全会话容器，不放进提示词、共享 MCP 配置、Issue 或日志。

登录后用 `GET /api/auth/me` 核对当前身份，再 `GET /api/projects` 获取本人实际作品 ID。Cookie 写入请求须携带允许的同源 `Origin` 和当前身份 `X-Workspace-User`；Bearer 写入不需要浏览器身份头，但发送的 Origin 仍须符合允许列表。伪造用户名、localhost 来源或 MCP 标签不能获得管理员权限。会话只在当前服务进程内保存，服务重启、过期或密码变化后重新登录；账号切换时更新连接身份。

| 响应 | 接续方式 |
| --- | --- |
| `401` | 停止写入、核对会话并重新登录，再检查原回执 |
| `403` | 检查 Origin、权限与接口作用域；不绕过授权 |
| `404` | 核对已返回的真实 ID；同键回执未找到时原提交可能仍在处理 |
| `409` | 重读实际 revision/状态并比较，不能自动覆盖或换键提交 |
| `422` | 检查 schema、输入组合与结构化 blockers |
| 超时/连接断开 | 写入结果可能已保存，先查询记录或原任务回执 |

## 最短创作合同

先读当前服务的对应 OpenAPI schema。下列 JSON 是无真实账号、路径或素材的示例；作品/分镜 ID 和 revision 必须使用实际返回值。创建、上传、添加分镜都改变业务数据，示例不会自动授权生成。

1. 创建空白作品，`select:false` 保留用户当前选择。使用返回的 `project.id`，不要用标题推测 ID。

   `POST /api/projects/create`

   ```json
   {"series":"示例系列","title":"示例作品","creation_mode":"auto","select":false}
   ```

2. 读取 `GET /api/projects/{project_id}/plan`，以实际计划版本保存高层分镜。

   `POST /api/projects/{project_id}/plan/segments`

   ```json
   {"prompt":"用户的分镜描述","duration_seconds":5,"generation":{"mode":"auto","aspect_ratio":"16:9","seed":42},"expected_revision":0}
   ```

   这里的 `0` 只表示尚未修改的初始计划示例。H3 单段支持 4–15 秒，画幅 `16:9`、`1:1`、`9:16`；实际输出规格以预检为准。已有分镜用 `PATCH /api/projects/{project_id}/plan/segments/{segment_id}` 修改，传最新 `expected_revision`，保留用户输入。不要求普通用户安装 preset 或猜测技术 stage ID。

3. 图片/声音通过 `POST /api/projects/{project_id}/upload` 的 multipart `files` 上传，使用登记回执中实际素材引用。素材存在于库中不等于已绑定为首帧；角色参考、首尾帧、表演声音和完整母版应按对应字段分别指定。远程客户端不能把自身磁盘路径传给服务器路径导入接口。

4. 无生成预检：`POST /api/projects/{project_id}/pipeline/auto/validate`。

   ```json
   {"asset_id":"实际分镜ID","values":{}}
   ```

   读取 `valid`、实际时长/帧数与 `readiness` 中的 blockers、allowed_actions、next_actions 和 revision。预检不生成，不以读取或预检方式改写计划。缺失模型、素材或未支持组合先明确处理。

5. 用户授权一次生成后，先保留唯一请求键和完整参数，再向 `POST /api/projects/{project_id}/tasks` 提交一次。

   ```json
   {"asset_id":"实际分镜ID","pipeline_stage_id":"auto","expected_revision":1,"idempotency_key":"客户端为本次操作保存的唯一键"}
   ```

   `expected_revision` 使用刚才预检的实际版本。相同作品、请求键和参数返回原任务，键相同但参数不同会冲突。未知响应使用 `GET /api/projects/{project_id}/submission-receipt?key=原键` 核对；等待时不换键重发。

6. 用 `GET /api/queue` 和 `GET /api/projects/{project_id}/tasks/{task_id}` 查询原任务。候选通过任务返回的授权媒体入口访问；`GET /api/projects/{project_id}/segments/{segment_id}/versions` 读取历史。`stop_requested` 是停止请求，仍需等实际结束；`needs_reconcile` 需核对原执行。任务成功不表示审核、采用或交付通过。

创建/上传/新增分镜没有统一幂等回执，未知结果先读取本人作品、计划或素材确认。不要给这些操作套用生成任务的同键保证。

## 其他业务入口

完整参数和工具可用性以认证后的 OpenAPI 与当前 MCP 工具发现为准。常用作用域如下：

| 能力 | HTTP 入口 |
| --- | --- |
| 完整作品状态与准备度 | `/api/projects/{project_id}/state` |
| 剧本与场次，独立 revision | `/api/projects/{project_id}/script` |
| 图片/声音/媒体工具缺项 | `/api/keyframe-capability`、`/api/speech-capability`、`/api/media-capability` |
| 图片、声音、音频编辑、媒体处理任务 | `/api/projects/{project_id}/keyframe-tasks`、`speech-tasks`、`audio-edit-tasks`、`media-tasks` |
| 批次任务 | `/api/projects/{project_id}/batches` |
| 审核与显式采用 | `/api/projects/{project_id}/reviews`、`adoptions` |
| 参考分析与校订 | `/api/projects/{project_id}/reverse-analyses` |

这些为入口索引，不是免校验的参数模板。制作需通过 UI/API/MCP 完成；客户端不直接运行模型、FFmpeg 或 Whisper 补做产品未接入的步骤。保存剧本、计划、审核或采用时使用各自 schema 的版本字段，不能混用计划 revision 与剧本/审核 revision。审核证据与显式采用分别保存，技术报告不能替代画面、音色和表演验收。

## 页面引导

用户在目标浏览器标签页允许 Agent 引导后，客户端可通过 `/api/agent/pages` 列出本人可引导页面，再向 `/api/agent/page-actions` 提交带唯一 `action_id` 和目标 `page_id` 的受控动作。可显示未保存草稿、定位、导航、打开当前候选或播放/暂停；该层不替业务保存或生成。

用 `/api/agent/pages/{page_id}/actions` 查询实际回执。`pending` 只说明已发送，没有附属页面或呈现失败时不能声称已点击/播放。手工接管、刷新和版本变化不重放旧动作。客户端报告的呈现回执不是用户已听看或审核的证明；不能自行 ACK 伪造呈现。

## MCP 和验证边界

远程 MCP 使用同一私人 URL 的 `/mcp/`，要求可配置 Bearer 的 Streamable HTTP 客户端。目前远程端只开放能力发现中的创作/页面子集及读取指南；完整适配工具可通过本机 stdio 使用，其他业务使用认证 HTTP。没有 OAuth 登录发现；只能 OAuth 且不能配置现有 Bearer 的宿主尚未支持。

MCP 默认保留官方 SDK 的 loopback Host/Origin 限制和 DNS rebinding 防护。经本机 SSH 隧道可使用原 loopback 入口；私人网络直接访问须由服务器维护者在进程启动前配置允许列表，不能由客户端请求或 `Forwarded`/`X-Forwarded-Host` 决定：

| 服务器环境变量 | 格式与作用 |
| --- | --- |
| `DIRECTOR_MCP_ALLOWED_HOSTS` | 逗号分隔的精确 Host，可含端口；脱敏示例 `workbench.example.invalid:4100` |
| `DIRECTOR_MCP_ALLOWED_ORIGINS` | 逗号分隔的完整 HTTP(S) Origin；脱敏示例 `http://workbench.example.invalid:4100`，不带路径或末尾斜杠 |
| `DIRECTOR_PRIVATE_ORIGINS` / 启动参数 `-Origins` | 私人业务层的浏览器允许列表；客户端发送 Origin 时也须通过这一层 |

外部配置不接受通配符、URL 凭据、路径、查询参数或片段；默认 loopback 入口仍保留。客户端不发送 Origin 时仍检查 Host 与本人会话；发送 Origin 时须同时符合 MCP 和私人业务允许列表。未知 Host 返回 421，未知 Origin 返回 403，失效会话返回 401。允许入口不授予账号或作品权限。真实地址与配置只保存在服务器，不写入公开发现、Issue 或 Git；配置变更的部署另按授权处理。

接入验证先完成发现、登录、本人作品列表、权限拒绝和无生成预检。MCP schema/HTTP 回归、服务部署、干净跨设备接入与真实 GPU 输出分别验收。只读连通测试不授权生成，已实现的接口也不证明其对应制作方案全部通过。反馈采用[GitHub 协作约定](github-collaboration.md)，公开记录仅保留脱敏步骤和结论。

## 审核可写值与诊断结果

HTTP `/openapi.json` 为阶段和状态的参数来源：审核阶段为 `sample/finish/final`，审核状态为 `pending_review/approved/changes_requested/stale`；设定、checkpoint 与 artifact 另允许 `draft`。MCP `record_review` 的手动写入子集为 `pending_review/changes_requested`，正式采用通过 `adopt_candidate`，不要把两个入口的权限和参数混用。非法 HTTP 值返回 422，不保存记录。

`video_qc` 抽帧与联系表是任务诊断结果，保留在任务回执供查看；不登记为创作关键帧，不进入首尾帧或其他作品的可复用图片。旧诊断登记也过滤，旧分镜中的已保存诊断引用需明确改选创作素材，工作台不会擅自改写原计划。

当前远程 MCP 还保留 SDK 的 loopback Host/Origin 防护。建议客户端经已配置的 SSH 隧道使用 localhost 地址；HTTP 页面能直连 tailnet 不代表 `/mcp/` 接受该 Host。直连 tailnet 域名或反向代理需另行配置并验收允许的 Host/Origin；不能通过关闭这项防护解决 421/403，也不能把 HTTP 的 Origins 参数当作 MCP Host 配置。
