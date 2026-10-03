# 远程客户端优先

2026-10-03 DW-026：先从工作台登录页“连接你的 Agent”取得 `/agent/start`。该入门和 `/.well-known/director-workbench.json` 是公开的非敏感发现入口，详细业务 OpenAPI、作品、任务及媒体仍需本人认证。

远程 MCP 的目标为同一私人工作台 URL 下 `/mcp/`，官方 SDK `mcp==2.2.0` Streamable HTTP、stateless、JSON 响应；启动生命周期由原4100进程管理，不新增服务或生成队列。每次工具调用从当前请求身份取得会话，再走原认证 HTTP。普通客户端无需 Windows Python 或 SSH 管理员凭据。客户端先经账号登录，从会话容器取得本人 Bearer，再配置远程 MCP 请求头，密钥不写共享配置。会话过期重新登录，账号切换更换身份，不能用旧连接假定管理员。

首轮范围支持可配置 Bearer 的标准 Streamable HTTP 客户端；没有 OAuth 登录发现，不能宣称只支持 OAuth 的所有宿主可用。仍使用既有私人网络/客户机隧道，不开放 ComfyUI、公网或扩大 Host/Origin 范围。实际部署与干净 Mac 客户端产品验收以唯一 ISSUES 的 DW-026 为准；代码存在不代表已上线。

上传工具 `upload_material` 通过原 multipart 上传（单文件最多16MiB的base64载荷）；较大素材使用原 HTTP multipart，不传远程客户机磁盘路径给服务器路径导入工具。创建、上传等非幂等操作未知响应先读保存结果核对。页面工具只记录受控表现动作；用户同源浏览器允许指定标签页并确认实际呈现。没有浏览器回执不能声称点击或播放；业务保存与提交不由表现层重复执行。

当前能力按 `/agent/capabilities?group=creation` 或 `presentation` 分组读取，映射会与原 OpenAPI 和 MCP 工具 schema 回归核对。完整工具继续共用原 HTTP，其他业务逐项按其 schema 接续；远程大参考文件分析等不属于首轮完整产品验收。

以下保留家庭服务端的本机 stdio 配置，供主动诊断；它不是普通 Mac 用户接入前置。

# 导演工作台 MCP 连接

本机工具使用 stdio，使用官方 Python SDK `mcp==2.2.0`。工作台 HTTP 服务须独立运行。工具只经 HTTP 访问业务，不导入后端、修改数据库或启动 ComfyUI。下方 Windows 路径示例用于家庭电脑上的本机 MCP 进程；Mac 作为客户机时，Agent 可直接通过工作台有文档的 HTTP API 访问同一业务能力，登录与局域网设置见[私人模式接入](../docs/private-mode-integration.md)。

在项目目录安装可选依赖：

```powershell
.venv/Scripts/python.exe -m pip install -r requirements-mcp.txt
```

在支持 stdio 的 MCP 客户端添加下列配置，并把绝对路径和端口替换为实际部署值：

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

**私人账号服务还需要登录会话。**先用 `POST /api/auth/login` 以目标账号登录，从 HTTP 客户端的 Cookie 容器取出 `director_session`，再由可信的 MCP 客户端启动环境把它传给子进程的 `DIRECTOR_WORKBENCH_SESSION`。不要把密码或会话值写进上面的共享配置、作品档案或日志。服务端按该会话的账号限定作品与素材；只有 `DIRECTOR_WORKBENCH_URL` 而没有会话时，工具会返回 401。会话过期或账号切换后重新登录并重启 MCP 连接。具体登录与部署边界见[私人模式接入](../docs/private-mode-integration.md)。

客户端通过工具发现获取参数 schema 与说明。常用工具包括 `list_projects`、`read_project`、`list_tasks`、`get_task`、`list_shot_versions`、`restore_shot_version`、`update_shot`、`submit_shot`、`get_submission_receipt`。先列作品获取 ID，再读取分镜并预检。普通创作使用高层输入，技术步骤仅供主动诊断。预检不会提交生成。

根据 2026-10-03 用户指示，开发期间不保留旧流程软件兼容。MCP 是当前工作台 HTTP 接口的薄适配，与服务端一同发布；写操作不再额外读取 OpenAPI 以判断旧后端版本，不静默降级或回退另一业务入口。HTTP 权限、版本冲突、输入检查、幂等和不确定回执处理继续保留。已有作品和历史记录属于用户数据，保留数据不等于维护旧软件流程。清理边界见[旧流程退役](../docs/legacy-flow-retirement.md)。

结果统一为 `{ok: true, data: ...}` 或 `{ok: false, error: ...}`。业务 HTTP 错误保留 `http_status`、`detail`，不应仅凭 MCP 传输成功判断操作成功。参数 schema 错误由 SDK 返回。任务分页使用服务返回的 `next_offset`。

