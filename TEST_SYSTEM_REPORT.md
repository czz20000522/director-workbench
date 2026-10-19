# 测试体系改造报告（Issue #123）

## 目标与结论

把测试从"每次全量跑一遍"改为按改动边界分层：

1. **fast**：纯单元/契约，硬预算 60s，不启动服务、媒体或浏览器；
2. **affected**：确定性测试影响面选择，只跑与 `--base` 以来改动相关的检查；
3. **core / browser**：保留为合并/发布门，共享契约变更自动升级到 full core。

实现包含通用选择器 `tools/select_tests.py` + 影响面配置 `tools/test_impact.toml`、`run_tests.ps1` / `tools/run_project_tests.py` 的 `fast`/`affected` 套件、pytest 全局硬超时、`tests/test_secure_files.py` 挂起根因修复，以及选择器契约测试。全仓 core 未在本次运行（任务范围明确排除），合并门行为保持兼容。

## 1. 测试影响面选择器

`tools/select_tests.py` 读取 `tools/test_impact.toml`，用 `git diff --name-status -M <base>` + `git ls-files --others --exclude-standard` 收集**已提交、已暂存、未暂存、未跟踪**的改动（含 rename 的旧/新路径），按固定优先级给每个文件命中规则：

| 顺序 | 规则 | 行为 |
| --- | --- | --- |
| 1 | 共享契约 | 升级 full core |
| 2 | 测试文件自身 / 浏览器驱动自身 | 只选自身 |
| 3 | 文档 | 公开源测试 + 公开源/链接门禁 |
| 4 | `[[mapping]]` ownership 映射 | 选中映射的 pytest（含 `file::node`）与 browser driver |
| 5 | Python import 反向依赖 | 选中直接/传递 import 该模块的测试；在共享模块处停止传播 |
| 6 | 前端未映射 | 升级到全部 browser driver 并提示补映射 |
| 7 | 未命中映射 | 升级 full core 并明确要求补 `[[mapping]]`，不静默跳过 |

共享契约区（`tools/test_impact.toml [shared]`）覆盖：核心 schema、路由聚合/`app.py`、认证/存储/任务入口、依赖清单、CI/发布、公共入口、测试基础设施（`conftest.py` 等）。映射目标在 `load_config()` 时校验存在；测试删除/重命名会直接报配置错误，CI 另行运行 `--check-config` 作为门禁。

CLI：

```powershell
.\.venv\Scripts\python.exe tools\select_tests.py --explain
.\.venv\Scripts\python.exe tools\select_tests.py --json
.\.venv\Scripts\python.exe tools\select_tests.py --base HEAD --check-config
```

输出包含 base/commit、逐文件命中规则、pytest 目标、browser driver、非 pytest 门禁、full-core 升级和补映射警告；退出码 0 表示得到可用选择，2 表示配置、映射或 git 基线不可信。

## 2. 分层入口

`run_tests.ps1`：

```powershell
.\run_tests.ps1 -Suite fast
.\run_tests.ps1 -Suite affected              # 默认 origin/main（可配置）
.\run_tests.ps1 -Suite affected -Base HEAD
.\run_tests.ps1 -Suite affected -WithBrowser
```

- `fast` 执行 `tools/test_impact.toml [fast].tests` 的 12 个文件，`--check` 只做解释器/包探测；
- `affected` 调用选择器；命中共享契约时复用 `core` 的同一 pytest 命令；`-WithBrowser` 才运行选中的驱动；
- 缺少 `pytest-timeout` 时明确报错并给出 `pip install -r requirements-dev.txt` 安装步骤；
- 原有 `core`/`auk`/`browser`/`all` 的命令、环境变量、报告结构与退出码保持不变，解释器套件共用同一探测/执行 helper。

pytest 硬超时：`pyproject.toml` 设 `timeout = 90`、`timeout_method = "thread"`（Windows 无 signal 方法），`requirements-dev.txt` 固定 `pytest-timeout==2.4.0`。

## 3. test_secure_files.py 挂起根因

**根因**：Starlette 1.9 的 `FileResponse.__call__` 在 ASGI `spec_version < 2.4` 时把流式发送放进 collapsing task group，并循环等待 `receive()` 返回 `http.disconnect`。测试里的假 ASGI scope 没声明 `asgi.spec_version`（默认 2.0），假 `receive()` 永远返回 `http.request`，且无 await 让出事件循环，于是 3 个 range 测试在 `send_file()` 完成后死循环（整文件运行时表现为第 6 个测试开始挂起）。这不是环境/基线问题，修复方式是在假 scope 声明 `spec_version = 2.4`，走 Starlette 的直接发送路径；测试语义（响应头、分块、短读终止、416）不变。此外全局 90s pytest 超时把未来的挂起变成失败而不是无限等待。

