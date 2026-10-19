# 公开开发指南

本指南面向维护代码的 Agent 和开发者。使用现有工作台的客户端只需要维护者提供的私人 URL 和本人账号，见 [HTTP API 指南](public-api-guide.md)与 [MCP 连接说明](../mcp_server/README.md)。

仓库根目录就是工作台项目目录，存放筛选过的开发源代码。它不包含 ComfyUI 安装、模型、工具环境、账号、作品或生产配置。目前服务端依赖 Windows 路径和本机媒体工具；从空白设备完整部署仍须独立验收。客户端设备无需安装服务端 GPU 环境。

## 架构与修改位置

| 位置 | 职责 |
| --- | --- |
| [src](../src) | React 页面、当前会话请求、计划与候选展示；`src/api/client.ts` 统一部分面板的 JSON 请求、错误映射、超时/取消和媒体 URL，重型面板在 `App.tsx` 中按需加载 |
| [backend/private_app.py](../backend/private_app.py) | 认证服务入口，校验本机账号、管理员和存储配置 |
| [backend/app.py](../backend/app.py) | 工作台级配置、共享业务函数、任务调度初始化，以及按既有顺序注册领域路由；保留兼容导出和 `backend.<name>` monkeypatch 语义 |
| [backend/routers](../backend/routers) | 15 个领域路由模块：`private_series`、`project_catalog`、`plan_editing`、`material_library`、`reverse_analysis`、`project_transfer`、`workbench_status`、`task_entry`、`task_media`、`batch_assembly`、`gpu_idle`、`queue_scheduler`、`task_control`、`events_media`；`__init__` 只提供包说明 |
| [backend/private_auth.py](../backend/private_auth.py)、[private_workspaces.py](../backend/private_workspaces.py) | Cookie/Bearer 会话、账号与作品归属 |
| [backend/task_scheduler.py](../backend/task_scheduler.py) | 同一服务进程的持久任务调度 |
| [backend/remote_mcp.py](../backend/remote_mcp.py) | 远程 MCP 入口，只开放固定的创作工具子集 |
| [mcp_server/server.py](../mcp_server/server.py) | HTTP 业务的 MCP 薄适配，不另建执行队列 |
| [tools](../tools)、[共享制作脚本](../.agents/skills/aigc-video-production/scripts) | 后端受控执行器；外部客户端通过工作台入口使用 |
| [storage_layout.py](../production/storage_layout.py) | 读取本机存储根；路径配置不由客户端传入 |
| [tests](../tests)、[contracts](../contracts) | 隔离回归和制作输入合同；`tests/conftest.py` 暴露共享 pytest fixtures，`tests/support.py` 集中放置接收 client/project 的纯 helper |

UI、HTTP 和 MCP 共用后端授权与任务记录。任务先冻结输入和归属，再进入执行器；产物登记为候选，审核和采用需显式操作。技术报告、任务成功或页面播放回执均不能替代人的内容验收。

## 平台边界：确定性工具

工作台是确定性工具，不是智能体。理解、判断、选择、改编、审核、采用与验收属于用户或其 Agent；工作台只存储、校验、渲染、对照、装配和记录。维护代码时遵守：

- **不代选、不兜底**：缺少显式制作方案、workflow 或参考范围时返回结构化阻塞。准备度在无法把明确 workflow 解析到已安装方案时返回 `recipe_unresolved`；没有任何显式选择时返回 `stage_required`，并把 `installed_pipeline_stages`、`available_presets` 等事实写入 blocker 的 `options`，不按第一个可执行 stage 或默认配方继续。
- **模型只产出候选**：模型结果命名为 `observations`、`model_candidates`、`draft_seed` 等，必须带 `provenance`、`user_status` 和待校订状态；不得自动写入 performance/prompt/计划，也不得自动审核或采用。
- **事实而非推荐**：模板目录返回 `available_templates` 及 `installed`/`missing_models` 等事实，不返回 `recommended`、默认项或无依据排序。
- **不宣称理解**：UI、API、MCP、文档和提示词只描述可观测事实与显式动作，不写“平台已理解/已分析/已改编/已推荐”。
- **入口一致**：能由 HTTP/MCP 完成的能力必须有对应入口和回归测试，不能只留 UI。UI 是可选的人工接管层。
- **保留显式路径**：调用方给出明确选择后，工作台仍完成生成、对照、装配和记录。

新增或修改相关行为时，按 [HTTP API 指南](public-api-guide.md)与 MCP 说明同步更新合同，并补充边界回归（缺显式选择时不写计划、不建任务、不产生业务写入）。

