# Wave3-Fix2：媒体 Range/HEAD 与素材登记 TOCTOU

## 结果

全部要求已完成，未执行 push、合并、服务启停、真实 GPU 或用户数据删除。

## Phase 提交

1. `8a3b99f fix: stream bounded media range responses`
   - `VerifiedFileResponse` 的单 Range 和 multipart Range 按 chunk 流式发送。
   - `HEAD` 路径只发送计算出的响应头，不打开或读取文件正文。
   - multipart Range 限制为最多 8 段、合并后总字节数最多 64 MiB，超限返回 416。
   - 增加 Range 流式、HEAD 无读取和超限测试。
2. `9072e6b fix: guard material registration copy races`
   - `secure_files.copy_checked` 使用已校验源句柄复制，并在复制后复核源身份和目标大小。
   - 素材登记先捕获 `FileIdentity`，复制和登记前后复核；检测到替换或复制竞态返回 409。
   - 增加素材登记替换竞态测试，正常复制与去重回归保持通过。

## 实际改动文件

- `backend/secure_files.py`
- `backend/routers/material_library.py`
- `tests/test_secure_files.py`
- `tests/test_material_bridge.py`
- `CODEX_REPORT.md`

## 验证

- 目标 pytest：`tests/test_secure_files.py tests/test_material_bridge.py tests/test_media_operations.py tests/test_media_operations_api.py tests/test_private_workbench.py`，`72 passed`。
- `ruff check`：通过。
- `npm run build`：通过，TypeScript 检查和 Vite production build 均完成。
- `tools/export_public_source.py --check`：通过，检查 307 个文件。
- `tools/check_public_docs.py`：通过，检查 307 个文件和 45 个相对链接。
- `run_tests.ps1 -Suite core -Check`：通过。
- `run_tests.ps1 -Suite browser -Check`：通过。

两次套件运行均报告旧 runtime run root 清理时的 `PermissionError` 警告；本次 suite 状态仍为 passed，警告不涉及工作区代码或本次修复。
