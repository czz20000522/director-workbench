# 公开开发指南

本指南面向维护代码的 Agent 和开发者。使用现有工作台的客户端只需要维护者提供的私人 URL 和本人账号，见 [HTTP API 指南](public-api-guide.md)与 [MCP 连接说明](../mcp_server/README.md)。

仓库是筛选过的开发源代码，保留 Comfy-Desktop 相对布局。它不包含 ComfyUI 安装、模型、工具环境、账号、作品或生产配置。目前服务端依赖 Windows 路径和本机媒体工具；从空白设备完整部署仍须独立验收。客户端设备无需安装服务端 GPU 环境。

## 架构与修改位置

| 位置 | 职责 |
| --- | --- |
| [src](../src) | React 页面、当前会话请求、计划与候选展示 |
| [backend/private_app.py](../backend/private_app.py) | 认证服务入口，校验本机账号、管理员和存储配置 |
| [backend/app.py](../backend/app.py) | HTTP 业务、作品登记、计划版本、任务/回执和队列接线 |
| [backend/private_auth.py](../backend/private_auth.py)、[private_workspaces.py](../backend/private_workspaces.py) | Cookie/Bearer 会话、账号与作品归属 |
| [backend/task_scheduler.py](../backend/task_scheduler.py) | 同一服务进程的持久任务调度 |
| [backend/agent_access.py](../backend/agent_access.py)、[agent_pages.py](../backend/agent_pages.py) | 公开发现、远程 MCP 子集、受控页面动作与回执 |
| [mcp_server/server.py](../mcp_server/server.py) | HTTP 业务的 MCP 薄适配，不另建执行队列 |
| [tools](../tools)、[共享制作脚本](../../../../.agents/skills/aigc-video-production/scripts) | 后端受控执行器；外部客户端通过工作台入口使用 |
| [storage_layout.py](../../../production/storage_layout.py) | 读取本机存储根；路径配置不由客户端传入 |
| [tests](../tests)、[contracts](../contracts) | 隔离回归和制作输入合同 |

UI、HTTP 和 MCP 共用后端授权与任务记录。任务先冻结输入和归属，再进入执行器；产物登记为候选，审核和采用需显式操作。技术报告、任务成功或页面播放回执均不能替代人的内容验收。

`tools` 中也保留早期开发辅助脚本；部分默认工作流不随仓库分发，须先检查参数并提供实际输入，不能将无参数脚本当作新设备制作入口。正式创作由 UI/API/MCP 负责登记与执行。

## 环境和存储

本机运行与开发布局以 `D:\Comfy-Desktop` 为根。ComfyUI 本体、Python 环境、自定义节点、模型、工具、热缓存和研究/测试临时目录均在该根下。`ComfyUI-Shared` 保持 D 盘真实目录，不建立跨盘 junction 或符号链接。账号、数据库、运行状态、构建与测试临时文件也属于本机数据，不纳入 Git。

输入、输出、生成素材和长期作品资产统一放在本机配置的 E 盘目录。引擎 input/output 是受控交换区；私人作品按 `用户/series/系列/作品` 登记。`user001`、`user002` 等账号隔离，通过服务端会话与登记验证归属，不能靠客户端传入用户名或磁盘路径授权。

服务端维护者另行准备：

- Python；目前维护环境使用 3.13，工作台使用项目 `.venv`，ComfyUI 和 AuK 使用各自专用解释器。
- Node.js/npm；前端按 [package-lock.json](../package-lock.json)安装。
- ComfyUI 桌面安装及相应模型、节点、媒体工具；检查当前能力接口返回的缺项，不根据代码存在推定模型可用。

`ComfyUI-Workspace/config/storage.json` 和根 `.config/director-workbench/local-storage-policy.md` 是不公开提交的本机配置。以下是符合维护环境分层的脱敏示例；根须为绝对真实目录、不重叠，共享兼容作品根在管理员 `series` 下的结构例外由存储模块校验：

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

