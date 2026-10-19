# Issue #122 实施报告：重写导演工作台 Skill 为许愿式创作 Agent 操作规程

- Issue：[#122](https://github.com/czz20000522/director-workbench/issues/122)，关联 [#119](https://github.com/czz20000522/director-workbench/issues/119)
- 分支：`codex/issue-122-skill`（基于 `codex/boundary-integration@2985740`）
- 日期：2026-10-19
- 范围：仅 Skill 与必要公开文档；未改代码、测试、其他 Issue 文件；未启动工作台/ComfyUI，未跑真实 GPU，未 push/合并。

## 1. 结论

`.agents/skills/director-workbench/SKILL.md` 已从旧的“auto + 页面流程”说明重写为按真实执行阶段推进的许愿式创作规程，覆盖认证、愿望/边界确认、参考与素材显式观察、显式制作方案、预检、生成、轮询/未知回执/`needs_reconcile`、候选媒体读取、逐段显式采用、装配/接点/音轨、受控媒体交付、失败/重试/幂等，并附带 54 个远程 MCP 工具的合同快照与最小端到端验收清单。

真实外部 Agent 客户端（Codex/Claude 等）只给 URL/token/愿望的端到端验收：**NOT_RUN**。本次未执行真实 GPU 生成、跨设备或外部客户端验收，报告中不把离线文档检查当作真实验收。

## 2. 关键合同核对与修正

- 远程 MCP 工具实际为 **54** 个（`backend/remote_mcp.py: REMOTE_TOOLS`）；本机 stdio 全量 **61** 个（`mcp_server/server.py` 注册）。原 `docs/public-api-guide.md` 写“50 个”，已改为 54，并注明 stdio 61。
- 仅本机 stdio 的 7 个工具：`update_guide_settings`、`import_reference`、`restore_shot_version`、`repair_shot_recipe_identity`、`control_batch`、`resolve_missing_execution`、`resume_generation`。Skill 附录 B 给出各自认证 HTTP 替代路径。
- 修正 `docs/public-api-guide.md` 中旧工具名 `adopt_candidate` → 实际 `approve_candidate`（或 HTTP `POST /adoptions`）。
- 明确当前 MCP 只注册 tools，未注册 resources/prompts：Skill 无法通过 MCP 资源接口拉取，必须由客户端按自身 Skill 机制安装；未虚构该能力。
- 明确受控媒体读取/交付通过同源 `GET /media-file?path=<URL 编码后的登记引用>`（带本人 Bearer），`/media/{asset_name}` 是旧别名；远程 MCP 无下载工具。
- 明确 `upload_material` base64 解码后单文件 16 MiB；HTTP multipart 默认单文件 2 GiB、单次 200 个文件、单请求 4 GiB；超限 413 不登记。
- 明确 `needs_reconcile` 的生成任务用 `reconcile_generation`、参考分析用 `reconcile_reference_analysis`；`resolve_missing_execution`/`resume_generation` 远程不可用，只能认证 HTTP 且须严格核实。
- 明确无自动方案/默认方案/自动拆分/自动 ASR/自动采用；保存分镜时若两者都不传只复用作品此前**显式安装**的方案，不是平台默认。

## 3. 改动文件

| 文件 | 改动 |
| --- | --- |
| `.agents/skills/director-workbench/SKILL.md` | 全面重写：版本/安装、认证与 CA/Host/Origin、愿望确认表、参考显式观察、计划与方案、预检、生成/批次、轮询/未知回执/`needs_reconcile`、候选/媒体读取、逐段采用、装配/接点/音轨、交付、失败/幂等总表、禁止事项、最小验收清单、54 个远程工具清单与 local-only 7 个 HTTP 替代 |
| `docs/public-api-guide.md` | 工具数 50→54（并注明 stdio 61）；`adopt_candidate`→`approve_candidate`；补充 Skill 获取/安装与 MCP resources/prompts 当前限制；补充 `/media-file` 媒体读取；补充 HTTPS/私人 CA 信任说明；补充远程 `import_reference` 仅 HTTP 登记 |
| `mcp_server/README.md` | 远程工具范围改为 54/61 并列出 local-only 7 个；远程客户端优先 agent-token；补充受控媒体读取与“任务成功≠查看/采用/交付”；Skill 安装与当前限制说明 |
| `ISSUE122_REPORT.md` | 本报告（可提交，Lead 最终移除） |

## 4. 验证命令与结果

在 worktree `D:\software\JetBrains\python_workspace\director-workbench-issue122` 执行：

```powershell
.\.venv\Scripts\python.exe tools\check_public_docs.py
# 结果：Public source syntax and 52 relative links passed (316 files).

.\.venv\Scripts\python.exe tools\export_public_source.py --check
# 结果：Checked 316 files; 3104211 bytes

# 工具数量/名称与代码一致性（脚本读取实际注册）
python -c "from mcp_server.server import create_server; ..."
# 结果：remote=54 full=61 local-only=[control_batch, import_reference,
# repair_shot_recipe_identity, resolve_missing_execution, restore_shot_version,
# resume_generation, update_guide_settings]

# 旧引用/禁用词断言
Select-String -Path '.agents/skills/director-workbench/SKILL.md' -Pattern 'auto|adopt_candidate|页面驱动'
# 结果：无匹配
Select-String -Path 'docs\*.md','mcp_server\README.md' -Pattern 'adopt_candidate|50 个|50个'
# 结果：无匹配

# 行尾空白检查（仓库文档为 CRLF）
git -c core.whitespace=cr-at-eol diff --check
# 结果：通过
```

未执行：`run_tests.ps1` 全量 core、真实 GPU 生成、真实外部 Agent 客户端、跨设备验收、工作台/ComfyUI 启停。这些均标记 **NOT_RUN**。

## 5. 提交

```text
15c55e3 docs: rewrite director-workbench skill for wish-driven agent workflow
233542c docs: sync MCP tool counts, media access and skill installation contract
（本报告单独提交，Lead 最终移除）
```

## 6. Issue 验收项对照

| 要求 | 状态 | 证据 |
| --- | --- | --- |
| 按认证→愿望→参考/素材→计划→显式方案→预检→生成→轮询→候选→采用→装配→交付重写 | 完成 | SKILL.md 第 1–11 节 |
| 明确必须问用户/观察候选/必须显式确认 | 完成 | SKILL.md 第 2 节表格、第 8–9 节 |
| 不宣称平台理解/判断/自动改编 | 完成 | SKILL.md 第 2、13 节 |
| token/CA、MCP 发现/schema、错误/重试/幂等/未知回执、长任务/分页 | 完成 | SKILL.md 第 1、12 节与附录 |
| 工具数量/名称与代码一致 | 完成 | 54/61 核对脚本；附录 A/B |
| 版本化、外部 Agent 安装方式、MCP resources/prompts 当前限制 | 完成 | SKILL.md 版本与安装节；mcp_server/README.md |
| 最小端到端验收（只给 URL/token/愿望、无 UI） | 清单完成，真实外部 Agent 验收 **NOT_RUN** | SKILL.md 第 14 节 |
| 失败路径（未选方案/未采用/参考缺失/会话过期/长任务失败） | 完成文档化 | SKILL.md 第 7、12、14.15 节 |

## 7. 剩余限制与风险

- **真实外部 Agent 验收未执行**：没有真实外部客户端按 Skill 完成一次生成/交付；需要在有 GPU 和真实账号的部署上另行执行并保留脱敏证据。
- **工作台部署/CA/Host/Origin 未验证**：本机未启动服务，未验证具体部署的 HTTPS 链、反向代理 Host/Origin 和 token 生命周期；Skill 只写通用合同，不含真实地址/凭据。
- **工具数量是快照**：54/61 来自当前代码；服务端 `tools/list` 与 `/openapi.json` 才是部署权威，后续工具变化需同步版本号与附录。
- **ISSUE122_REPORT.md 提交**：按任务“可提交、Lead 最终移除”处理；若仓库要求公开源代码白名单，Lead 移除时应同步清理该提交/文件。
