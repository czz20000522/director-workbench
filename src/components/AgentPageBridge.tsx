import { useCallback, useEffect, useRef, useState } from 'react';

export type AgentPageAction = {
  id: string; seq: number; kind: string;
  target: { project_id?: string; asset_id?: string; task_id?: string; revision?: string | number };
  values: { prompt?: string; duration_seconds?: number; generation?: unknown; page?: string; [key: string]: unknown };
  status: 'pending' | 'presented' | 'not_presented' | 'takeover'; expires_at?: string | number;
};
export type AgentPageActionReceipt = { status: 'presented' | 'not_presented' | 'takeover'; message?: string };
type PendingAction = { action: AgentPageAction; receipt?: AgentPageActionReceipt; ackHeaders?: Record<string, string> };

function createPageId() {
  const key = 'director-workbench:agent-page:v1';
  try {
    const saved = sessionStorage.getItem(key);
    if (saved) return saved;
    const id = crypto.randomUUID(); sessionStorage.setItem(key, id); return id;
  } catch { return crypto.randomUUID(); }
}
async function exchange(path: string, options?: RequestInit) {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), 20000);
  try {
    const response = await fetch(path, { ...options, credentials: 'same-origin', signal: controller.signal });
    if (!response.ok) {
      if (response.status === 401) throw new Error('登录已过期，页面引导暂不可用。');
      throw new Error(`Agent 页面连接暂不可用（${response.status}）。`);
    }
    return response.status === 204 ? {} : await response.json();
  } finally { window.clearTimeout(timer); }
}
function expired(value: AgentPageAction['expires_at']) {
  if (value === undefined) return false;
  const milliseconds = typeof value === 'number' ? value < 1e12 ? value * 1000 : value : Date.parse(value);
  return Number.isFinite(milliseconds) && milliseconds <= Date.now();
}

