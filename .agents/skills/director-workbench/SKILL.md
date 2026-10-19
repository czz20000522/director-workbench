---
name: director-workbench
description: 通过用户给出的导演工作台 URL 和本人凭据，使用公开 HTTP 或 MCP 入口完成许愿式创作：认证、确认愿望与边界、参考与素材的显式登记/观察、显式制作方案、预检、按授权生成、轮询、读取候选、逐段显式采用、接点/音轨/装配与受控媒体交付。不操作浏览器界面，不替用户选择，不自动拆分、转写或采用。
---

# 导演工作台：许愿式创作 Agent 操作规程

本 Skill 面向外部 Agent。用户可能只给一个工作台 URL、一份本人凭据和一个愿望（例如“帮我做一个这样的短片”），不一定打开界面。Agent 负责认证、询问必要的创作选择、调用工作台入口、轮询真实回执、读取真实候选并按用户决定逐步推进；工作台负责确定性登记、执行和回执，不代替用户理解、判断、选择或采用。

本文件只描述**执行规程**，不替代服务端 schema。认证后以 `tools/list` 和 `GET /openapi.json` 为当前部署的权威合同；本文件与代码不一致时，以服务端实际返回为准，并把差异记为待修文档问题。

## 版本与合同快照

- Skill 版本：`1.1.0`（2026-10-19）。
- 合同快照：远程 MCP 固定开放 **60** 个工具；本机 stdio 全量 **67** 个工具；远程固定名单见[附录 A](#附录-a远程-mcp-工具清单60)，仅本机工具见[附录 B](#附录-b仅本机-stdio-的-7-个工具)。
- 工具数量、名称或关键合同变化时，必须同步更新本版本号、附录和 `mcp_server/README.md`、`docs/public-api-guide.md`，不能只改实现。
- 工作台当前只注册 MCP **tools**，没有注册 MCP **resources** 或 **prompts**。因此不能通过 MCP 资源接口拉取本 Skill；这是当前限制，不是“已支持”。本 Skill 由仓库或公开源代码快照分发。

## 安装与获取 Skill

1. 从公开仓库或维护者提供的脱敏源代码快照取得 `.agents/skills/director-workbench/SKILL.md`。
2. 按外部 Agent 客户端自己的 Skill 安装机制，把这个目录放进该客户端的项目级或用户级 Skill 目录。不同客户端目录不同，以客户端文档为准；工作台不会自动写入任何客户端的 Skill 目录。
3. 工作台地址、agent-token、CA 证书和账号由维护者通过私人渠道单独提供，不写进本 Skill、仓库、Issue 或共享配置。
4. 如果客户端只能通过 MCP 自动发现 Skill，当前无法满足：服务端只有 tools，没有 resources/prompts。需要由用户手动安装该目录。

## 1. 认证、入口与工具发现

### 1.1 输入边界

- 只使用用户本次提供的 URL、账号或 token。不要猜服务器地址、端口、路径、ComfyUI 地址、ComfyUI 运行目录或数据库位置。
- 不要求用户给 SSH 管理员权限、服务器 Python 路径或 ComfyUI 节点信息；外部 Agent 只使用公开 HTTP/MCP 业务入口。
- 凭据只放在客户端内存、安全会话容器或本机环境变量中；不要写进提示词、聊天记录、日志、Issue、仓库或共享 MCP 配置。

### 1.2 agent-token（推荐）

1. `POST /api/auth/agent-token`，`Content-Type: application/json`，请求体必须且只能含 `username`、`password`。
2. 成功响应含 `token`、`username`、`administrator`、`expires_at`，**不设置 Cookie**。
3. 之后以 `Authorization: Bearer <token>` 调用 HTTP 和远程 MCP。Token 等同本人会话凭据。
4. `GET /api/auth/me` 核对当前身份；`GET /api/projects` 读取本人作品，使用实际返回的 `id`，不要用标题推测 ID。
5. 注销、过期、服务重启或换账号后重新获取 token；401 后停止写入并重新认证，不要带着旧 token 继续重试。

失败登录按“账号 + 客户端来源”限流：5 分钟窗口内默认 5 次失败后返回 `429`。停止猜密码，等待窗口结束或核对账号配置。

### 1.3 Cookie 与浏览器会话

- `POST /api/auth/login` 成功时服务端设置 HttpOnly Cookie `director_session`，登录 JSON 只返回身份和过期时间，不返回 token。
- 非浏览器客户端可以从自己的可信 Cookie 容器取出该会话值，作为 `Authorization: Bearer <会话值>` 使用；不要把 Cookie 值共享给其他人或写进配置库。
- Cookie 写入必须带允许的同源 `Origin` 和当前身份 `X-Workspace-User`；Bearer 写入不需要 `X-Workspace-User`，但如果发送 `Origin`，仍须通过允许列表。
- 无效的显式 Bearer **不会**回退到 Cookie；401 时不要用同一请求换 Cookie 再试来绕过。
- 远程 MCP 要求客户端能配置 Bearer。只能 OAuth 且不能配置现有 Bearer 的宿主当前不支持。

### 1.4 CA / TLS

- 如果工作台使用 HTTPS 且由私人 CA 签发，客户端所在操作系统/运行时必须信任该 CA 链。向维护者私下取得 CA 根证书，只安装到该客户端的受信存储，并限定用途。
- 不要关闭证书校验、不要接受任意证书、不要把 CA 私钥放进客户端。公开仓库不包含 CA、证书或私钥。
- 使用 loopback HTTP 或已配置的 SSH 隧道时不需要额外 CA 步骤；但 MCP 仍会检查 Host/Origin。

### 1.5 公网 MCP 与 Host/Origin

- 远程 MCP 入口是同一工作台 URL 下的 `/mcp/`，传输方式为 Streamable HTTP，请求头 `Authorization: Bearer <agent-token>`；不提供 OAuth 登录发现。
- 服务端保留 DNS rebinding 防护。默认只接受 loopback Host/Origin；公网或直连 tailnet 域名必须由维护者在**服务器启动前**配置精确允许列表：
  - `DIRECTOR_MCP_ALLOWED_HOSTS`：逗号分隔的精确 Host，可含端口，不接受通配符。
  - `DIRECTOR_MCP_ALLOWED_ORIGINS`：逗号分隔的完整 `http(s)://host[:port]` Origin，不带路径或末尾斜杠。
  - `DIRECTOR_PRIVATE_ORIGINS` / 启动参数 `-Origins`：私人业务层浏览器允许列表；客户端发送 Origin 时也须通过这一层。
- 客户端不能通过请求里的 `Host`、`Forwarded`、`X-Forwarded-Host` 扩大允许范围。未知 Host 返回 `421`，未知 Origin 返回 `403`，失效会话返回 `401`；这些都不是靠改请求头能解决的问题。反向代理必须保留 `Authorization` 和原始 `Host`。
- 公网入口配置和真实地址只保存在服务器；不要写进公开 Issue 或 Git。

### 1.6 工具发现与 schema

- 对远程 MCP：初始化后调用 `tools/list`，读取实际工具名和 `input_schema`（JSON Schema object）。本版本远程 60 个，名称见附录 A。
- 对 HTTP：认证后读取 `GET /openapi.json`，以当前部署的路径、参数和枚举为准；服务没有匿名发现入口。
- MCP 工具上的 `read_only_hint`、`idempotent_hint` 等 annotations 只是客户端提示，不是授权；服务端仍会校验账号、作品归属、版本和状态。`idempotent_hint=true` 只表示能用同一 `idempotency_key` 和相同参数重放并找回原记录；无键写入仍要未知回执先核对。
- 业务结果只有两种形状：成功 `{ok: true, data: ...}`；失败为 `isError=true` 且同一次调用带结构化 `{ok: false, error: ...}`。`error` 必含 `code`、`message`、`kind`、`retryable`；`kind` 为 `business`（HTTP 业务拒绝，含 `http_status`/`detail`）、`argument`（适配器参数/语义错误）、`transport`（读取暂时失败可重试）或 `outcome_unknown`（写入结果未知，先核对）。MCP 传输成功不代表业务成功。
- 参数缺失、类型错误、超长或超出 schema 范围时，SDK 在工具执行前返回**工具错误**（文本列出被拒字段），不调用工作台、也没有 `{ok:false}` 结构。读取错误文本并对照 `tools/list` 的 `input_schema` 修正参数，不要在参数不明时重试计费写入。
- 分页工具（`list_tasks` 1–100、`list_shot_versions` 1–100、`list_references` 1–100、`list_media` 1–100；`offset≥0`）的 `meta.pagination` 声明同一合同，响应带 `next_offset`。其他列表工具不要假设分页。
- 远程入口没有的工具，不要伪造同名调用；可使用认证 HTTP 原业务入口（见附录 B）或本机 stdio 全量工具。

## 2. 确认愿望与边界（先问再动）

用户只给愿望时，先把愿望复述为可执行的范围，并询问**最少但必要**的选择。没有明确答案时不要套用默认方案。

必须问或必须让用户明确选择的事项：

1. **交付物**：单段样片、多段短片还是完整成片；大概时长、画幅、分辨率、是否需要声音。
2. **制作方案**：从 `list_production_presets` 读取事实清单后，让用户选一个已安装/可安装的方案；不要按画幅、素材、速度或“推荐”替用户选。
3. **内容来源**：原创描述、参考视频、参考图片、音频或已有剧本；如果使用参考，明确使用哪个参考、哪个显式时间段、是否需要导帧/导音频/ASR/模型观察。
4. **生成授权与成本**：是否现在生成、先生成几条候选、是否接受排队/失败/重试；生成提交是需要用户授权的写入。
5. **候选取舍**：每个分镜的候选由用户看/听后决定；Agent 可以整理事实和差异，但不能替用户宣布“更好”或自动采用。
6. **装配与交付**：接点是否通过、全片音轨策略（保留片段原音/完整母版/静音）、是否装配、是否最终采用、交付到什么位置。
7. **范围扩张**：需要更多分镜、更长时长或更多重试时，先向用户说明新增范围，不静默扩张。

事实与能力边界（可以告诉用户，不要包装成平台判断）：

- H3 单段支持 4–15 秒；画幅为 `16:9`、`1:1`、`9:16`；实际帧数和输出时长以预检返回的 `h3_duration` 与 `readiness` 为准。
- 工作台不会替用户理解参考内容、推荐方案或自动改编。模型观察、ASR、技术检查都只是候选/证据。
- 用户说“你决定”时，你可以提出一个具体的显式方案，但要把选择的方案 ID、时长、画幅、参考段和理由写清楚；不能声称这是平台推荐或默认值。涉及生成、采用和装配的写入仍按用户本次授权执行。

| 类别 | 内容 | 规程 |
| --- | --- | --- |
| 必须问用户 | 作品目标、交付规格、制作方案、参考来源/显式时间段、是否运行 ASR/模型观察、生成授权与数量、候选取舍、采用、接点/音轨、装配、最终交付 | 得到明确回答后再写入；未回答就停在只读观察 |
| 只作观察/候选 | 预检 readiness/blockers、任务成功或失败、技术检查、ASR、模型观察、版本历史、任务回执、媒体文件存在 | 可以汇报和整理，但不能说成用户确认、内容通过或已采用 |
| 必须显式确认/写入 | 安装方案、创建/修改分镜、提交生成/批次、逐段采用、接点审核、音轨策略、装配提交、成片最终采用 | 用实际 revision/ID/候选引用写入并保存回执；一次写入对应一次明确决定 |

用户明确委托 Agent 选择时，把具体选择写进请求并在交付中标注“受用户委托的选择”；这仍然不是平台自动选择，也不免除用户对采用和交付的最终决定。

## 3. 参考与素材：上传只登记，观察必须显式

### 3.1 上传素材

- HTTP：`POST /api/projects/{project_id}/upload`，multipart 字段 `files`。响应含 `uploaded`（实际登记引用）和 `report.counts`。
- 默认上限：单文件 2 GiB、单次 200 个文件、单请求 4 GiB；部署方可通过 `DIRECTOR_MAX_UPLOAD_BYTES`、`DIRECTOR_MAX_UPLOAD_FILES`、`DIRECTOR_MAX_UPLOAD_REQUEST_BYTES` 收紧。超限返回 `413`，不登记素材；拆分批次或压缩后重试，不要原样重发。
- 远程 MCP 的 `upload_material` 接受 base64 内容，解码后单文件最多 16 MiB；较大文件走 HTTP multipart。远程客户端不能把本机路径传给服务端路径导入接口。
- 上传是**非幂等**操作：响应未知时先读 `read_project(section='reusable-materials')` 或作品 manifest 核对，不盲目重传。
- 素材存在于库中不等于已绑定为首帧、尾帧、表演音频或完整母版。绑定必须用对应字段：
  - `first_frame`：开始画面；
  - `last_frame`：结束画面；
  - `audio_guide`：表演/对白引导音频；
  - `delivery_master`：完整声音母版；
  - `reference_asset_id`：参考素材/参考画像。
- 角色参考、首尾帧、表演声音、完整母版各有用途，不能互相冒充。方案不支持的输入组合会返回 `422`；保留用户输入并说明缺口，不能静默忽略。

### 3.2 参考上传只登记

- 远程 MCP 没有本机路径导入工具 `import_reference`（仅 stdio 可用）。远程客户端用 HTTP：`POST /api/projects/{project_id}/reverse-analyses`，multipart 字段 `file`。
- 支持常见音视频后缀；响应只登记源文件和媒体探测事实，`timeline`、`observations` 保持为空，不会自动拆分、抽帧或运行 ASR。
- 记录返回的实际 `analysis_id`，后续观察使用该 ID。参考拆解有独立 `revision`；列表/详情用 `list_references`、`read_reference`。

### 3.3 显式观察（不得把采样当镜头）

只有调用方明确提交时间/时间点时才产生观察：

- `extract_reference_range`：提交 `start_seconds`、`end_seconds`，可选 `frame_seconds`（必须在区间内）。生成只读范围和帧观察，`is_shot_cut=false`，不能自动成为制作分镜来源。
- `sample_reference_frames`：提交 1–64 个明确 `times_seconds`。结果标记为“等距采样/待确认范围”，**绝对不能当作镜头切点**。
- `extract_reference_audio`：提交明确音频范围；参考没有音轨时返回 `422`。
- `transcribe_reference`：显式运行 ASR。结果是 `provenance=model_inference`、`status=candidate_needs_review`、`review_label=模型候选/待校订`，不能当作原词、事实或已确认台词。
- `start_reference_analysis`：启动模型观察前，必须先有至少一帧显式范围/采样观察；否则返回 `422 explicit_observation_required`。用 `read_reference(section='semantic_job')` 查询回执；`needs_reconcile` 时用 `reconcile_reference_analysis` 核对原回执，不重新分析。
- 模型输出只能写入 `observations` / `model_candidate`，保留 `provenance`、`user_status`；不会自动覆盖人工校订，也不会自动填入制作字段。
- 需要校订时，先 `read_reference` 取 `revision`，再显式调用 `annotate_reference_timeline`、`correct_reference_artifact` 或 `correct_reference_classification`；`409` 后重读比较，不能盲目提高版本覆盖。
- `convert_reference_to_shots` 只接受调用方明确提交的**非空** `shots` 列表；每项包含 `start_seconds`、`end_seconds` 和至少一个制作字段。空列表、采样观察、模型候选都不会自动创建分镜；模型候选也不会自动成为 `prompt`/`performance`。

### 3.4 参考与素材的分页和读取

- `list_references(limit, offset)` 返回 `total`、`next_offset`；使用 `next_offset` 继续。
- 需要查看参考原文/参考片段时，用工具返回的稳定 `media_ref`（或在 `list_media(category='references')` 中读取）和短期受控 URL 读取（见第 8 节），不要猜服务器文件路径。

## 4. 计划与显式制作方案

### 4.1 创建作品

- 先 `list_projects` 核对；只有用户要求新作品时才创建。
- `create_project` / `POST /api/projects/create` 必须显式传 `creation_mode:"advanced"`；自动创作模式已移除，缺省或旧值返回 `422`。
- 创建是**非幂等**操作。响应未知时先 `list_projects` 按标题/系列和实际返回核对，不要自动重发；同名可能自动加后缀，使用返回的实际 `project.id`。
- `select:false` 不切换用户当前作品；外部 Agent 使用显式 `project_id` 调用后续工具，不依赖“当前作品”概念。

### 4.2 读取方案事实，让用户显式选择

1. `list_production_presets`：读取方案 `id`、`title`、`description`、`available`、`missing_models`、`verification` 等事实。它不含“推荐”或默认选择。
2. 让用户从事实清单里选一个方案。若用户没有选，列出有差异的候选并等待选择；不要按画幅、素材、速度或顺序替选。
3. 用户选定后，用 `configure_production_preset` / `POST /api/projects/{project_id}/production-presets/{preset_id}` 显式安装。安装只写作品 manifest 和执行图，不生成。
4. `read_project(section='pipeline')` 读取实际 `stages`，记录真实 `stage_id`、`workflow`、`input_specs`。不要猜 stage ID。
5. `read_project(section='reusable-templates')` 返回 `available_templates` 事实清单，不含 `recommended` 或默认选择；是否采用由用户明确决定。

### 4.3 保存分镜

- 读取 `read_project(section='plan')` 的实际 `revision`。
- 新增：`create_shot` / `POST /api/projects/{project_id}/plan/segments`，提交作品 ID、分镜 ID、真实 `expected_revision`、`duration_seconds`、用户原文 `prompt`/`performance`，并显式传 `workflow` 或已安装的 `preset_id`。
  - 如果两者都不传，服务端会复用该作品**此前显式安装**的方案；这不是平台默认，也不是从目录自动挑方案。作品没有已安装方案时返回结构化 `creation_plan_required`/`preset_not_installed` 和可选项，不写计划；预检/状态准备度则用 `stage_required`/`recipe_unresolved` 表达同一缺口。
  - 多方案、无方案或方案不匹配时，先回到 4.2 让用户选择，不要猜。
- 修改：`update_shot` / `PATCH /api/projects/{project_id}/plan/segments/{segment_id}`，传最新 `expected_revision`，只提交要改的字段，保留用户原文。
- 整理：`arrange_shot` 支持 copy、move、split、merge、merge_range；按操作只传该操作需要的参数。整理可能使候选和审核过期，不生成视频；回执不确定先读计划。
- 剧本场次是叙事单位，不是生成分镜；绑定场次需同时传 `script_scene_id` 和 `read_script` 的 `expected_script_revision`。
- 写入版本冲突（409）后重读实际计划比较，不自动覆盖；非幂等写入响应未知时先读计划核对，不盲目重发。

## 5. 预检（无生成）

- 预检：`preflight(project_id, asset_id, stage_id, values?)` / `POST /api/projects/{project_id}/pipeline/{stage_id}/validate`。`stage_id` 必须来自该作品已安装的 pipeline；自动步骤已移除。
- 读取 `valid`、`readiness`（`state`、`ready`、`queueable`、`blockers`、`allowed_actions`、`next_actions`）、实际材料/参数、`binding_count`、`h3_duration`（若返回）。
- `read_project(section='state')` 返回同一套准备度，包括每个分镜和装配的 blockers/actions；`read_project(section='reusable-templates')` 返回方案事实，不含推荐。
- 常见阻塞：
  - `stage_required`：尚未显式选择方案，返回可选的已安装步骤/方案事实；
  - `recipe_unresolved`：登记的工作流/步骤不在已安装方案中；
  - `workflow_required`：提交或预检时缺少明确 workflow/方案；
  - `waiting_dependency`：前置分镜任务未完成，可能 `queueable` 但 `ready=false`；
  - 素材/模型缺失、时长/画幅不匹配等：按 blocker 的 `action` 补齐。
- 预检不生成、不改变计划 revision。`valid=false` 时不得提交生成；先补齐输入或请用户重新选择方案，再重新预检。

## 6. 生成与批次（显式提交）

### 6.1 提交前

1. 用户已明确授权本次生成（“生成看看/按这段跑”可以授权一次必要提交；“解释/草稿/预检”不等于授权）。
2. 使用刚预检的实际 `stage_id`、分镜 ID、计划 `expected_revision` 和完整参数。
3. 为本次操作生成唯一 `idempotency_key`（1–160 字符），并在本地保存键 + 完整请求参数。键必须在同一作品内唯一，且不能重复用于不同参数。

### 6.2 单段

`submit_shot` / `POST /api/projects/{project_id}/tasks`：

```json
{
  "asset_id": "实际分镜ID",
  "pipeline_stage_id": "实际stage_id",
  "expected_revision": 1,
  "idempotency_key": "客户端保存的唯一键",
  "pipeline_values": {}
}
```

- `asset_id` 必填；服务端不会从项目默认分镜补值。
- 分镜必须绑定明确 workflow 或提交明确 `pipeline_stage_id`；缺失返回 `422 workflow_required`，不会使用项目默认 workflow。
- 提交一次即可。相同作品、请求键和参数返回原任务；键相同但参数不同返回 `409 idempotency_key_conflict`。
- 不会自动选择候选、自动采用或自动继续后续步骤。

### 6.3 批次

`submit_batch` / `POST /api/projects/{project_id}/batches`：

- 必须显式提供非空 `asset_ids` 列表；不能空列表，也不能让服务端隐式加入全部分镜。
- 前置分镜必须排在依赖它的分镜之前，否则 `422 batch_dependency_order`。
- 每个分镜仍需明确 workflow/方案；缺一个就会在创建任务前返回 `422 workflow_required`，批次不创建。
- 同一 `idempotency_key` 和参数返回原批次；参数不同冲突。
- 批次控制 `control_batch` 仅 stdio 可用；远程/HTTP 用 `POST /api/projects/{project_id}/batches/{batch_id}/stop|resume`。`needs_reconcile` 批次先核对原回执，不能直接恢复。

## 7. 轮询、失败、未知回执与幂等

### 7.1 状态与轮询

- 提交返回任务或批次回执，初始状态常见 `scheduler_waiting` / `batch_waiting` / `preparing`；用 `get_task`、`list_tasks`、`read_queue` 读取真实状态。
- 调度等待原因在任务 `queue` 中（如 `waiting_execution_slot`、`waiting_dependency`、`waiting_reconciliation`）。`waiting` 不是失败。
- 终态：`succeeded`、`failed`、`stopped`。执行中常见：`scheduler_waiting`、`batch_waiting`、`preparing`、`submitting`、`submitted`、`queued`、`running`、`finalizing`、`stopping`、`stop_requested`。
- 轮询使用退避（例如几秒后逐步拉长），拿到终态即停止；不要高频率紧循环。长时间未终态时记录任务 ID、状态和 queue，不重复提交。
- `stop_requested` 只是停止请求，不是停止完成；继续 `get_task` 核对终态。`needs_reconcile` 先核对原执行。
- 分页：`list_tasks(limit 1–100, offset)`、`list_shot_versions(limit 1–100, offset)`、`list_references(limit 1–100, offset)`、`list_media(limit 1–100, offset)` 使用 `next_offset`；工具 `meta.pagination` 声明同一合同。其他列表工具不要假设有分页。

### 7.2 未知回执

- MCP 失败结构统一为 `isError=true` 加 `{ok:false,error:{code,message,kind,retryable,...}}`。`kind=transport` 的 GET 可重试；`kind=outcome_unknown`（`outcome_unknown=true`、`retryable=false`）表示写入可能已保存，先查原回执，不重发。
- 参数缺失/类型或范围不符由 SDK 在工具执行前拒绝（tool error，仅文本）；按 schema 修正后重试，不要当成业务失败去换键重提。
- 生成类写入先保存原键和完整参数，再调用 `get_submission_receipt(project_id, idempotency_key)` / `GET /api/projects/{project_id}/submission-receipt?key=...` 核对。
- 回执 `404 submission_not_found` 只表示尚未找到已落库任务，不证明原请求失败；保留原键稍后核对，不能换新键重发计费操作。
- 用原键查到原任务后，按任务实际状态继续；同一键同参数不会创建第二个任务。
- 创建作品、上传、新增分镜、审核、采用、装配编辑没有统一的同键幂等保证；响应未知时先读取相应记录/回执核对，再决定是否需要用户授权后的新操作。`idempotent_hint=true` 的工具才可以用原键原参数重放。

### 7.3 `needs_reconcile`

- 任务状态为 `needs_reconcile` 时，先调用 `reconcile_generation` 核对原 ComfyUI 队列/历史或 CPU 媒体完成回执；它不创建重试、不提交新生成。
- 参考分析任务用 `reconcile_reference_analysis`；仅用于 `needs_reconcile`，409 表示尚不能确认，保留待核对状态，不能据此重启分析。
- 远程 MCP 不暴露 `resolve_missing_execution` 和 `resume_generation`。只能在核实旧执行确已结束、已检查输出后，通过 HTTP 原业务入口调用；不能仅凭等待超时使用，也不能把未知执行标记为失败或停止。
- 不允许在 `needs_reconcile` 状态下换键重复提交；先核对，再按用户决定是否新开一次操作。

## 8. 候选与显式媒体读取

- 任务成功后读取真实候选来源：
  - `get_task`：远程回执的 `media`/`candidate_ref` 受控引用，以及 `result.published_outputs`（本机回执）；
  - `list_shot_versions`：分镜生成历史、当前版本、`inspection`、`comparison_sources`（当前候选 vs 参考 vs 历史）、`change_impact`，候选带稳定 `candidate_ref`；
  - `list_media`：按 `category` 分页列出素材、分镜候选、成片、审片、参考观察和任务产物；
  - `read_project(section='plan')`：当前分镜与装配进度（远程响应中的媒体以 `media_ref` 替代路径）；
  - `read_project(section='manifest')`：已登记素材和作品信息。
- 媒体通过稳定 `media_ref` 加短期受控 URL 读取：`list_media` 返回 `media_ref`/`url`；`get_media_url` 为已有引用重签；`download_media` 对小文件返回 base64、对大文件只返回 URL。URL 形如 `<工作台 URL>/api/projects/{project_id}/media/{token}`，带本人 `Authorization: Bearer`；不返回、不接受服务器路径。`/media-file?path=` 和 `/media/{asset_name}` 只是本机 UI 的登记引用入口，外部 Agent 不要构造；`media_ref` 形如 `asset:<素材ID>:A`、`segment:<分镜ID>:video`、`task:<任务ID>:video:0`、`reference:<拆解ID>:<观察ID>:frame_0`。
- **只有实际取回媒体内容并解码/播放检查后，才能说“看过/听过这份候选”。** 仅看到任务成功、`media_ref` 或文件名，不等于已检查内容。
- 技术检查（`inspect_video` 等媒体任务）和 ASR 是证据，不是画面、音色、表演或成片验收；模型候选保持 `provenance`/`user_status`，标记为“模型候选/待校订”。
- 参考 vs 候选对照使用工作台返回的 `comparison_sources` 和显式媒体读取；差异、时长、画幅、音轨是事实，优劣判断由用户做。
- 任务成功不表示审核通过、采用或交付；候选生成成功也不表示用户已看/已听。`download_media` 返回的 base64/URL 只是文件事实，不改变审核或采用状态。

## 9. 逐段显式采用

- 先读取 `read_project(section='state').reviews` 和 `read_project(section='plan')` 的实际 revision；没有审核记录时 revision 记为 0。
- `record_review` 只用于记录初审意见或退回修改：`stage` 为 `sample`/`finish`/`final`，`status` 只接受 `pending_review`/`changes_requested`。它不执行最终采用。
- 正式采用：`approve_candidate` / `POST /api/projects/{project_id}/adoptions`，必须提供：
  - 真实 `asset_id`、`stage`、`candidate_ref`；
  - 当前 `expected_review_revision` 和 `expected_plan_revision`；
  - 非空 `note`（采用依据）；
  - 严格布尔 `confirm_adoption=true`。
- 采用时服务端会核对候选确实属于本作品、本素材、本成功任务，且是当前登记文件；不满足返回 `409`。`sample` 先采用后，`finish` 才可继续；`finish` 依赖已采用样片。
- 每个分镜单独采用，不能因为一次任务/一个候选成功就批量采用其他分镜。明确采用不等于用户亲自验收；交付时仍要标注“采用依据”和“用户是否亲自确认”。
- 采用后计划/审核版本会变化；后续操作重新读取最新 revision，不要沿用旧值。

## 10. 装配、接点、音轨与成片

1. `read_assembly_edit`：读取相邻接点、当前视频版本、预览时间、`revision` 和 `audio_assets`。
2. 接点审核：`review_assembly_join(project_id, left_id, right_id, expected_revision, status, constraint?, note?)`。`status` 为 `pending`/`approved`/`redo_left`/`redo_right`；`expected_revision` 用装配编辑的 `revision`（来自计划）。接点两侧必须都有当前视频才能 `approved`；更换视频后审核自动过期。
3. 音轨策略：`set_assembly_sound(project_id, expected_revision, policy, audio_asset_id?)`：
   - `segment_native`：保留片段原音（不指定 `audio_asset_id`）；
   - `complete_master`：用本作品已登记的完整音频母版替换各段原音（必须指定 `audio_asset_id`）；
   - `mute`：静音（不指定 `audio_asset_id`）。
4. 预检：`preflight_assembly_edit` / `GET /api/projects/{project_id}/assembly/preflight`。`ready=true` 且无 blockers 才可考虑提交装配；检查缺失视频、接点、采用、音轨和时长。
5. 装配提交：`submit_assembly(project_id, expected_revision, idempotency_key)` / `POST /api/projects/{project_id}/assembly`。
   - 所有参与装配的分镜必须已**显式采用当前候选**；否则 `409 assembly_adoption_required`。
   - 接点必须全部通过且未过期；否则 `409`。
   - 同键同参数返回原任务；未知响应用 `get_submission_receipt` 核对，不换键重提。
6. 装配任务成功后读取 `read_project(section='plan').assembly` 和任务结果；成片是候选，不自动成为“已交付”或“已最终采用”。
7. 成片最终采用（用户明确要求时）：`approve_candidate` 的 `asset_id` 用作品 `assembly_asset_id`（通常 `MASTER`），`stage='final'`，并提供真实候选引用、计划/审核 revision 和非空说明。当前计划变化或接点未通过时不能采用。

## 11. 交付

- 交付前确认用户要的是“候选/成片/仅证据”。不要把预检、生成成功或装配成功说成交付完成。
- 交付内容至少包含：
  - 作品 ID、计划 revision、分镜 ID/标题；
  - 采用的候选引用、审核 revision、采用依据；
  - 任务/批次 ID、最终状态和回执时间；
  - 受控媒体：稳定 `media_ref` 和工具返回的短期 URL（`<工作台 URL>` + `url` 路径，带本人 Bearer 才能访问）；用 `download_media` 取得字节时报告返回的内联字节数或 URL；如果客户端已实际下载，再报告本地文件及大小；
  - 参考/候选对照、技术检查、ASR 等证据及各自局限；
  - 剩余限制（模型未就绪、长任务未完成、未采用、未审核接点、音轨未确认等）。
- 不猜测额外下载 API；远程交付使用 `list_media`、`get_media_url`、`download_media` 和任务/候选回执中的受控引用，不要构造 `/media-file?path=`、`/media/{asset_name}` 或服务器磁盘路径。
- 不能把任务成功、审核通过、采用或装配成功等同于用户亲自验收；交付说明中区分“平台记录的事实”“Agent 的观察”“用户的决定”。

## 12. 失败、重试与幂等总表

| 情况 | 处理 |
| --- | --- |
| `401` 会话失效/无 token | 停止写入，重新认证，重连 MCP；再读取原回执，不重发未知写入 |
| `403` Origin/权限 | 核对允许 Origin、账号和作品归属；不绕过授权、不改 Host 头 |
| `404` 作品/任务/回执 | 核对真实 ID；回执 404 只表示尚未落库，不证明原请求失败 |
| `409` 版本/键冲突/状态冲突 | 重读实际 revision、任务或候选比较；不自动覆盖、不换键提交 |
| `413` 上传超限 | 按单文件/单请求上限拆分或压缩；不要原样重试 |
| `422` schema/blocker | 读取 `blockers`/`detail`，补方案、素材、时长或参数后重新预检 |
| 工具错误（参数 schema） | 按 `tools/list` 的 `input_schema` 修正字段/范围/枚举；不调用工作台，也不要用业务重试绕开 |
| `429` 登录/队列上限 | 登录限流等待窗口；队列上限等待或停止自己的未派发任务 |
| `421`/`403` MCP Host/Origin | 由服务器配置允许 Host/Origin；客户端不能靠请求头修复 |
| 超时/连接断开 | 写入结果可能已保存：先查回执/记录；GET 可重试，非 GET 不盲目重发 |
| `needs_reconcile` | 先按任务类型核对原执行；不自动重跑、不标假终态 |
| 生成失败 | 读取 `error`/`result` 和输入；请用户决定是否新开操作，使用新键 |
| 参考分析失败/未知 | 查 `semantic-jobs/current`、核对回执；不重复启动模型分析 |
| `approve_candidate` 409 | 重读候选、成功任务、计划/审核 revision；不伪造候选或跳过门禁 |

幂等速记：

- **有请求键（`idempotent_hint=true`）**：生成、批次、关键帧、声音、音频编辑/母版、媒体任务、装配；按已上传素材幂等登记的参考导入；核对类工具。相同键 + 相同参数返回原任务/原记录；不同参数冲突。
- **无统一请求键（`idempotent_hint=false`）**：创建作品、上传、新增/修改分镜、整理、审核、采用、装配编辑、参考观察/校订。响应未知先读回补，不能套用生成任务的同键保证。
- **绝不**在未知回执时换一个新键重复计费操作；annotation 只是提示，真实门禁以服务端返回为准。

## 13. 明确禁止

- 不使用自动创作模式、默认方案、默认画幅/时长/种子来替用户开始创作；用户没给参数就先问最少选择。
- 不自动拆分参考、不自动抽帧、不自动运行 ASR/模型分析、不把等距采样当镜头切点。
- 不自动采用候选、不自动装配、不自动恢复/重试失败任务、不自动删除或取消。
- 不宣称工作台“理解/分析/推荐/自动改编”了内容；只报告可观测事实、候选和用户决定。
- 不驱动浏览器标签页、不声称点击过界面、播放过页面或看到过 UI；所有动作只走公开 HTTP/MCP 业务入口。
- 不在运行目录临时跑模型/FFmpeg/Whisper 生成作品产物来补产品缺口；缺能力时说明限制。
- 不读/写服务器任意路径、不伪造服务器路径、不把客户端路径传给服务端路径导入接口。
- 不伪造证据、不把 `NOT_RUN` 写成通过、不把离线测试/文档检查说成真实外部 Agent 或 GPU 验收。

## 14. 最小端到端验收清单（只给 URL/token/愿望，无 UI）

> 真实外部 Agent 客户端验收：**NOT_RUN**。本仓库本次只做 Skill/文档修改与离线文档校验，未执行真实外部 Agent、真实 GPU 生成或跨设备交付。以下清单是验收步骤与证据模板，不是已完成结果。

验收前提：用户只提供工作台 URL、agent-token（或账号）和一个愿望；不手动打开界面、不手动上传/点击/采用。

1. **认证**：`POST /api/auth/agent-token` 成功；`GET /api/auth/me` 返回本人；`GET /api/projects` 返回本人作品。
2. **发现**：MCP `tools/list` 成功，记录工具数量和实际名称（本版本远程 60）；HTTP `/openapi.json` 可读。
3. **愿望**：Agent 复述愿望，向用户确认画幅、时长、参考/原创、方案和是否生成；记录用户回答。
4. **作品**：`create_project` 显式 `creation_mode=advanced`；记录 `project.id`。若响应未知，`list_projects` 核对。
5. **方案**：`list_production_presets` 读事实；用户选一个；`configure_production_preset` 安装；`read_project(section='pipeline')` 记录真实 `stage_id`。
6. **计划**：`create_shot` 传实际 revision、用户原文、时长、画幅/参数和显式 workflow 或 preset_id；记录 `segment.id` 和新 revision。
7. **预检**：`preflight` 用真实 `stage_id`；记录 `valid`、`readiness`、`h3_duration`。若阻塞，补输入/重新选方案，不提交。
8. **生成**：用户授权后保存唯一 `idempotency_key` 和完整参数，`submit_shot` 提交一次；记录 task ID。若未知，`get_submission_receipt` 用原键核对。
9. **轮询**：`get_task`/`read_queue` 轮询到终态；记录状态、等待原因和耗时；不重复提交。
10. **候选**：读取回执中的 `media`/`candidate_ref`，用 `media_ref` 调 `get_media_url` 或 `download_media` 带 Bearer 实际取回字节/内容；记录媒体引用、大小/类型。只有实际检查后才写“已查看/已试听”，否则写“仅登记/未查看”。
11. **采用**：`record_review` 记录意见（如需）；`approve_candidate` 逐段提交真实候选引用、审核/计划 revision、说明和 `confirm_adoption=true`；记录采用 revision。
12. **装配**（多段）：`read_assembly_edit` 读取接点和音轨；逐对 `review_assembly_join`；`set_assembly_sound` 显式选择；`preflight_assembly_edit`；`submit_assembly` 保存键并提交一次。
13. **成片**：轮询装配任务；读取 `assembly.output` 并实际取回媒体；用户要求时用 `approve_candidate(stage=final)` 最终采用。
14. **交付**：输出作品/分镜/任务/候选 ID、受控媒体 URL、采用依据、剩余限制；区分事实/候选/用户决定。
15. **失败路径抽检**：不安装方案直接保存分镜 → `422 creation_plan_required`/`preset_not_installed`（预检/状态显示 `stage_required`/`recipe_unresolved`）；未逐段采用就装配 → `409 assembly_adoption_required`；参考段缺失 → `422`；会话失效 → `401`；长任务 `needs_reconcile` → `reconcile_generation`，不重跑。

验收记录模板（只在真实执行后填写，否则写 `NOT_RUN`）：

```text
外部客户端/版本：
工作台 URL（脱敏）：
认证方式：agent-token
工具发现：远程工具数=____；关键工具名核对=是/否
作品 ID：
方案 ID / stage_id：
计划 revision：
分镜 ID：
预检：valid=____；h3_duration=____
任务 ID / 键：
终态 / 等待原因：
候选引用 / 媒体字节数：
审核 revision / 采用 revision：
装配任务 ID / 成片引用：
真实媒体读取：是/否（未读取写“未查看”）
交付 URL（脱敏）：
剩余限制：
失败路径覆盖：____
结论：PASS / FAIL / NOT_RUN
```

## 附录 A：远程 MCP 工具清单（60）

以下为 2026-10-19 代码快照的固定远程名单；服务端 `tools/list` 为最终权威。所有远程工具都通过同一 Bearer 和既有 HTTP 业务路由执行。

**作品、计划、分镜、素材（21）**

`list_projects`、`upload_material`、`read_guide_settings`、`read_queue`、`list_production_presets`、`configure_production_preset`、`create_project`、`read_project`、`read_script`、`save_script`、`list_tasks`、`get_task`、`list_shot_versions`、`preflight`、`save_creative_draft`、`record_review`、`approve_candidate`、`update_shot`、`arrange_shot`、`create_shot`、`reuse_materials`

**参考（14）**

`import_reference_from_material`、`import_reference_content`、`extract_reference_range`、`extract_reference_audio`、`sample_reference_frames`、`transcribe_reference`、`list_references`、`read_reference`、`start_reference_analysis`、`reconcile_reference_analysis`、`annotate_reference_timeline`、`correct_reference_artifact`、`correct_reference_classification`、`convert_reference_to_shots`

**生成、任务、媒体与装配（25）**

`submit_shot`、`submit_batch`、`get_keyframe_capability`、`submit_assembly`、`read_assembly_edit`、`review_assembly_join`、`set_assembly_sound`、`preflight_assembly_edit`、`check_assembly_continuity`、`submit_keyframe`、`get_speech_capability`、`submit_speech`、`get_submission_receipt`、`reconcile_generation`、`stop_generation`、`list_media`、`get_media_url`、`download_media`、`submit_audio_edit`、`submit_audio_master`、`get_media_capability`、`create_silence`、`inspect_video`、`transcribe_audio`、`export_silent_video`

## 附录 B：仅本机 stdio 的 7 个工具

这些工具存在于本机 stdio（全量 67 个），不在远程 MCP 固定名单中。远程客户端如果需要，只能使用认证 HTTP 原业务入口，并自行承担相同的版本、门禁和审计要求：

| 工具 | 远程/HTTP 替代 |
| --- | --- |
| `update_guide_settings` | `PATCH /api/settings/guide` |
| `import_reference` | `POST /api/projects/{project_id}/reverse-analyses`（multipart `file`） |
| `restore_shot_version` | `POST /api/projects/{project_id}/segments/{segment_id}/versions/{task_id}/restore` |
| `repair_shot_recipe_identity` | `POST /api/projects/{project_id}/segments/{segment_id}/recipe-identity/repair` |
| `control_batch` | `POST /api/projects/{project_id}/batches/{batch_id}/stop` 或 `/resume` |
| `resolve_missing_execution` | `POST /api/projects/{project_id}/tasks/{task_id}/resolve-missing`（严格核实后才能用） |
| `resume_generation` | `POST /api/projects/{project_id}/tasks/{task_id}/resume`（非幂等，可能创建新尝试） |

## 附录 C：相关公开合同

- [HTTP API 与 Agent 接入](../../../docs/public-api-guide.md)
- [MCP 连接说明](../../../mcp_server/README.md)
- [公开开发指南](../../../docs/public-development-guide.md)
