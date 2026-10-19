# HTTP API 与 Agent 接入

客户端使用维护者提供的私人工作台 URL 和本人账号，不需要服务器源码、Windows 路径、SSH 管理员凭据或 ComfyUI 地址。服务端部署见[开发指南](public-development-guide.md)，MCP 配置见[连接说明](../mcp_server/README.md)。

本指南描述公开代码的接入合同；实际部署版本、模型就绪和产品验收以当前服务返回值及 [GitHub Issues](https://github.com/czz20000522/director-workbench/issues) 为准。接入先登录取得本人会话，再读取 `/openapi.json` 查看本部署的完整合法输入；服务不提供匿名发现入口。

## 认证与权限

`POST /api/auth/login` 接受 `Content-Type: application/json`，JSON 只能含 `username` 和 `password`。服务端在响应 `Set-Cookie` 设置 `director_session`；登录 JSON 返回身份和过期时间，**不返回 token 字段**。浏览器 Cookie 为 HttpOnly，由页面管理；非浏览器客户端从自己的可信 Cookie 容器取出会话，并以 `Authorization: Bearer <本人的会话>` 调用。凭据仅存于内存或客户端安全会话容器，不放进提示词、共享 MCP 配置、Issue 或日志。

失败登录按“账号 + 客户端来源”记录：默认 5 次失败在 5 分钟窗口内会返回 `429`，窗口过期或成功登录后清除计数。客户端应停止继续猜测密码，等待窗口结束或核对账号配置；该限制不改变成功会话的 Cookie/Bearer 使用方式。

需要 Bearer 的非浏览器客户端可调用 `POST /api/auth/agent-token`（JSON 同样只含 `username`/`password`）：成功响应包含 `token`、`username`、`administrator`、`expires_at`，**不设置 Cookie**。把返回的 `token` 作为 `Authorization: Bearer <token>` 使用；Token 等同本人会话凭据，只放本地客户端配置/环境变量，不写仓库、日志或聊天。注销、过期或换账号后重新获取，该入口与登录共用账号、会话和限流。

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
   {"prompt":"用户的分镜描述","duration_seconds":5,"workflow":"workflows/已登记方案.json","generation":{"mode":"auto","aspect_ratio":"16:9","seed":42},"expected_revision":0}
   ```

   这里的 `0` 只表示尚未修改的初始计划示例。H3 单段支持 4–15 秒，画幅 `16:9`、`1:1`、`9:16`；实际输出规格以预检为准。已有分镜用 `PATCH /api/projects/{project_id}/plan/segments/{segment_id}` 修改，传最新 `expected_revision`，保留用户输入。不要求普通用户安装 preset 或猜测技术 stage ID。

3. 图片/声音通过 `POST /api/projects/{project_id}/upload` 的 multipart `files` 上传，使用登记回执中实际素材引用。素材存在于库中不等于已绑定为首帧；角色参考、首尾帧、表演声音和完整母版应按对应字段分别指定。远程客户端不能把自身磁盘路径传给服务器路径导入接口。

   上传有服务端上限：单文件默认 2 GiB、单次默认 200 个文件、单请求默认 4 GiB（部署方可用 `DIRECTOR_MAX_UPLOAD_BYTES`、`DIRECTOR_MAX_UPLOAD_FILES`、`DIRECTOR_MAX_UPLOAD_REQUEST_BYTES` 收紧或调整）。超限返回 `413`，不登记素材；客户端应据此拆分批次或压缩素材，而不是重试同一请求。请求体上限会在 multipart 解析前检查，单文件写入使用分块和临时文件，失败时清理半成品。

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

   `asset_id` 是必填的单段对象标识；服务端不会从项目默认分镜补值。分镜必须在计划中绑定明确的 `workflow` 或提交明确的 `pipeline_stage_id`，缺失时返回 `422` 的 `workflow_required` blocker，不会使用项目默认 workflow。

6. 批次提交必须显式列出非空 `asset_ids`，例如：

   ```json
   {"asset_ids":["S01","S03"],"idempotency_key":"客户端为本次批次保存的唯一键"}
   ```

   空列表、缺少字段或未绑定 workflow 的分镜会在创建任务前返回结构化 `422` blocker；服务端不会把所有可执行分镜隐式加入批次。

7. 用 `GET /api/queue` 和 `GET /api/projects/{project_id}/tasks/{task_id}` 查询原任务。候选通过任务返回的授权媒体入口访问；`GET /api/projects/{project_id}/segments/{segment_id}/versions` 读取历史。`stop_requested` 是停止请求，仍需等实际结束；`needs_reconcile` 需核对原执行。任务成功不表示审核、采用或交付通过。

创建/上传/新增分镜没有统一幂等回执，未知结果先读取本人作品、计划或素材确认。不要给这些操作套用生成任务的同键保证。

## 其他业务入口

完整参数和工具可用性以认证后的 OpenAPI 与当前 MCP 工具发现为准。常用作用域如下：

| 能力 | HTTP 入口 |
| --- | --- |
| 完整作品状态与准备度 | `/api/projects/{project_id}/state` |
| 工作台与 ComfyUI 状态 | `/api/health`、`/api/status`；状态响应不返回服务器运行目录等机器路径 |
| 剧本与场次，独立 revision | `/api/projects/{project_id}/script` |
| 图片/声音/媒体工具缺项 | `/api/keyframe-capability`、`/api/speech-capability`、`/api/media-capability` |
| 图片、声音、音频编辑、全片母版后期、媒体处理任务 | `/api/projects/{project_id}/keyframe-tasks`、`speech-tasks`、`audio-edit-tasks`、`audio-master-tasks`、`media-tasks` |
| 批次任务 | `/api/projects/{project_id}/batches` |
| 审核与显式采用 | `/api/projects/{project_id}/reviews`、`adoptions` |
| 参考素材登记、显式范围/帧/音频观察、ASR、模型分析与校订 | `/api/projects/{project_id}/reverse-analyses` |

参考素材上传只登记原始文件和媒体探测事实，响应中的 `timeline`、`observations` 和任务表都保持为空；不会自动拆分、抽帧或运行 ASR。需要观察时，调用方必须明确调用：

- `POST .../observations/range`：提交 `start_seconds`、`end_seconds`，可选 `frame_seconds`，生成只读范围和帧观察；
- `POST .../observations/sample`：提交明确的 `times_seconds`，结果标为“等距采样/待确认范围”，不能当作镜头切点；
- `POST .../observations/audio`：提交明确音频范围；
- `POST .../observations/asr`：显式运行 ASR，结果标为 `model_inference` / `candidate_needs_review`。

`POST .../semantic-jobs` 也要求先存在至少一帧显式观察。模型输出只能写入 `model_candidate`，保留 `provenance` 和 `user_status`，不会自动覆盖人工校订或制作字段。

转制作使用 `POST .../convert` 时，必须提交非空 `shots` 列表；每项包含显式参考时间段和至少一个明确制作字段。空列表、采样观察或模型候选不会自动创建 `Rxx` 分镜或写入制作计划。

这些为入口索引，不是免校验的参数模板。制作需通过 UI/API/MCP 完成；客户端不直接运行模型、FFmpeg 或 Whisper 补做产品未接入的步骤。保存剧本、计划、审核或采用时使用各自 schema 的版本字段，不能混用计划 revision 与剧本/审核 revision。审核证据与显式采用分别保存，技术报告不能替代画面、音色和表演验收。

## MCP 和验证边界

远程 MCP 使用同一私人 URL 的 `/mcp/`，要求可配置 Bearer 的 Streamable HTTP 客户端。公网入口由 HTTPS 反向代理转发到本机 `/mcp/`，未认证请求返回 401；远程端固定开放一组经过业务路由授权的安全工具（当前 50 个），按能力覆盖：

| 类别 | 远程能力 |
| --- | --- |
| 作品与计划 | 作品/剧本/创作草稿读取与保存、分镜新增/修改/整理、素材上传与复用、审核记录、版本和预检读取 |
| 参考与素材 | 参考拆解列表/详情、分析任务回执、时间线/分类/产物校订、转为分镜草案 |
| 任务与制作 | 队列/指南/制作方案读取，单段和批次提交，任务读取、请求停止、回执和待核对状态 |
| 关键帧、声音与媒体 | 关键帧/语音能力检查和候选提交，音频编辑、静音、视频技术检查、转写和无声审片候选 |
| 装配 | 装配编辑与预检读取、接点/音轨记录、装配提交 |

这些工具仍通过现有 Bearer 认证和 HTTP 业务路由执行；远程端不提供账户或管理操作、删除/清理、任意服务器路径导入或导出、脚本执行、全局设置修改、历史版本恢复、任务恢复/强制收尾等能力。完整适配工具可通过本机 stdio 使用，其他业务使用认证 HTTP。没有 OAuth 登录发现；只能 OAuth 且不能配置现有 Bearer 的宿主尚未支持。

MCP 默认保留官方 SDK 的 loopback Host/Origin 限制和 DNS rebinding 防护。公网入口必须在工作台或反向代理启动前配置精确允许列表，不能由客户端请求或 `Forwarded`/`X-Forwarded-Host` 决定：

| 服务器环境变量 | 格式与作用 |
| --- | --- |
| `DIRECTOR_MCP_ALLOWED_HOSTS` | 逗号分隔的精确 Host，可含端口；脱敏示例 `workbench.example.invalid:4100` |
| `DIRECTOR_MCP_ALLOWED_ORIGINS` | 逗号分隔的完整 HTTP(S) Origin；脱敏示例 `http://workbench.example.invalid:4100`，不带路径或末尾斜杠 |
| `DIRECTOR_PRIVATE_ORIGINS` / 启动参数 `-Origins` | 私人业务层的浏览器允许列表；客户端发送 Origin 时也须通过这一层 |

外部配置不接受通配符、URL 凭据、路径、查询参数或片段；默认 loopback 入口仍保留。客户端不发送 Origin 时仍检查 Host 与本人会话；发送 Origin 时须同时符合 MCP 和私人业务允许列表。未知 Host 返回 421，未知 Origin 返回 403，失效会话返回 401。允许入口不授予账号或作品权限。真实地址与配置只保存在服务器，不写入公开发现、Issue 或 Git；配置变更的部署另按授权处理。

接入验证先完成登录、本人作品列表、权限拒绝和无生成预检。MCP schema/HTTP 回归、服务部署、干净跨设备接入与真实 GPU 输出分别验收。只读连通测试不授权生成，已实现的接口也不证明其对应制作方案全部通过。反馈采用[GitHub 协作约定](github-collaboration.md)，公开记录仅保留脱敏步骤和结论。

## 审核可写值与诊断结果

HTTP `/openapi.json` 为阶段和状态的参数来源：审核阶段为 `sample/finish/final`，审核状态为 `pending_review/approved/changes_requested/stale`；设定、checkpoint 与 artifact 另允许 `draft`。MCP `record_review` 的手动写入子集为 `pending_review/changes_requested`，正式采用通过 `adopt_candidate`，不要把两个入口的权限和参数混用。非法 HTTP 值返回 422，不保存记录。

`video_qc` 抽帧与联系表是任务诊断结果，保留在任务回执供查看；不登记为创作关键帧，不进入首尾帧或其他作品的可复用图片。旧诊断登记也过滤，旧分镜中的已保存诊断引用需明确改选创作素材，工作台不会擅自改写原计划。

当前远程 MCP 仍保留 SDK 的 loopback Host/Origin 防护。公网使用 HTTPS 反向代理时，必须把实际 Host/Origin 通过服务器环境变量加入允许列表，并在代理层保留 `Authorization` 和 `Host`；不能用关闭防护解决 421/403。

标准 MCP 客户端配置：

```text
Endpoint: https://<workbench-host>/mcp/
Header:   Authorization: Bearer <agent-token>
```

先按上文 `POST /api/auth/agent-token` 获取 token，不要依赖浏览器 Cookie 作为长期凭据。