export default function AgentPageBridge({ onAction }: {
  onAction: (event: AgentPageAction) => Promise<AgentPageActionReceipt>;
}) {
  const [pageId] = useState(createPageId);
  const [enabled, setEnabled] = useState(false);
  const [ready, setReady] = useState(false);
  const [changing, setChanging] = useState(false);
  const [error, setError] = useState('');
  const [lastReceipt, setLastReceipt] = useState<AgentPageActionReceipt | null>(null);
  const mounted = useRef(true);
  const enabledRef = useRef(false);
  const takeover = useRef(false);
  const onActionRef = useRef(onAction); onActionRef.current = onAction;
  const registrationVersion = useRef(0);
  const pageKey = useRef<string | undefined>(undefined);
  const registrationPromise = useRef<Promise<void> | null>(null);
  const cursor = useRef(0);
  const seen = useRef(new Set<string>());
  const jobs = useRef<PendingAction[]>([]);
  const processing = useRef(false);
  const retryTimer = useRef<number | undefined>(undefined);
  const basePath = `/api/agent/pages/${encodeURIComponent(pageId)}`;

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; window.clearTimeout(retryTimer.current); };
  }, []);

  const registerPage = useCallback(async (allow: boolean, baseline = false) => {
    const version = ++registrationVersion.current;
    setChanging(true);
    try {
      const registration = await exchange('/api/agent/pages', { method: 'POST', headers: { 'Content-Type': 'application/json', ...(pageKey.current ? { 'X-Agent-Page-Key': pageKey.current } : {}) }, body: JSON.stringify({ page_id: pageId, label: document.title || '导演工作台', enabled: allow }) });
      if (registration.page_id !== pageId) throw new Error('页面注册回执不完整，请重新连接。');
      if (version !== registrationVersion.current || !mounted.current) return;
      pageKey.current = typeof registration.page_key === 'string' ? registration.page_key : undefined;
      if (baseline) {
        const history = await exchange(`${basePath}/actions?after=0`, { headers: pageKey.current ? { 'X-Agent-Page-Key': pageKey.current } : {} });
        if (!Array.isArray(history.actions)) throw new Error('页面动作记录暂不可用。');
        if (version !== registrationVersion.current || !mounted.current) return;
        // Refresh records history without replaying navigation, editor changes or playback.
        cursor.current = Math.max(cursor.current, Number(history.cursor) || 0, ...history.actions.map((action: AgentPageAction) => Number(action.seq) || 0));
        for (const action of history.actions) seen.current.add(action.id);
      }
      if (version !== registrationVersion.current || !mounted.current) return;
      enabledRef.current = allow; setEnabled(allow); setReady(true); setError('');
      if (allow) takeover.current = false;
    } catch (reason) {
      if (version === registrationVersion.current && mounted.current) {
        enabledRef.current = false; setEnabled(false);
        setError(reason instanceof Error ? reason.message : 'Agent 页面连接暂不可用。');
      }
    } finally { if (version === registrationVersion.current && mounted.current) setChanging(false); }
  }, [pageId, basePath]);

  const register = useCallback((allow: boolean, baseline = false) => {
    const operation = registerPage(allow, baseline);
    registrationPromise.current = operation;
    return operation;
  }, [registerPage]);

  useEffect(() => { void register(false, true); }, [register]);

  const processJobs = useCallback(async (): Promise<void> => {
    if (processing.current || !mounted.current) return;
    processing.current = true;
    try {
      while (jobs.current.length && mounted.current) {
        const job = jobs.current[0];
        if (!job.receipt) {
          if (!enabledRef.current || takeover.current) job.receipt = { status: 'takeover', message: '你已手动接管此标签页。' };
          else if (expired(job.action.expires_at)) job.receipt = { status: 'not_presented', message: '页面动作已过期，未执行。' };
          else {
            try { job.receipt = await onActionRef.current(job.action); }
            catch (reason) { job.receipt = { status: 'not_presented', message: reason instanceof Error ? reason.message : '页面未完成操作。' }; }
            if (takeover.current) job.receipt = { status: 'takeover', message: '你已手动接管此标签页。' };
          }
          if (job.receipt.status === 'takeover' && !takeover.current) {
            takeover.current = true; enabledRef.current = false; setEnabled(false);
            void register(false);
          }
          setLastReceipt(job.receipt);
        }
        if (!mounted.current) return;
        try {
          if (!job.ackHeaders) {
            // A permission change may register a fresh nonce before its takeover receipt is saved.
            await registrationPromise.current;
            if (!mounted.current) return;
            job.ackHeaders = { 'Content-Type': 'application/json', ...(pageKey.current ? { 'X-Agent-Page-Key': pageKey.current } : {}) };
          }
          // Keep both the result and nonce unchanged after an uncertain ACK response.
          const acknowledged = await exchange(`${basePath}/actions/${encodeURIComponent(job.action.id)}/ack`, { method: 'POST', headers: job.ackHeaders, body: JSON.stringify(job.receipt) });
          if (['presented', 'not_presented', 'takeover'].includes(acknowledged.status)) {
            job.receipt = { status: acknowledged.status, message: acknowledged.message };
            setLastReceipt(job.receipt);
            if (acknowledged.status === 'takeover' && !takeover.current) {
              takeover.current = true; enabledRef.current = false; setEnabled(false);
              void register(false);
            }
          }
          jobs.current.shift();
        } catch (reason) {
          setError(`${reason instanceof Error ? reason.message : '页面回执保存失败。'} 将重试同一回执，页面操作不会重复。`);
          retryTimer.current = window.setTimeout(() => void processJobs(), 1000);
          return;
        }
      }
    } finally { processing.current = false; }
  }, [basePath, register]);

  useEffect(() => {
    if (!ready) return;
    let disposed = false;
    let timer: number;
    async function poll() {
      try {
        const data = await exchange(`${basePath}/actions?after=${cursor.current}`, { headers: pageKey.current ? { 'X-Agent-Page-Key': pageKey.current } : {} });
        if (disposed) return;
        if (!Array.isArray(data.actions)) throw new Error('页面动作记录暂不可用。');
        const actions: AgentPageAction[] = [...data.actions].sort((left, right) => left.seq - right.seq);
        for (const action of actions) {
          if (!Number.isInteger(action.seq) || action.seq <= cursor.current || seen.current.has(action.id)) continue;
          seen.current.add(action.id);
          if (action.status === 'pending' && (enabledRef.current || takeover.current)) jobs.current.push({ action });
        }
        cursor.current = Math.max(cursor.current, Number(data.cursor) || 0, ...actions.map(action => Number(action.seq) || 0));
        void processJobs();
      } catch (reason) {
        if (!disposed) setError(reason instanceof Error ? reason.message : 'Agent 页面连接暂不可用。');
      }
      if (!disposed) timer = window.setTimeout(() => void poll(), 1000);
    }
    void poll();
    return () => { disposed = true; window.clearTimeout(timer); };
  }, [ready, basePath, processJobs]);

  const takeControl = useCallback(() => {
    if (!enabledRef.current) return;
    takeover.current = true; enabledRef.current = false; setEnabled(false);
    setLastReceipt({ status: 'takeover', message: '你已手动接管此标签页，Agent 后续页面动作不会执行。' });
    void register(false); void processJobs();
  }, [register, processJobs]);

  useEffect(() => {
    const interacted = (event: Event) => {
      if (!event.isTrusted || !enabledRef.current || !(event.target instanceof Element)) return;
      const target = event.target;
      if (target.closest('[aria-label="Agent 标签页引导"], [aria-label="连接你的Agent"], [data-agent-connect]')) return;
      const editing = ['input', 'change'].includes(event.type) && target.closest('input, textarea, select, [contenteditable="true"]');
      const control = target.closest('button, a[href], summary, [role="button"], [role="tab"], [role="menuitem"], video, audio, input[type="checkbox"], input[type="radio"], input[type="range"], input[type="button"], input[type="submit"]');
      const activating = event.type === 'click' || (event instanceof KeyboardEvent && ['Enter', ' ', 'ArrowLeft', 'ArrowRight'].includes(event.key));
      if (editing || (control && activating)) takeControl();
    };
    for (const name of ['click', 'input', 'change', 'keydown']) document.addEventListener(name, interacted, true);
    return () => { for (const name of ['click', 'input', 'change', 'keydown']) document.removeEventListener(name, interacted, true); };
  }, [takeControl]);

  return <section className="director-operation-panel" aria-label="Agent 标签页引导">
    <label><input type="checkbox" checked={enabled} disabled={!ready || changing} onChange={event => { if (event.target.checked) void register(true); else takeControl(); }} /> 允许 Agent 引导此标签页</label>
    <p className="director-muted">{enabled ? '此标签页已允许页面引导。你可以随时手动接管。' : '页面引导未允许，你可以继续手动创作。'}</p>
    <p className="director-muted" style={{ overflowWrap: 'anywhere' }}>页面编号：<output aria-label="页面编号">{pageId}</output></p>
    <button type="button" className="button secondary compact" disabled={!ready || changing || !enabled} onClick={takeControl}>手动接管此标签页</button>
    {lastReceipt && <p role="status">{lastReceipt.status === 'presented' ? '页面动作已呈现。' : lastReceipt.status === 'takeover' ? '已手动接管。' : '页面动作未呈现。'}{lastReceipt.message ? ` ${lastReceipt.message}` : ''}</p>}
    {error && <p role="alert" className="director-error">{error}</p>}
    {!ready && error && <button type="button" className="text-button" disabled={changing} onClick={() => void register(false, true)}>重新连接页面</button>}
  </section>;
}
