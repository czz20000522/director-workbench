/**
 * 前端异常上报。
 *
 * 错误边界以前只往浏览器控制台打一行，用户截图发过来时我们看不到堆栈，
 * 只能靠猜。这里把渲染异常、脚本异常和未处理的 Promise 拒绝写到后端
 * （<数据根>/logs/client-errors.log），排查时直接读文件。
 *
 * 纪律：上报本身绝不能再抛异常，也不能阻塞页面——一律 fire-and-forget。
 */

export type ClientErrorKind = 'render' | 'window' | 'promise' | 'chunk';

type Payload = {
  message: string;
  stack?: string;
  component_stack?: string;
  url?: string;
  kind?: ClientErrorKind;
};

let reported = 0;

export function reportClientError(payload: Payload): void {
  if (reported >= 30) return; // 一个页面里连环崩时别刷爆日志
  reported += 1;
  try {
    const body = JSON.stringify({
      message: String(payload.message || '未知错误').slice(0, 2000),
      stack: String(payload.stack || '').slice(0, 8000),
      component_stack: String(payload.component_stack || '').slice(0, 8000),
      url: String(payload.url || (typeof location === 'undefined' ? '' : location.href)).slice(0, 1000),
      kind: payload.kind || 'render',
      user_agent: typeof navigator === 'undefined' ? '' : navigator.userAgent.slice(0, 500),
    });
    void fetch('/api/client-errors', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body,
      keepalive: true,
    }).catch(() => undefined);
  } catch {
    /* 上报失败不影响页面 */
  }
}

/** 装上 window 级监听：渲染之外的异常也有记录。返回卸载函数。 */
export function installClientErrorHandlers(): () => void {
  if (typeof window === 'undefined') return () => undefined;

  const onError = (event: ErrorEvent) => {
    const error = event.error;
    const message = event.message || (error instanceof Error ? error.message : '脚本异常');
    // 图片/音频 404 之类会走这里但 event.error 为空、message 是 "Script error."；
    // 这种资源问题另有网络日志，不当作页面崩溃上报，避免淹没真正的异常。
    if (!error && (!message || message === 'Script error.')) return;
    reportClientError({
      message,
      stack: error instanceof Error ? error.stack : '',
      kind: 'window',
    });
  };

  const onRejection = (event: PromiseRejectionEvent) => {
    const reason = event.reason as { name?: string; message?: string; stack?: string } | undefined;
    if (reason?.name === 'AbortError') return; // 主动取消的请求是正常行为
    reportClientError({
      message: reason?.message || String(reason ?? '未处理的 Promise 拒绝'),
      stack: reason?.stack || '',
      kind: 'promise',
    });
  };

  window.addEventListener('error', onError);
  window.addEventListener('unhandledrejection', onRejection);
  return () => {
    window.removeEventListener('error', onError);
    window.removeEventListener('unhandledrejection', onRejection);
  };
}
