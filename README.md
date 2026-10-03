# 导演工作台

基于 ComfyUI 的多用户视频创作工作台，提供浏览器界面、HTTP API 与 MCP。项目仍在开发中。

## 使用与开发

仓库保留 Comfy-Desktop 目录布局。Windows 上将本仓库检出到安装根目录；ComfyUI、模型和工具由用户另行安装到 ComfyUI-Shared / ComfyUI-Installs，不随仓库分发。

工作台代码位于 `ComfyUI-Workspace/projects/director-workbench`。在该目录运行 `npm ci`、`npm run build`；创建项目专用 Python .venv，并安装 requirements.txt。账号通过 tools/private_accounts.py 本机交互创建，密码不放入命令行。用 start_workbench.ps1 启动，默认地址 http://127.0.0.1:4100。

GPU、AuK 与 ComfyUI 环境需要另行配置；run_tests.ps1 -Check 检查环境，run_tests.ps1 运行完整回归。首次公开的是筛选的开发源代码，尚未验证从空白设备部署；代码存在不表示所有制作方案均已验收。默认应使用私人账号入口，不将服务直接暴露到公网。

## 多设备协作

用户和客户端 Agent 在 [Issues](https://github.com/czz20000522/director-workbench/issues) 提交脱敏反馈。本机维护 Agent 复现、修复、验证并创建关联 PR。参见 [协作约定](ComfyUI-Workspace/projects/director-workbench/docs/github-collaboration.md)。代码合并、服务部署与跨设备验收分别记录。

仓库不包含私人账号、密钥、配置、模型、作品、运行/审核记录或旧 Git 历史。作品为独立资产，不属于代码版本。公开可见不自动授予许可证；当前未选择开源许可证。
