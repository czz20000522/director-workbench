# Wave2 测试质量门禁与覆盖率报告

## Phase 提交

- Q1 `b0986f7`：加入 `pytest-cov==6.1.1`、Coverage 配置和 CI 覆盖率门禁；后续 `3cade08` 将门禁调整为 48%。
- Q2 `d8c0ad6`：加入 OpenAPI 路由/方法/operationId/schema 快照和核心响应字段契约测试。
- Q3 `939232a`：为 Python/npm 依赖启用 GitHub Actions 缓存。
- Q4 `d6fb10c`：集中 `preset_client_factory`，移除两个 preset wrapper 的跨测试模块 fixture import。

## 实际改动文件

- `.github/workflows/public-source.yml`
- `requirements-dev.txt`
- `pyproject.toml`
- `tests/conftest.py`
- `tests/test_creative_inspection.py`
- `tests/test_director_workbench_contracts.py`
- `tests/test_media_qc.py`
- `tests/test_remote_mcp.py`
- `CODEX_REPORT.md`

## 行为与测试变化

- CI 的 `core-tests` 同时安装运行时和开发测试依赖，执行 `backend` 与 `mcp_server` 覆盖率，门禁为 48%。
- OpenAPI 快照规范化 108 条路径和 50 个组件 schema，固定 SHA-256 为 `cacdfd16a2cffacfa4b007a00eaa50bfc0836e5961b187c88ac3e140f257badf`。
- `/api/project-state`、`/api/status`、`/api/tasks` 增加字段集合契约校验。
- 两个 host-tool wrapper 通过共享 factory 创建隔离 client；`test_remote_mcp` 的进程外 fixture `__wrapped__` 调用保留并加注释。

## 验证结果

- 完整 core：`1054 passed, 4 skipped, 42 subtests passed`。
- 契约及 wrapper 相关回归：`38 passed`。
- Ruff：通过。
- `tools/export_public_source.py --check`：299 文件通过。
- `tools/check_public_docs.py`：45 个相对链接通过。
- GitHub Actions YAML：本机无 `actionlint`，已用 PyYAML 解析通过并人工核对 job、缓存和命令结构。
- 覆盖率插件基线：本机 `.venv` 未安装 `pytest-cov`/`coverage`，按约束未安装依赖；指定命令因此无法启动。使用标准库 trace 对同一筛选集（排除缺少 `soundfile` 的 AUK 测试）估算 AST statement coverage 为 52.12%，据此设置 48% 保守门禁。CI 安装 dev requirements 后会执行真实 pytest-cov。

## 未完成项与风险

- 本机没有真实 pytest-cov 行覆盖率百分比；最终数值由 CI 安装 `requirements-dev.txt` 后确认。
- 未运行 `npm run build` 和 browser suite，因为本次没有前端或静态资源改动。
- 未 push、未合并、未启动工作台或 ComfyUI。

## 新文件

- `CODEX_REPORT.md`