MCP 适配器仅连接本机 HTTP 地址；私人账号认证由工作台的 Bearer 会话完成，不依赖 MCP 工具参数里的用户名。工具的只读注解不等同于服务端权限系统。写工具目前支持创作草稿、已有分镜局部修改、跨作品素材复制和初审/退回意见，支持带请求标识的单段生成，已提供单任务停止请求与恢复工具，通过 approve_candidate 提供显式采用，也未自动修改用户 MCP 配置。网络请求不自动重试。端口未设置时为 4100。

验证：官方 SDK 客户端真实启动 stdio 子进程，完成工具发现、状态读取与 HTTP 结果对照、任务分页和未知作品错误检查。适配回归位于 `tests/test_mcp_adapter.py`；可选依赖未安装时这部分测试跳过。

SDK 依据：[官方文档](https://py.sdk.modelcontextprotocol.io/)、[stdio 客户端](https://py.sdk.modelcontextprotocol.io/client/transports/)。制作流程将由 Skill 承担，此文件只维护 MCP 的安装、连接与返回约定。

写工具要求 expected_revision，取自对应作品状态记录，不存在时为 0。409 后重新读取并比较，不自动覆盖。网络中断时写入可能已完成，错误标记 outcome_unknown；先核对服务器记录。director-mcp 来源标签不是认证身份。采用权限来自可信会话与用户委托，不能由 source 标签授予。

制作流程 Skill 位于 [director-workbench](../.agents/skills/director-workbench/SKILL.md)，可按宿主机制加载；发布后同一内容通过 `/agent/skill/SKILL.md` 分发，不自动安装到用户全局目录。原私人HTTP纯文本生成、任务与候选已有Mac真实样片证据；DW-026外部发现／远程MCP／页面表现的新版本仍待受控部署和干净客户端验收。生成成功不代表审核、采用或完整交付已通过。

参考列表工具返回分页摘要，详情保留原分析与人工校订字段；当前分页在 MCP 适配层进行，HTTP 服务仍返回完整列表，尚非服务端分页。参考文件支持本机路径导入，随后可启动后台分析与核对回执。

参考时间线备注可通过 annotate_reference_timeline 保存为待复核，要求分析文档 revision。MCP 与后端使用同一发布版本；版本冲突由公开 HTTP 业务接口校验，不在适配器预探测另一版本的契约。

submit_shot 强制提供 idempotency_key，直接提交现有 HTTP 接口。相同作品、键和参数返回原任务；不同参数冲突。批次 submit_batch 可提供 idempotency_key，装配 submit_assembly 要求请求键；均调用相同 HTTP 接口，没有自动重试。生成回执不等于渲染成功，使用 get_task 查询。

get_submission_receipt 可在 ComfyUI 离线时按作品/请求标识查询已落库任务，不重新提交。404 可能表示原提交尚在处理中，保留原键核对。

reconcile_generation 核对原执行并更新原任务/结果档案，不创建失败后的重试。核对确认失败后需另行决定是否重新生成；正在受执行器监控的任务返回冲突，继续查询状态。

create_project 创建空白作品并通过 select=false 保留人的当前选择。创建不幂等，使用返回的实际项目 ID，丢失响应后先列出作品核对。

create_shot 追加分镜草案并强制提供 expected_revision，由 HTTP 接口检查计划版本。制作流程见 Skill。

DW-023/024 高层创作（代码交接，部署另验）：新作品默认auto；create_shot与update_shot接受generation（画幅／声音含义／seed），不需要先装preset或猜stage_id。preflight默认使用auto，submit_shot／submit_batch可传预检的expected_revision；执行仍是同一HTTP业务服务。已有作品不自动迁移，明确切换auto后才解析。输入字段、支持组合和真实小样限制见[普通分镜创作合同](../docs/automatic-shot-creation.md)；本适配不调用ComfyUI或另建队列。

`read_script(project_id)` 返回完整剧本、场次与独立 `revision`（未建档为 0）。`save_script` 以 `expected_revision` 完整保存 `title`、`text`、`scenes` 与 `target_duration_seconds`，写入草稿并保留历史；目标时长不填时传 null，清除目标时长。沿用已存在的场次 ID，被分镜引用的场次不能删除。场次时长是叙事预算，不是生成镜头时长。

从场次建立分镜时，先 `read_script` 与 `read_project(section="plan")`，再 `create_shot` 传两个版本：`expected_revision` 为计划版本、`expected_script_revision` 为剧本版本，并指定 `script_scene_id`；另行给出该分镜的 `duration_seconds`。已有分镜用 `update_shot(changes={"script_scene_id":"SC01","expected_script_revision":3}, expected_revision=7, ...)` 关联；空串解除关联。两者均不提交生成。关联字段由同一 HTTP 接口检查剧本及计划版本。409 后重新读取比较；未知写入回执先核对剧本或计划，不盲目重试。协议回归见 `tests/test_mcp_script.py`。

list_production_presets 与 configure_production_preset 提供制作方案发现与配置。配置目标作品且不切换当前选择。配置不下载模型、不启动生成。

convert_reference_to_shots 将已保存参考转成制作草案。已有来源关联防止重复新增，并保留制作分镜的后续修改；工具不会运行参考分析或生成视频。

参考转换工具要求 expected_revision，直接调用同源转换接口。页面与 MCP 随同发布，不维护旧后端的兼容分支。

start_reference_analysis 启动已有参考的后台内容分析；read_reference(section=semantic_job) 查询回执；reconcile_reference_analysis 核对服务重启后的完成回执，不重新运行分析。工具列表以客户端发现结果为准。启动分析不是幂等操作：活动任务复用原任务，终态后再次调用会新建任务；回执不确定时先查询，不自动重发。

import_reference 读取 MCP 进程所在机器上的绝对音视频路径，经 multipart 上传到作品并接受参考提取任务。返回 import_task_id 后使用 get_task/read_reference 查询，提取中的 ASR 与其他 GPU 工作共同排队；上传回执不代表时间线已提取。不接受 URL、不自动重试、不启动语义模型。跨设备客户端应先把素材传到 MCP 所在机器或通过页面上传。

arrange_shot 复用分镜结构操作接口，支持复制、移动、拆分和相邻合并，强制 expected_revision，由同源接口检查版本。参数由工具 schema 和说明维护；流程与候选失效处理见 Skill。

submit_speech 接入独立台词任务，复用查询与请求标识回执工具；reconcile_generation 也可核对声音完成回执。新任务使用 AuK-Flash，支持 gen_seconds 和 seed；历史 IndexTTS 任务保持原引擎解释。API/MCP 已接通，真实 Flash 工作台生成验收仍待完成，文件就绪不代表生成验证通过。结果登记为独立音频候选，使用流程见 Skill。

执行器契约测试依赖独立 AuK 环境的 numpy/soundfile：用该环境 Python 运行 tests/test_generate_auk_flash.py。工作台环境的 pytest 使用 --ignore=tests/test_generate_auk_flash.py；这只是拆分测试环境，交付验收须同时运行两组，不向工作台添加模型依赖。

get_speech_capability 只读查询本机声音能力缺项，不启动模型，与页面复用相同检查接口。

无音频素材时，`submit_speech(mode="design", voice_description="年长男性，低沉沙哑，克制缓慢", text="台词", project_id="...", idempotency_key="...")` 可设计新声音；不要传 `voice_reference`。默认 `mode="reference"` 保持原行为，必须传作品中的 `voice_reference`，不要传 `voice_description`。两种模式均支持 `gen_seconds`、`seed`，生成的仍是待试听候选，可作为后续台词参考；接口可用不等于音色已确认。

get_keyframe_capability(mode) 查询对应图片模型与节点；submit_keyframe 支持 reference 改图及 text 文字首图，复用任务查询、提交回执与结果核对。参考图模式已通过 MCP stdio→GPU→素材登记和页面解码验证；文字模式已通过页面提交→GPU→候选登记及刷新恢复，另有 MCP→API 回归。使用参数和流程见项目 Skill，不代表生成内容已采用。

read_project(section="creative-template") 提供系列设定快照，包含来源版本与适用于新作品的 values。页面与 MCP 共享后端筛选规则；创建和保存流程见项目 Skill。

submit_assembly 要求计划版本及请求标识，按计划中每个分镜的当前视频装配，不依赖旧审核记录。直接提交同源装配接口；回执与结果查询复用已有工具。

装配前可用 `read_assembly_edit` 读取相邻接点、当前视频版本和音轨方案；`review_assembly_join` 保存绑定当前版本的连续性约束与通过/重做决定；`set_assembly_sound` 选择当前作品已登记的完整音频母版替换各段原音，或保留各段原音；`preflight_assembly_edit` 无 GPU 地核对缺失视频、过期接点和音轨。每次写入都需最新 plan revision，正式装配仍由 `submit_assembly` 单独提交。详见 `docs/assembly-edit.md`。

DW-012/013/014 增加 `read_queue`、`submit_batch`、`control_batch`、`read_guide_settings`、`update_guide_settings`。分镜状态返回服务端 `readiness`，已有 validate/preflight 返回同源结构化原因、允许动作和下一步。指南按账号保存阅读进度，不调用生成；任务在资源忙时仍能持久排队。取消、停止与重启恢复沿用原任务接口，MCP 不实现调度。参数、状态和附加表部署约定见 [共享队列与指引](../docs/shared-queue-and-guide.md)。本轮只有隔离无 GPU 回归，完整在线验收仍待完成。

`list_shot_versions` 按作品和分镜分页读取生成历史，`generated_at` 是生成完成时的 UTC 系统时间，`snapshot` 展示当次已记录的提示词、首尾帧、音频等输入；旧任务缺少完整输入时 `snapshot_complete=false`。`restore_shot_version` 以历史任务 ID 和当前 `plan.revision` 恢复版本，返回单独的恢复时间；恢复不会改写原生成时间。两者都使用当前登录会话，由后端校验作品归属。409 后重新读取计划和历史；丢失写入响应时先查询当前版本，不直接重发。

resolve_missing_execution 用于经核实已结束但历史丢失的非声音待核对任务：明确传 confirm_execution_ended=true 和非空核实说明（不超过2000字符）。必须先核实旧执行进程已结束并检查输出，不能仅凭超时调用。服务端要求 ComfyUI 在线、全队列为空且原历史为空；成功只收尾原任务为 failed，保留回执和审计，不生成重试。响应不确定先 get_task 核对。

stop_generation 只请求停止，按返回状态继续查询，stop_requested 不等于 stopped。resume_generation 使用现有作用域恢复 API，先读取原任务、核对原执行历史；可能返回原成功任务或创建新的尝试，非幂等操作，必须使用返回 ID 接续。声音/图片目前只核对原回执。未知响应先 get_task/list_tasks 查证，不自动重发。record_review 保留 final 等阶段的待审核/退回意见；显式采用使用 approve_candidate。

### 音频编辑

`submit_audio_edit` 使用本作品的 source_asset_id 与可选 background_asset_id，提交 CPU 裁切、线性增益、淡入淡出和带偏移背景混音；只产生独立候选，不覆盖原音轨。直接使用 `/audio-edit-tasks` 公开业务接口。PCM16 WAV、同采样率/声道、最长120秒。保留原请求键查询，未知响应不换键重发。

correct_reference_classification 显式校订参考内容类型及理由，要求分析版本。只确认分类，不批准整个分析、不触发分析或转换；模型重分析保留校订并另存分类候选。409 后重新读取比较，参数以工具 schema 为准。

correct_reference_artifact 显式校订单个参考工件。先 read_reference 获取实际 artifact_id 与分析 revision，再传 project_id、analysis_id、artifact_id、完整替换的 summary、必填 expected_revision；可选 confidence 为0到1。只将该工件标为 reviewed，不批准整份分析、不重分析或转换。409 后重读比较，未知响应先核对该工件及版本，不盲目重发。每次成功保存把此前 summary/user_status/status/provenance/confidence 与 revision、recorded_at 追加至 review_history，并保留已有 original_candidate；缺失字段记 null，recorded_at 为本次校订的 Unix 秒。历史从此功能启用后的保存开始记录，过去已覆盖的人工内容无法恢复。

approve_candidate 允许受用户委托的 Agent 检查实际候选后显式采用 sample/finish/final。必填 candidate_ref、expected_review_revision、expected_plan_revision、非空 note 与严格布尔 confirm_adoption=true；1不能替代true。先读取作品state/plan，直接调用adoptions接口，不回退reviews。后端原子核对候选/版本/归属；409重读比较，未知响应读取审核和候选核对，不自动提升版本或重发。不表示用户亲自批准，不在生成完成后自动调用，也不自动提交装配。

## 媒体工具：原生创作入口

网页“任务与成片 → 媒体工具”与以下 MCP 工具共用工作台 API：

- `get_media_capability`：查看本机可用操作与缺失资源。
- `create_silence`：生成指定时长、采样率、声道的静音 WAV。
- `inspect_video`：抽帧、接触表及视频技术检查。
- `transcribe_audio`：音视频转写、逐词时间戳及预期台词对照。
- `export_silent_video`：保存独立无声审片候选。

以上写操作使用 `POST /api/projects/{id}/media-tasks`，输入来源只能是本作品登记素材 ID，不能传任意磁盘路径。调用前保存 `idempotency_key`，回执不明先 `get_submission_receipt`，再 `get_task`；不能换键重复生成。任务结果的 `outputs` 提供可访问候选，`report` 提供检查数据；使用现有停止/核对/恢复任务接口。

技术检查和 ASR 台词对照不等于人工验收，也不能批准画面、表演或音色。创作所需静音、抽帧、转写和无声导出应通过这些入口完成，不在客户端直接运行 FFmpeg/Whisper 补做。