空白开发机器可先将[仓库](https://github.com/czz20000522/director-workbench)检出到 `D:\Comfy-Desktop`，保留其相对目录结构。已有安装机器使用源代码分支更新，避免以新的 clone 覆盖安装、资产或未提交修改。安装与测试不读取私人制作文档。

以下命令在 PowerShell 的项目目录执行：

```powershell
Set-Location D:\Comfy-Desktop\ComfyUI-Workspace\projects\director-workbench
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
npm ci
npm run build
.\run_tests.ps1 -Suite core -Check
.\run_tests.ps1 -Suite core
```

`python` 应由维护者选择已安装的基础 Python；既有 `.venv` 不需要重建。前端构建执行 TypeScript 检查并输出 `dist`，不代表后端或 GPU 生成通过。Python 依赖已包含 MCP；[requirements-mcp.txt](../requirements-mcp.txt)提供同一依赖入口。

完整离线回归用 `run_tests.ps1 -Check`、`run_tests.ps1`。统一入口调用 [run_project_tests.py](../tools/run_project_tests.py)：核心组在工作台 `.venv` 执行 pytest；AuK 组在 `ComfyUI-Shared/tools/AuK/.venv` 执行声音执行器 unittest。缺少 AuK 环境时完整检查失败，不能把核心组通过报告为完整通过，也不向 API 或 ComfyUI 基础环境安装模型依赖。报告和临时文件保存在隔离的 `ComfyUI-Workspace/runtime` 下，不提交。

优先运行与修改相关的回归，再按交付范围执行完整入口。离线回归、在线业务、真实 GPU 输出与跨设备访问分别记录。

依赖已安装节点源码的主机探针还需相应 ComfyUI 安装；缺少主机资源导致的跳过须在结果中说明。通过其余测试不能替代该探针或真实 GPU 验证。

## 账号与服务启动

账号由服务器维护者在本机交互创建，用户名格式为 `user` 加三位数字。下面的 `user001` 只是示例：

```powershell
.\.venv\Scripts\python.exe tools\private_accounts.py create user001
.\start_private_workbench.ps1 -AdminUsers user001
```

账号工具不回显密码；账号摘要留在本机 `ComfyUI-Workspace/runtime/director-private`。启动脚本要求显式指定已存在的管理员，使用 `backend.private_app:app`、单 worker，默认监听 `127.0.0.1:4100` 并服务已构建的页面。不要以不带认证的 `backend.app:app` 对外提供服务。

首次启动允许没有 `projects/catalog.json` 或默认作品：服务以空作品状态启动，不在导入时创建示例作品、计划或私人 manifest；本人作品列表为空，通过 UI/API 创建实际作品后才登记数据。仍须先安装依赖、构建页面、创建账号、指定管理员并配置资产根，不能从已有生产环境复制私人目录作为部署模板。

公开源代码导出的隔离回归已验证：合成账号环境无 catalog/plan 时，私人应用可加载，匿名发现、登录和空作品列表正常。此证据不等同于空白机器的 ComfyUI/模型安装、完整浏览器使用或 GPU 制作验收。

常用本机启动配置如下。它们均留在部署环境，不能提交凭据或实际网络地址：

| 参数/环境变量 | 用途 |
| --- | --- |
| `-AccountsFile` / `DIRECTOR_ACCOUNTS_FILE` | 哈希账号文件，须在允许的服务器根内 |
| `-WorkspaceRoot` / `DIRECTOR_PRIVATE_ROOT` | 私人作品根，须通过服务端允许列表与无链接校验 |
| `-AdminUsers` / `DIRECTOR_ADMIN_USERS` | 已创建管理员，逗号分隔 |
| `-HostAddress`、`-Port`、`-Origins` | 监听地址/端口和允许的完整浏览器 Origin |
| `DIRECTOR_COMFY_URL` | 内部 ComfyUI 地址，默认 `http://127.0.0.1:8188` |
| `DIRECTOR_COOKIE_SECURE=1` | HTTPS 部署使用 Secure Cookie |

启动时使用维护者配置的私人作品根；需要覆盖时显式传 `-WorkspaceRoot`，仍须满足服务端允许列表。`start_workbench.ps1 -Dev` 可启动 Vite 与私人后端，用于本机调试；它固定示例管理员 `user001`，需要先创建账号。自定义部署优先使用显式参数的私人启动入口。

## 多设备部署和验收

现有私人网络可通过客户机 SSH 隧道访问服务器 `127.0.0.1:4100`，或由维护者配置指定监听地址及精确 Origin。浏览器 Origin 包含协议、主机和端口，不带末尾斜杠；非 loopback 启动必须提供 `-Origins`。客户端接入工作台服务，不直接公开 ComfyUI 端口。尾网、SSH 用户/密钥与地址由维护者通过私人渠道提供。

上线前先检查登录、`/api/auth/me`、`/api/health`、本人作品列表与未授权访问拒绝。`health.ok=true` 只说明工作台处理了请求，还需读取 `comfy.online`；声音、图片与媒体工具各自查看 `/api/speech-capability`、`/api/keyframe-capability`、`/api/media-capability`。模型/节点缺失应由能力检查及预检返回，不能盲目提交生成。

需要 GPU 或跨设备验收时，按 Issue 的明确条件使用公开业务入口创建测试作品、提交一次有授权的小样、核对同键回执与真实媒体，并记录账号隔离、版本冲突和停止/恢复结果。作品和验收产物留在本机；GitHub 只保存脱敏结论。部署会启动调度与资源管理，不在审查代码时顺手重启服务。

## 协作与提交

接手维护先读[项目 AGENTS](../AGENTS.md)与 [GitHub 协作约定](github-collaboration.md)。GitHub Issue 记录实现、实际验证、剩余问题及明确验收条件；PR 交付源代码，合并、部署与实际验收分别处理。

根 `.gitignore` 是逐文件源代码白名单。检查新增文件内容、暂存 diff、依赖来源与公开文档链接，才能加入白名单。禁止提交作品、配置、账号、数据库、运行/审核记录、缓存或旧历史。不要强制添加被排除的数据。公开问题的准确状态以 [GitHub Issues](https://github.com/czz20000522/director-workbench/issues) 为准。

当前远程 MCP 还保留 SDK 的 loopback Host/Origin 防护。建议客户端经已配置的 SSH 隧道使用 localhost 地址；HTTP 页面能直连 tailnet 不代表 `/mcp/` 接受该 Host。直连 tailnet 域名或反向代理需另行配置并验收允许的 Host/Origin；不能通过关闭这项防护解决 421/403，也不能把 HTTP 的 Origins 参数当作 MCP Host 配置。
