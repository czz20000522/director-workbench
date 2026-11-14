import { lazy, type ComponentType } from 'react';
import { reportClientError } from './clientErrorReporter';

/**
 * 懒加载 + 旧页面自愈。
 *
 * 工作台每次重建前端都会换成新的 hash 文件名，`dist/assets` 里的旧 chunk 随之消失。
 * 如果浏览器还开着重启前的页面，点「文生图」这类懒加载入口就会去请求一个 404 的
 * chunk，React 把它当渲染异常抛出去 —— 用户看到的就是整页「工作台暂时遇到问题」。
 *
 * 这里在 chunk 加载失败时自动整页重载一次（新页面引用的是新文件）；30 秒内
 * 已经重载过还失败，说明不是缓存问题，把原错误抛给错误边界，不掩盖真实缺陷。
 */

const RELOAD_KEY = 'director-workbench:chunk-reload-at';
const RELOAD_WINDOW_MS = 30_000;

function readReloadAt(): number {
  try {
    return Number(sessionStorage.getItem(RELOAD_KEY) || 0) || 0;
  } catch {
    return 0;
  }
}

function writeReloadAt(value: number): void {
  try {
    sessionStorage.setItem(RELOAD_KEY, String(value));
  } catch {
    /* 隐私模式下写不了，仍然重载；靠时间窗口兜底会失效，但不会更糟 */
  }
}

/** 所有懒加载入口都走它：失败先报告，再决定重载还是抛错。 */
export function lazyWithReload<T extends ComponentType<any>>(
  load: () => Promise<{ default: T }>,
  label: string,
) {
  return lazy(async () => {
    try {
      const module = await load();
      // 加载成功说明页面已经是最新的，清掉重载标记，下次重建还能再自愈。
      try { sessionStorage.removeItem(RELOAD_KEY); } catch { /* ignore */ }
      return module;
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      reportClientError({
        message: `chunk 加载失败（${label}）：${message}`,
        stack: error instanceof Error ? error.stack : '',
        kind: 'chunk',
      });
      const now = Date.now();
      if (now - readReloadAt() > RELOAD_WINDOW_MS) {
        writeReloadAt(now);
        window.location.reload();
        // 重载生效前保持 Suspense 的加载态，不要闪一下错误页。
        return await new Promise<never>(() => undefined);
      }
      throw error;
    }
  });
}