路由拆分只改变代码组织，不改变公开接口、幂等键、`needs_reconcile`、`operationId`、任务冻结或路由注册顺序。新增领域接口应在对应 `backend/routers/` 模块中实现，并通过 `app.py` 的既有注册位置接入；需要被旧测试或其他模块 monkeypatch 的名称继续由 `backend.app` 重新导出。

`tools` 中也保留早期开发辅助脚本；部分默认工作流不随仓库分发，须先检查参数并提供实际输入，不能将无参数脚本当作新设备制作入口。正式创作由 UI/API/MCP 负责登记与执行。

## 环境和存储

本机运行布局以 `D:\Comfy-Desktop` 为根。ComfyUI 本体、Python 环境、自定义节点、模型、工具、热缓存和研究/测试临时目录均在该根下；工作台源代码是独立目录，不在该根下。`ComfyUI-Shared` 保持 D 盘真实目录，不建立跨盘 junction 或符号链接。账号、数据库、运行状态、构建与测试临时文件也属于本机数据，不纳入 Git。

输入、输出、生成素材和长期作品资产统一放在本机配置的 E 盘目录。引擎 input/output 是受控交换区；私人作品按 `用户/series/系列/作品` 登记。`user001`、`user002` 等账号隔离，通过服务端会话与登记验证归属，不能靠客户端传入用户名或磁盘路径授权。

服务端维护者另行准备：

- Python；目前维护环境使用 3.13，工作台使用项目 `.venv`，ComfyUI 和 AuK 使用各自专用解释器。
- Node.js/npm；前端按 [package-lock.json](../package-lock.json)安装。
- ComfyUI 桌面安装及相应模型、节点、媒体工具；检查当前能力接口返回的缺项，不根据代码存在推定模型可用。

工作台数据根的 `config/storage.json` 和 `.config/director-workbench/local-storage-policy.md` 是不公开提交的本机配置（默认数据根 `D:/software/JetBrains/python_workspace/director-workbench-data`）。以下是符合维护环境分层的脱敏示例；根须为绝对真实目录、不重叠，共享兼容作品根在管理员 `series` 下的结构例外由存储模块校验：

```json
{
  "schema_version": 1,
  "roots": {
    "input": "E:/Comfy-Assets/input",
    "output": "E:/Comfy-Assets/output",
    "workspaces": "E:/DirectorWorkspaces/user001/series",
    "private": "E:/DirectorWorkspaces"
  }
}
```

未配置时共享存储模块仍有 D 盘开发默认值，因此维护者需在正式使用前显式配置资产根，并保证 ComfyUI 的实际 input/output 与工作台一致。已有登记路径和历史映射由本机配置保留，不发布私人记录。改变配置会影响路径解释，须核对真实文件并验证读写，不能当作已完成资产搬迁。清理数据遵守本机 safe-delete 两阶段规则。

## 构建与测试

