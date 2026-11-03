# 导演工作台

基于 ComfyUI 的多用户视频创作工作台，提供浏览器界面、HTTP API 与 MCP。项目仍在开发中。

当前包含：作品流程（分镜 → 关键帧 → 视频 → 装配）、单步工具（文生图 / 文生视频 / 图生视频 / 清晰化 / 配音 / 音色设计 / 配乐 / 换音轨），以及五个可维护的库（提示词、素材参考、清晰化、剧本、预设）。库的编辑与删除范围见部署文档 §8.5。

## 使用与开发

工作台代码就是本仓库根目录。从[公开开发指南](docs/public-development-guide.md)了解架构、Windows 环境、构建、账号、启动和测试；客户端接入见 [HTTP API 指南](docs/public-api-guide.md)与 [MCP 连接说明](mcp_server/README.md)；把整套环境复刻到另一台电脑（目录、模型、引擎、存储配置、验收）见 [本地复刻与目录建设方案](docs/local-deployment-plan.md)。开发指南也说明 `backend/app.py` 与 15 个 `backend/routers/` 领域模块、`tests/conftest.py`/`tests/support.py` 的测试组织，以及 fast core、browser 和四项 CI 门禁。前端的共享请求层位于 `src/api/client.ts`，重型工作区面板按需加载。这些入口只链接公开源文件，私人制作记录不作为开发前置。

普通设备使用维护者提供的私人工作台 URL 和本人账号；服务端保留 Comfy-Desktop 目录布局。ComfyUI、模型、媒体工具及其专用环境需另行安装，不随仓库分发。本机把引擎、模型与工作台数据放在同一个根（`D:\AI本地拓展坞\MiniMax H3`）下，各资产根由 `director-data\config\storage.json` 决定，也可以指向别的盘。空白设备的目录清单、模型放置与逐项验收见[本地复刻与目录建设方案](docs/local-deployment-plan.md)，那份文档就是重建依据，不能以构建或离线测试通过代替实际验收。

## 多设备协作

用户和客户端 Agent 在 [Issues](https://github.com/czz20000522/director-workbench/issues) 提交脱敏反馈。本机维护 Agent 复现、修复、验证并创建关联 PR。参见 [协作约定](docs/github-collaboration.md)。代码合并、服务部署与跨设备验收分别记录。

仓库不包含私人账号、密钥、配置、模型、作品、运行/审核记录或旧 Git 历史。作品为独立资产，不属于代码版本。公开可见不自动授予许可证；当前未选择开源许可证。
