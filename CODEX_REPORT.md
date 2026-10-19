# Wave3-B3 执行报告

## 结果

远程 MCP allowlist 已从 17 个扩展到 49 个。集合覆盖作品/剧本/计划、素材与参考、队列和任务读取、停止请求、关键帧/语音/媒体、音频编辑以及装配读取和提交；所有工具继续复用 Bearer 认证后的 HTTP 业务路由。

远程边界明确排除任意服务器路径导入或导出、账户/管理和全局指南写入、删除/清理、脚本执行、历史版本恢复、批次恢复控制、任务恢复/强制收尾和配方身份修复等高风险能力。

## Phase 提交

| Phase | 提交 | 内容 |
| --- | --- | --- |
| 1 | `76a3b2b` | 扩展 `REMOTE_TOOLS` 到 49 个，并增加远程顺序快照、schema 校验和 3 个真实只读调用 |
| 2 | `d290b48` | 验证 allowlist 中每个工具来自 `mcp_server` 且 schema 可列出；锁定高风险排除集合 |
| 3 | `ae0d31c` | 更新 `docs/public-api-guide.md` 的远程 MCP 能力和安全边界说明 |
| 4 | report commit | 本报告与最终门禁记录 |

## 实际改动文件

- `backend/remote_mcp.py`
- `tests/test_remote_mcp.py`
- `docs/public-api-guide.md`
- `CODEX_REPORT.md`

## 验证

- `tests/test_remote_mcp.py`: 8 passed
- `tests/test_mcp_adapter.py`: 与远程测试合计 19 passed
- `ruff check backend/remote_mcp.py tests/test_remote_mcp.py`: passed
- `run_tests.ps1 -Suite browser`: passed
- `npm run build`: passed
- `run_tests.ps1 -Suite core`: 1060 passed, 4 skipped，42 subtests passed
- `tools/export_public_source.py --check`: passed（2766480 bytes）
- `tools/check_public_docs.py`: passed（299 files，45 relative links）

未 push、未合并、未启动或停止工作台/ComfyUI，未执行真实 GPU 生成，未删除用户数据。
