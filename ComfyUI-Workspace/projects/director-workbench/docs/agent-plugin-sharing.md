# Agent 插件与认证边界

本机的插件代码、插件注册和认证凭据是三个不同层次，不能用“把插件目录复制给另一个 Agent”来解决全部问题。

## 当前实测状态

- Codex 的插件注册位于用户级 `C:/Users/<用户>/.codex/config.toml`。OpenAI 官方文档说明，ChatGPT 桌面端、Codex CLI 和 IDE 扩展共享同一 Codex 主机上的 MCP 配置；这不等于与 Claude 共享。当前启用的 bundled/primary-runtime 与 curated 插件由 Codex 自己加载，插件文件位于该用户的插件缓存或本地 marketplace；这属于 Codex 配置，不属于本项目仓库。
- Codex 当前已有若干 MCP endpoint 和插件 manifest。manifest/marketplace 只是能力定义，active registration 才是客户端实际连接；存在 manifest 不能证明服务已认证或 token 有效。
- Claude 的插件注册位于其自己的用户配置（当前机器为 `C:/Users/<用户>/.claude/settings.json`）；审计时全局和本项目的 `mcpServers` 均为空，没有发现与 Codex 共用的 MCP server 注册，因此 Claude 不会自动继承 Codex 的插件实例。
- Codex 的 OAuth 凭据存放在其受保护的 secrets store（本机可见的是加密文件和锁文件）；不能复制到项目、环境变量或 Claude 配置。审计只确认存储结构存在，不判断其中凭据是否仍有效。

## 可实现的共享目标

机器级共享应落在“服务实例”层：把需要同时服务多个 Agent 的 MCP 服务安装到机器级目录（例如 `D:/software/agent-services/<service>`），服务只保留一份；Codex 和 Claude 各自在自己的配置中注册同一个本机 endpoint 或同一个启动脚本。项目仓库只保存服务定义、无凭据的配置模板、启动和健康检查脚本。Codex 的 HTTP MCP 配置支持 OAuth、bearer 环境变量和本地 header helper；应优先使用 OAuth/受保护 helper，避免静态 token 进入配置。

这样可以共享插件代码和运行实例，但不假定认证状态可共享。若服务使用 OAuth，是否能跨客户端复用登录态取决于服务自己的认证后端。可行的安全方案是由本机 MCP gateway 使用 Windows Credential Manager 或其他受保护存储完成一次认证，两个 Agent 只调用 localhost gateway；不能把 Codex 私有 refresh token 解密后写入 Claude。

## 迁移步骤

1. 先列出需要共享的具体插件及其协议入口，确认它们能以 stdio 或 localhost HTTP 独立启动。
2. 将服务代码和非敏感配置放到机器级目录，并为每个服务提供 `start`、`health` 和版本检查入口。
3. 分别向 Codex 和 Claude 注册同一服务 endpoint；配置文件仍然各自维护，避免一个客户端升级覆盖另一个客户端的配置。
4. 只在 gateway 的认证流程中登录。认证成功后用健康检查验证两个客户端都能调用同一服务；不要检查或打印 token 内容。

目前本机没有发现可直接迁移的 Claude MCP 注册；Codex 已有 endpoint/manifest，但尚未验证它们能否被 Claude 直接使用。因此本轮不修改 `.codex` 或 `.claude` 的账号配置，避免把用户凭据或应用私有状态写入项目；下一步应以具体插件名称为输入，再按上述流程接入。若任何认证 token 曾被写入日志、聊天或环境变量，应在对应供应商侧撤销并轮换，不能把旧 token 复制到另一客户端。

参考： [Codex Plugins](https://developers.openai.com/codex/plugins/) · [Codex MCP servers](https://developers.openai.com/codex/mcp/)。

## 与工作台的关系

工作台的 MCP 服务属于项目运行服务，使用工作台自己的认证和账号隔离。它可以被 Codex、Claude 共同调用，但两者仍需分别注册 endpoint，并且必须通过工作台登录获得各自会话；“插件安装与账号无关”不等于绕过工作台的用户鉴权。
