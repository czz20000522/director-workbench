# Wave3-C 前端执行报告

## Phase 1：可访问性

- 为素材桥、目录删除、分镜删除和大图预览加入 Escape 关闭、初始焦点、Tab 焦点陷阱和关闭后焦点恢复。
- 增加跳转主内容链接、主内容 landmark、键盘可见焦点样式和触控设备目标尺寸修正。
- 为 toast、任务/操作状态和素材桥反馈补充 `role="status"`/`aria-live`；错误反馈保留 `role="alert"`。
- 提交：`b4786c6 feat: improve frontend dialog accessibility`

## Phase 2：Tailwind 与 CSS 清理

- 确认源码没有 Tailwind 指令、工具类或配置使用，移除 `@tailwindcss/vite`、`tailwindcss` 依赖及 Vite 插件。
- 合并重复的 `:root` 和初始焦点规则，保留最终视觉主题和 reduced-motion 规则。
- 提交：`bf71c63 chore: remove unused tailwind setup`

## Phase 3：App 结构拆分

- 从 `App.tsx` 抽出顶部新建/导入面板 `ProjectActionsPopover`。
- 抽出连接与资源状态面板 `ResourceStatusPanel`；状态与所有业务回调仍由 `App.tsx` 持有。
- 为新增公开源文件补充 `.gitignore` 白名单。
- 提交：`b33affa refactor: split workbench shell views`

## 验证

- `.\.venv\Scripts\ruff.exe check`：通过。
- `npm run build`：通过（TypeScript 与 Vite 构建均通过）。
- `.\run_tests.ps1 -Suite browser`：通过，15 个浏览器驱动全绿。
- `.\run_tests.ps1 -Suite core`：`1058 passed, 4 skipped`；首次运行因新增文件未列入公开白名单失败，补充白名单后重跑通过。
- `.\.venv\Scripts\python.exe tools\export_public_source.py --check`：302 文件、45 个相对链接通过。
- `.\.venv\Scripts\python.exe tools\check_public_docs.py`：通过。

## 实际改动文件

`.gitignore`、`package.json`、`package-lock.json`、`vite.config.ts`、`src/App.tsx`、`src/AudioEditPanel.tsx`、`src/DeleteDirectoryDialog.tsx`、`src/EnhancementPanel.tsx`、`src/FirstUseGuide.tsx`、`src/KeyframePanel.tsx`、`src/LoginGate.tsx`、`src/MaterialBridge.tsx`、`src/MediaToolsPanel.tsx`、`src/ProjectMaterialLibrary.tsx`、`src/QuickStudioPrototype.tsx`、`src/ReverseWorkbench.tsx`、`src/ScriptWorkbench.tsx`、`src/SpeechPanel.tsx`、`src/TaskQueueStatus.tsx`、`src/main.tsx`、`src/styles.css`、`src/app/useDialogFocusTrap.ts`、`src/components/ProjectActionsPopover.tsx`、`src/components/ResourceStatusPanel.tsx`、`src/components/WorkbenchOverlays.tsx`。

未执行 push、合并、部署、工作台/ComfyUI 启停或真实 GPU 生成。
