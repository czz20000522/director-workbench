/** Shared HTTP and media-URL client for the workbench UI.
 *
 * The panels used to carry near-identical request helpers and media-URL builders.  This
 * module is the single place for JSON parsing, error mapping, timeouts and abort handling;
 * the browser session identity header stays in `sessionFetch`, which wraps `window.fetch`
 * for the whole app.
 */

const DEFAULT_TIMEOUT_MS = 30_000;

export class ApiError extends Error {
  readonly status: number;
  readonly code?: string;

  constructor(message: string, status: number, code?: string) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
  }
}

function describe(detail: unknown, status: number): { message: string; code?: string } {
  if (typeof detail === 'string' && detail) return { message: detail };
  if (detail && typeof detail === 'object') {
    const value = detail as { message?: unknown; code?: unknown };
    const message = typeof value.message === 'string' && value.message
      ? value.message
      : `请求失败（HTTP ${status}）`;
    const code = typeof value.code === 'string' ? value.code : undefined;
    return { message, code };
  }
  return { message: `请求失败（HTTP ${status}）` };
}

export type RequestOptions = {
  /** Extra fetch options (method, headers, body, signal). Kept for the panel helpers. */
  init?: RequestInit;
  /** Convenience body serialized as JSON with the matching Content-Type. */
  json?: unknown;
  timeoutMs?: number;
};

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { init, json, timeoutMs = DEFAULT_TIMEOUT_MS } = options;
  const controller = new AbortController();
  const timer = globalThis.setTimeout(() => controller.abort(), timeoutMs);
  const external = init?.signal;
  const abort = () => controller.abort();
  if (external) {
    if (external.aborted) controller.abort();
    else external.addEventListener('abort', abort, { once: true });
  }
  try {
    const headers = new Headers(init?.headers);
    let body = init?.body;
    if (json !== undefined) {
      headers.set('Content-Type', 'application/json');
      body = JSON.stringify(json);
    }
    const response = await fetch(path, { ...init, headers, body, signal: controller.signal });
    if (!response.ok) {
      let detail: unknown;
      try {
        const body = await response.json();
        // FastAPI wraps application errors as {"detail": ...}; a plain body is tolerated.
        detail = body && typeof body === 'object' && 'detail' in body
          ? (body as { detail?: unknown }).detail
          : body;
      } catch { /* non-JSON error body */ }
      const mapped = describe(detail, response.status);
      throw new ApiError(mapped.message, response.status, mapped.code);
    }
    if (response.status === 204) return undefined as T;
    return await response.json() as T;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if (error instanceof DOMException && error.name === 'AbortError') {
      throw new ApiError('请求超时或已取消', 0);
    }
    throw error;
  } finally {
    globalThis.clearTimeout(timer);
    external?.removeEventListener('abort', abort);
  }
}

export function mediaUrl(path?: string | null): string | undefined {
  return path ? `/media-file?path=${encodeURIComponent(path)}` : undefined;
}

export function materialUrl(path?: string | null): string | undefined {
  return path ? `/material-file?path=${encodeURIComponent(path)}` : undefined;
}

export function workflowUrl(path?: string | null): string | undefined {
  return path ? `/workflow-file?path=${encodeURIComponent(path)}` : undefined;
}

/** File name proposed by a media/workflow URL, used for download attributes. */
export function downloadName(source: string): string {
  try {
    const path = new URL(source, window.location.href).searchParams.get('path');
    return path ? path.split(/[\\/]/).pop() ?? '' : '';
  } catch {
    return '';
  }
}