同文件另一个测试 `test_copy_checked_keeps_existing_destination_when_source_changes` 依赖同尺寸重写的 mtime 变化来触发身份校验，在时间戳粒度内可能漏检而偶发失败；现已改为写入不同尺寸的内容，使替换必然被设备/inode/size/mtime 身份比较发现，重复运行 5 次稳定通过。

## 4. 新增测试

`tests/test_test_impact.py`（15 项）覆盖：

- 测试文件变更只选自身；
- 后端 A 功能只选 A 相关测试（`backend/audio_edit.py` 不会选中 speech 测试）；
- 共享契约升级 full core（schema/app/依赖/CI/conftest）；
- 未命中源码 fail-closed 到 full core 并要求补映射；
- 未命中前端升级全部驱动、已映射前端只选对应驱动；
- 文档变更只跑公开源门禁；
- 测试删除提示、映射目标缺失报警；
- Python import 反向依赖选择导入方测试；
- browser driver 自身选择、`file::node` 目标保留；
- `collect_changes` 覆盖已提交修改、未跟踪、rename/删除；
- CLI `--json` 选择结果与 stale 配置退出码 2。

`tests/test_project_test_runner.py` 新增 7 项，覆盖 fast 列表/预算/缺插件提示、affected 选中目标、full-core 升级、stale 映射不跑子集、文档门禁、`-WithBrowser`。

## 5. 实际命令与结果

| 命令 | 结果 |
| --- | --- |
| `.venv\Scripts\python.exe -m pytest tests/test_secure_files.py -q` | PASS，10 passed in 0.19s（修复前整文件挂起） |
| `.venv\Scripts\python.exe -m pytest tests/test_test_impact.py tests/test_project_test_runner.py tests/test_secure_files.py -q` | PASS，40 passed in 1.81s |
| `.\run_tests.ps1 -Suite fast -RuntimeRoot <临时目录>` | PASS，100 passed, 2 skipped, 19 subtests，6.81–7.54s（预算 60s） |
| `.\run_tests.ps1 -Suite affected -Base HEAD`（临时修改 `backend/audio_edit.py`） | PASS，40 passed, 2 skipped, 2.70s；只选 4 个 audio 测试，未选 speech |
| `.\run_tests.ps1 -Suite affected -Base HEAD`（临时修改 docs） | PASS，docs-only；2 个公开源门禁 + 8 个 pytest，3.58s |
| `.venv\Scripts\python.exe tools/select_tests.py --check-config` | PASS，37 mappings / 12 fast / 15 browser drivers |
| `.venv\Scripts\python.exe -m ruff check` | PASS，All checks passed |
| `.venv\Scripts\python.exe tools/export_public_source.py --check` + `tools/check_public_docs.py` | PASS，316 files / 47 links |
| `run_tests.ps1 -Suite fast -Check` | PASS（探测含 pytest_timeout） |

环境说明：本机 `.venv` 已安装 `pytest-timeout==2.4.0`；新环境执行 `.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt` 即可。

## 6. 改动文件

- 新增：`tools/select_tests.py`、`tools/test_impact.toml`、`tests/test_test_impact.py`、`TEST_SYSTEM_REPORT.md`
- 修改：`tools/run_project_tests.py`、`run_tests.ps1`、`tests/test_project_test_runner.py`、`tests/test_secure_files.py`、`pyproject.toml`、`requirements-dev.txt`、`.gitignore`、`docs/public-development-guide.md`、`.github/workflows/public-source.yml`

提交（按 phase）：

1. `test: bound every test with a hard timeout and unblock secure-file streaming cases`
2. `feat: add deterministic test impact selector`
3. `feat: add fast and affected layers to the test entry points`
4. `docs: record the layered test system and gate its impact map in CI`
5. `test: make the secure-file replacement case detect the rewrite deterministically`

## 7. 未运行 / 已知边界

- **未跑全量 `core`**：任务明确排除；当前分支存在与本次无关的 30 个失败。改动通过既有 runner 测试 + fast + 目标测试验证。
- **未跑真实 browser/GPU**：浏览器驱动与 GPU 验收不在本次范围；`affected -WithBrowser` 的驱动选择逻辑由单测和 fake runner 覆盖。
- 映射是维护点：新增源码目录/测试时需补 `tools/test_impact.toml`；未补时选择器 fail-closed 到 full core（成本高但不会漏测）。没有专用驱动的 UI 组件目前回退到全部前端驱动并告警。
- `fast` 是显式 curated 列表，新增快速测试需要手动加入 `[fast].tests`；超预算只告警，由评审决定移出而非悄悄放宽预算。
- 共享契约选择器的 full-core 升级会复用完整 core 命令，因此 `affected` 在共享契约改动上不是快速反馈手段。
