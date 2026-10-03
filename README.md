# 导演工作台

基于 ComfyUI 的多用户视频创作工作台，提供浏览器界面、HTTP API 与 MCP。项目仍在开发中。

## 使用与开发

工作台代码位于 `ComfyUI-Workspace/projects/director-workbench`。从[公开开发指南](ComfyUI-Workspace/projects/director-workbench/docs/public-development-guide.md)了解架构、Windows 环境、构建、账号、启动和测试；客户端接入见 [HTTP API 指南](ComfyUI-Workspace/projects/director-workbench/docs/public-api-guide.md)与 [MCP 连接说明](ComfyUI-Workspace/projects/director-workbench/mcp_server/README.md)。这些入口只链接公开源文件，私人制作记录不作为开发前置。

普通设备使用维护者提供的私人工作台 URL 和本人账号；服务端保留 Comfy-Desktop 目录布局。ComfyUI、模型、媒体工具及其专用环境需另行安装，不随仓库分发。运行与开发环境位于 D 盘，输入、输出及作品资产位于配置的 E 盘目录，`ComfyUI-Shared` 保持 D 盘真实目录。空白设备部署与 GPU 制作方案仍须实际验收，不能以构建或离线测试通过代替。

## 多设备协作

用户和客户端 Agent 在 [Issues](https://github.com/czz20000522/director-workbench/issues) 提交脱敏反馈。本机维护 Agent 复现、修复、验证并创建关联 PR。参见 [协作约定](ComfyUI-Workspace/projects/director-workbench/docs/github-collaboration.md)。代码合并、服务部署与跨设备验收分别记录。

仓库不包含私人账号、密钥、配置、模型、作品、运行/审核记录或旧 Git 历史。作品为独立资产，不属于代码版本。公开可见不自动授予许可证；当前未选择开源许可证。