空白开发机器可先将[仓库](https://github.com/czz20000522/director-workbench)检出到一个独立目录；仓库根目录就是项目目录。已有安装机器使用源代码分支更新，避免以新的 clone 覆盖安装、资产或未提交修改。安装与测试不读取私人制作文档。

以下命令在 PowerShell 的项目目录执行：

```powershell
Set-Location <项目目录>
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
npm ci
npm run build
.\run_tests.ps1 -Suite core -Check
.\run_tests.ps1 -Suite core
```

`python` 应由维护者选择已安装的基础 Python；既有 `.venv` 不需要重建。前端构建执行 TypeScript 检查并输出 `dist`，不代表后端或 GPU 生成通过。Python 依赖已包含 MCP；[requirements-mcp.txt](../requirements-mcp.txt)提供同一依赖入口。`requirements-dev.txt` 只装开发检查工具（ruff、pytest-cov、pytest-timeout），不进入生产环境。

完整离线回归用 `run_tests.ps1 -Check`、`run_tests.ps1`。统一入口调用 [run_project_tests.py](../tools/run_project_tests.py)：`all`、`core`、`auk`、`browser` 是完整回归/合并门的可选 suite；核心组在工作台 `.venv` 执行 pytest，AuK 组在 `ComfyUI-Shared/tools/AuK/.venv` 执行声音执行器 unittest。缺少 AuK 环境时完整检查失败，不能把核心组通过报告为完整通过，也不向 API 或 ComfyUI 基础环境安装模型依赖。报告和临时文件保存在工作台数据根的 `runtime` 下（默认 `D:/software/JetBrains/python_workspace/director-workbench-data/runtime`），不提交。

### 分层测试入口

测试按改动边界分层，完整 `core`/`browser` 是合并门，不是每次编辑的默认成本：

| suite | 内容 | 何时运行 |
| --- | --- | --- |
| `fast` | `tools/test_impact.toml` 中 `[fast]` 列出的纯单元/契约测试；不启动服务、媒体或浏览器，硬预算 60s | 每次编辑后的第一轮反馈 |
| `affected` | 调用 [select_tests.py](../tools/select_tests.py)，只跑与 `--base`（默认 `origin/main`）以来改动相关的 pytest；`-WithBrowser` 额外跑选中的浏览器驱动 | 提交前或 PR 的受影响面 |
| `core`、`browser` | 完整核心组与真实浏览器验收 | 合并、发布，或共享契约/依赖/CI 变更 |

```powershell
.\run_tests.ps1 -Suite fast
.\run_tests.ps1 -Suite affected              # 默认对比 origin/main，含未提交与未跟踪文件
.\run_tests.ps1 -Suite affected -Base HEAD   # 只对比当前未提交改动
.\run_tests.ps1 -Suite affected -WithBrowser # 同时跑 selector 选中的浏览器驱动
```

选择器是确定性、可解释、fail-closed 的：测试文件变更只选自身；源码变更按 [test_impact.toml](../tools/test_impact.toml) 的 ownership 映射和 Python import 反向依赖选相关测试；核心 schema、路由聚合、认证/存储/任务入口、依赖清单、CI/发布与公共入口等共享契约自动升级为 full core；文档变更只跑公开源/链接门禁；前端改动只选挂载该组件的浏览器驱动；没有任何映射命中时不会静默跳过，而是升级到 full core/全部前端驱动并在输出中要求补映射。映射目标不存在（测试被删除或重命名）会作为配置错误报出，因此移动测试时必须同步更新映射。

需要单独查看或调试选择结果时：

```powershell
.\.venv\Scripts\python.exe tools\select_tests.py --explain
.\.venv\Scripts\python.exe tools\select_tests.py --json
.\.venv\Scripts\python.exe tools\select_tests.py --base origin/main --check-config
```

pytest 通过 `pytest-timeout` 给每个测试设置 90s 硬超时，挂起按失败处理；`requirements-dev.txt` 安装该插件（`.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt`）。确实需要更久的测试用 `@pytest.mark.timeout(<秒>)` 显式放宽，不能靠移除超时让套件通过。

CI 的公开边界 job 仍使用自己的标记过滤生成不依赖宿主工具的回归集；本地按上面的分层入口运行，两者通过同一份 pytest 断言保持行为一致。

优先运行与修改相关的回归，再按交付范围执行完整入口。离线回归、在线业务、真实 GPU 输出与跨设备访问分别记录。

### 浏览器验收组

`-Suite` 还接受 `browser`，运行 [tests](../tests) 下的 `*.browser.mjs` 真实浏览器验收：用系统 Edge 打开实际页面并操作真实界面。该组需要 node 与系统 Edge，浏览器自动化库 `playwright` 已在 `package.json` 声明；缺少 node 或项目解释器时该组判为失败而不是跳过。

```powershell
.\run_tests.ps1 -Suite browser
```

驱动通过命令行参数取得夹具，契约如下。运行目录必须是绝对路径且尚不存在：相对路径或已存在的目录会在 `backend/user_context.py` 触发 `AccessDenied`。多数驱动不需要额外参数，其余按下表取值，由套件自行准备，不需要手工素材。

| 驱动 | `argv[2]` 起 |
| --- | --- |
| `enhancement`、`quick_generation` | 项目解释器绝对路径，然后一个尚不存在的绝对运行目录 |
| `client_restore` | 可解码的 MP4，由 `tests/client_restore_media.py` 先编码 |
| `media_qc`、`creative_review` | pytest `--basetemp` 目录，由对应 `tests/test_*.py` 先写入 |
| `guide_joins` | 运行目录，其夹具由 `tests/test_guide_joins.py` 先写入 |

浏览器验收覆盖真实界面与真实媒体解码，但不覆盖真实 GPU 生成，也不替代跨设备访问验收。

### 公开仓库的 CI 门禁

[public-source.yml](../.github/workflows/public-source.yml) 在 PR 与 `main` 上执行四个 job：`public-source` 检查公开源码白名单、敏感模式、Python 语法、文档相对链接，并校验测试影响映射的目标仍存在（测试删除/重命名后映射过期会失败）；`python-lint` 运行 `ruff` 的非侵入规则（语法错误、未定义名称、断言误用）；`frontend-build` 执行 `npm ci && npm run build`；`core-tests` 在 Windows runner 上运行 fast core。

CI 的 `core-tests` 使用 `pytest -m "not host_tool and not slow and not windows"`，覆盖不依赖本机 FFmpeg、ASR 解释器、ComfyUI 安装或 PowerShell/junction 的用例。[pyproject.toml](../pyproject.toml) 定义 `slow`、`host_tool`、`windows` 三个标记；本地 `run_tests.ps1 -Suite core` 仍运行完整核心组，标记只用于 CI 筛选，不改变断言或跳过结果。

CI 不安装 ComfyUI、模型或宿主媒体工具，也不运行 AuK 与浏览器验收。CI 通过代表公开边界、前端构建与 fast 后端回归通过，不代表完整主机回归、在线业务或真实生成通过；完整回归仍按本机入口执行。

依赖已安装节点源码的主机探针还需相应 ComfyUI 安装；缺少主机资源导致的跳过须在结果中说明。通过其余测试不能替代该探针或真实 GPU 验证。

## 账号与服务启动

账号由服务器维护者在本机交互创建，用户名格式为 `user` 加三位数字。下面的 `user001` 只是示例：

```powershell
.\.venv\Scripts\python.exe tools\private_accounts.py create user001
.\start_private_workbench.ps1 -AdminUsers user001
```

账号工具不回显密码；账号摘要留在工作台数据根的 `runtime/director-private`（默认 `D:/software/JetBrains/python_workspace/director-workbench-data/runtime/director-private`）。`start_private_workbench.ps1`、`start_backend.ps1` 和 `start_workbench.ps1` 使用 `backend.private_app:app`、单 worker，默认监听 `0.0.0.0:4100` 并服务已构建的页面；启动时自动生成 loopback、局域网、Tailscale IPv4 和主机名的精确 Origin 白名单。只需本机访问时显式传 `-HostAddress 127.0.0.1`。不要以不带认证的 `backend.app:app` 对外提供服务。

首次启动允许没有 `projects/catalog.json` 或默认作品：服务以空作品状态启动，不在导入时创建示例作品、计划或私人 manifest；本人作品列表为空，通过 UI/API 创建实际作品后才登记数据。仍须先安装依赖、构建页面、创建账号、指定管理员并配置资产根，不能从已有生产环境复制私人目录作为部署模板。

公开源代码导出的隔离回归已验证：合成账号环境无 catalog/plan 时，私人应用可加载，匿名发现、登录和空作品列表正常。此证据不等同于空白机器的 ComfyUI/模型安装、完整浏览器使用或 GPU 制作验收。

常用本机启动配置如下。它们均留在部署环境，不能提交凭据或实际网络地址：

| 参数/环境变量 | 用途 |
| --- | --- |
| `-AccountsFile` / `DIRECTOR_ACCOUNTS_FILE` | 哈希账号文件，须在允许的服务器根内 |
| `-WorkspaceRoot` / `DIRECTOR_PRIVATE_ROOT` | 私人作品根，须通过服务端允许列表与无链接校验 |
| `-AdminUsers` / `DIRECTOR_ADMIN_USERS` | 已创建管理员，逗号分隔 |
| `-HostAddress`、`-Port`、`-Origins` | 监听地址/端口和允许的完整浏览器 Origin；未传 `-Origins` 时自动生成精确列表 |
| `DIRECTOR_COMFY_URL` | 内部 ComfyUI 地址，默认 `http://127.0.0.1:8188` |
| `DIRECTOR_COOKIE_SECURE=1` | HTTPS 部署使用 Secure Cookie |

启动时使用维护者配置的私人作品根；需要覆盖时显式传 `-WorkspaceRoot`，仍须满足服务端允许列表。`start_workbench.ps1 -Dev` 可启动 Vite 与私人后端，用于本机调试；它固定示例管理员 `user001`，需要先创建账号。自定义部署优先使用显式参数的私人启动入口。

## 多设备部署和验收

ComfyUI 引擎恢复使用正式入口 `tools/restore_comfy_engine.py`。先用工作台解释器执行该文件（默认只检查安装、配置和端口，不启动）；仅在已授权的维护窗口加 `--launch`。它读取本机资产配置，使用桌面版 `.venv`、D 盘共享模型/temp 和配置的 E 盘 input/output，监听本机 8188。引擎端口被占用或上一启动仍存活时拒绝重复启动；健康等待超时须核对已有进程和日志，不能盲目重发。入口使用 Windows 隐藏进程启动，日志与启动回执保存在隔离 runtime，不提交；不停止现有引擎、不提交生成。完整引擎启动后的窗口、唯一监听和模型加载仍需实际验收。

现有私人网络可通过客户机 SSH 隧道访问服务器 `127.0.0.1:4100`，例如 `ssh -N -L 4100:127.0.0.1:4100 czh@<服务器地址>`，也可由维护者配置指定监听地址及精确 Origin。浏览器 Origin 包含协议、主机和端口，不带末尾斜杠；脚本会自动收集本机可用 IPv4，固定域名或反向代理则显式传 `-Origins`。客户端接入工作台服务，不直接公开 ComfyUI 端口。尾网、SSH 用户/密钥与地址由维护者通过私人渠道提供。

`start-director-services.ps1` 是同时管理 ComfyUI 和工作台的协调启动器，默认 `-WorkbenchPort 4188` 是为现有服务编排和 SSH 转发约定保留的值（脚本把客户端本地 `14188` 映射到服务器工作台 `4188`）。需要统一使用 4100 时显式传 `-WorkbenchPort 4100`；单独运行上述工作台脚本则继续使用它们各自的 4100 默认值。无论选择哪个端口，浏览器 Origin 都必须包含实际端口，必要时通过 `-Origins` 覆盖自动列表。

跨设备验收至少检查两条链路：远端浏览器打开与页面完全相同的工作台地址（协议、主机、端口必须一致），登录后上传一个素材，再打开一个已有图片或视频；同时从服务器本机重复一次。若上传只显示 `Failed to fetch`，先看浏览器请求是否命中工作台端口，而不是只打开了 Vite 原型端口；再确认启动脚本的 `-Origins` 包含浏览器地址的完整 Origin。认证层会对允许的 Origin 回应上传所需的 `OPTIONS` 预检，未列入白名单的来源仍会被拒绝。媒体地址由工作台返回为同源 `/media-file?...`，不要把 `127.0.0.1` 或服务器磁盘路径复制到另一台设备的地址栏。

上线前先检查登录、`/api/auth/me`、`/api/health`、本人作品列表与未授权访问拒绝。`health.ok=true` 只说明工作台处理了请求，还需读取 `comfy.online`；声音、图片与媒体工具各自查看 `/api/speech-capability`、`/api/keyframe-capability`、`/api/media-capability`。模型/节点缺失应由能力检查及预检返回，不能盲目提交生成。

需要 GPU 或跨设备验收时，按 Issue 的明确条件使用公开业务入口创建测试作品、提交一次有授权的小样、核对同键回执与真实媒体，并记录账号隔离、版本冲突和停止/恢复结果。作品和验收产物留在本机；GitHub 只保存脱敏结论。部署会启动调度与资源管理，不在审查代码时顺手重启服务。

### 文件路径与并发替换

工作台对上传、媒体/工作流/素材读取和私人目录删除都执行两层检查：先用 `no_links` 拒绝符号链接、junction 和 Windows reparse point，再在实际打开或交给删除工具前后比较文件或目录的设备、inode、大小、修改时间和模式。上传使用临时同级文件并在替换前后复核父目录；删除计划由 safe-delete 保存完整快照，应用时再次比较快照。若检查发现路径在请求期间被替换，操作会失败并要求重新读取或重新预览，不能继续使用旧路径授权。

这些检查是并发文件系统变更的检测机制，不是操作系统级隔离。部署仍应限制工作区写权限，避免其他进程同时修改作品目录；客户端遇到 `409` 时应重新读取素材或重新创建删除计划。

## 协作与提交

接手维护先读[项目 AGENTS](../AGENTS.md)与 [GitHub 协作约定](github-collaboration.md)。GitHub Issue 记录实现、实际验证、剩余问题及明确验收条件；PR 交付源代码，合并、部署与实际验收分别处理。

根 `.gitignore` 是逐文件源代码白名单。检查新增文件内容、暂存 diff、依赖来源与公开文档链接，才能加入白名单。禁止提交作品、配置、账号、数据库、运行/审核记录、缓存或旧历史。不要强制添加被排除的数据。公开问题的准确状态以 [GitHub Issues](https://github.com/czz20000522/director-workbench/issues) 为准。

当前远程 MCP 还保留 SDK 的 loopback Host/Origin 防护。建议客户端经已配置的 SSH 隧道使用 localhost 地址；HTTP 页面能直连 tailnet 不代表 `/mcp/` 接受该 Host。直连 tailnet 域名或反向代理需另行配置并验收允许的 Host/Origin；不能通过关闭这项防护解决 421/403，也不能把 HTTP 的 Origins 参数当作 MCP Host 配置。
