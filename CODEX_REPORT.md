# CODEX_REPORT

任务：Wave3-Fix1 装配连续性性能、状态与锁修复  
分支：`codex/fix-assembly-continuity`  
基线：`8d7e8ca`

## 修复内容

- Phase 1（`aeae36d`、`1de4f8b`）：为连续性媒体测量加入按路径/dev/ino/size/mtime_ns 的 TTL + LRU 有界缓存；加入接点上限、总分析 deadline、单次 FFmpeg/ffprobe 超时；媒体变化自动使用新缓存身份；探测、抽帧或音频提取失败按 `unavailable`/`partial` 返回原因，不抛 500。
- Phase 2（`3a39bd2`、`a4b1375`）：`GET /assembly/edit` 保留 advisory 计算；接点、音轨保存和预检使用轻量视图，计划事务锁内不调用连续性分析；音轨保存复用已完成的时长校验结果；单个接点也受总 deadline 约束。
- Phase 3（`6219d85`）：`AssemblyEditPanel` 区分“已测量 / 部分结果 / 不可用”，缺少音轨、解码失败、超预算时不显示误导性成功；浏览器验收补充成功状态断言。
- 实际改动文件：`backend/assembly_continuity.py`、`backend/app.py`、`backend/routers/batch_assembly.py`、`src/AssemblyEditPanel.tsx`、`tests/test_assembly_continuity.py`、`tests/test_assembly_edit.py`、`tests/assembly_edit.browser.mjs`、`CODEX_REPORT.md`。

## 验证

- 目标 pytest：`11 passed, 3 skipped`；跳过项均因本机 FFmpeg/ffprobe 不可用，未伪造媒体结果。
- Core gate：`run_tests.ps1 -Suite core -Check` → `core=passed`。
- Ruff：`python -m ruff check` → passed。
- Public source：`tools/export_public_source.py --check` → 308 files checked；`tools/check_public_docs.py` → 45 links passed。
- 前端：`npm run build` → TypeScript 检查与 Vite build passed。
- Browser：`node tests/assembly_edit.browser.mjs` → join review、音轨选择、预检和“已测量”状态通过。
- 未执行：push、合并、服务启停、真实 GPU 或作品生成。

## 外部记录

Notion 连接当前不可用，已按工作约定保存 `PENDING_SYNC` 待同步记录；仓库与上述测试输出是本轮实现和验证证据。

