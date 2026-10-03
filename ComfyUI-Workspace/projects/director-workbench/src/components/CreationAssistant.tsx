import { useCallback, useEffect, useRef, useState } from 'react';

type AssistantResult = { project_id?: string; task_id?: string; asset_id?: string };
export type AssistantPresentationEvent = {
  seq: number; kind: 'show_draft' | 'saved' | 'preflight' | 'submitted' | 'candidate' | 'navigate' | 'play' | 'pause';
  project_id?: string; asset_id?: string; task_id?: string; text?: string;
  duration_seconds?: number; aspect_ratio?: string; data?: Record<string, unknown>;
};
type PresentationReceipt = { status: 'presented' | 'not_presented' | 'takeover'; message?: string };
type PresentationJob = { owner: string; requestId: string; event: AssistantPresentationEvent; receipt?: PresentationReceipt };
type AssistantRequest = {
  id: string; status: string; text?: string; message?: string; project_id?: string | null;
  asset_id?: string; task_id?: string; steps?: { name: string; status: string; message?: string }[];
  error?: unknown; next_action?: unknown; result?: AssistantResult & { message?: string; answer?: string };
  presentation_target?: string; events?: AssistantPresentationEvent[];
};
type AssistantSession = {
  id: string; updated_at?: string | number; project_id?: string | null;
  messages?: { role: 'user' | 'assistant'; content: string }[]; requests?: AssistantRequest[];
};
const activeStatuses = new Set(['queued', 'preparing', 'running']);
const terminalStatuses = new Set(['succeeded', 'failed', 'cancelled', 'needs_reconcile']);
const statusLabels: Record<string, string> = {
  queued: '等待助手处理', preparing: '助手准备中', running: '正在执行', succeeded: '助手处理完成',
  failed: '处理失败', cancelled: '助手准备已取消', needs_reconcile: '需要核对执行结果',
};
function readable(value: unknown): string {
  if (typeof value === 'string') return value;
  if (value && typeof value === 'object') {
    const record = value as Record<string, unknown>;
    return readable(record.message || record.detail || record.action);
  }
  return '';
}
function requestPath(sessionId: string, requestId: string) {
  return `/api/assistant/sessions/${encodeURIComponent(sessionId)}/requests/${encodeURIComponent(requestId)}`;
}
class HttpResponseError extends Error {
  constructor(message: string, readonly status: number) { super(message); }
}
async function readJson(path: string, options?: RequestInit) {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 20000);
  try {
    const response = await fetch(path, { ...options, credentials: 'same-origin', signal: controller.signal });
    const data = await response.json();
    if (!response.ok) throw new HttpResponseError(readable(data.detail) || `助手请求失败（${response.status}）`, response.status);
    return data;
  } finally { window.clearTimeout(timeout); }
}

export default function CreationAssistant({ projectId, onResult, onPresentation }: {
  projectId?: string; onResult?: (result: AssistantResult) => void;
  onPresentation?: (event: AssistantPresentationEvent) => Promise<PresentationReceipt>;
}) {
  const [expanded, setExpanded] = useState(false);
  const [sessions, setSessions] = useState<AssistantSession[]>([]);
  const [sessionId, setSessionId] = useState('');
  const [draft, setDraft] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [sending, setSending] = useState(false);
  const [cancelling, setCancelling] = useState('');
  const sendingRef = useRef(false);
  const onResultRef = useRef(onResult);
  onResultRef.current = onResult;
  const onPresentationRef = useRef(onPresentation);
  onPresentationRef.current = onPresentation;
  const reported = useRef(new Set<string>());
  const restoreVersion = useRef(0);
  const [presentationTarget] = useState(() => {
    const key = 'director-workbench:assistant:presentation-target:v1';
    try {
      const saved = sessionStorage.getItem(key);
      if (saved) return saved;
      const target = crypto.randomUUID(); sessionStorage.setItem(key, target); return target;
    } catch { return crypto.randomUUID(); }
  });
  const seenEvents = useRef(new Map<string, number>());
  const presentationJobs = useRef<PresentationJob[]>([]);
  const presenting = useRef(false);
  const takeovers = useRef(new Set<string>());
  const mounted = useRef(true);
  const presentationTimer = useRef<number | undefined>(undefined);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; window.clearTimeout(presentationTimer.current); };
  }, []);

  const rememberRequest = useCallback((ownerId: string, request: AssistantRequest) => {
    setSessions(previous => previous.map(session => {
      if (session.id !== ownerId) return session;
      const records = session.requests ?? [];
      const older = records.find(record => record.id === request.id);
      // A late read must not replace a cancellation or final receipt with an earlier running state.
      if (older && terminalStatuses.has(older.status) && activeStatuses.has(request.status)) return session;
      const merged = { ...older, ...request };
      return { ...session, requests: older ? records.map(record => record.id === request.id ? merged : record) : [...records, merged] };
    }));
  }, []);

  const restore = useCallback(async () => {
    const version = ++restoreVersion.current;
    setLoading(true);
    try {
      const data = await readJson('/api/assistant/sessions');
      if (version !== restoreVersion.current || !mounted.current) return;
      if (!Array.isArray(data.sessions)) throw new Error('助手会话记录格式不完整，请重新读取。');
      const records: AssistantSession[] = data.sessions.filter((session: AssistantSession) => typeof session.id === 'string');
      // Restored events are history: a refresh never repeats navigation, draft replacement or playback.
      for (const session of records) for (const request of session.requests ?? []) {
        seenEvents.current.set(`${session.id}/${request.id}`, Math.max(0, ...(request.events ?? []).map(event => event.seq)));
        const result = { project_id: request.result?.project_id || request.project_id || undefined, task_id: request.result?.task_id || request.task_id, asset_id: request.result?.asset_id || request.asset_id };
        if (result.task_id || result.asset_id || request.result?.project_id) reported.current.add(`${session.id}:${request.id}:${result.project_id}:${result.task_id}:${result.asset_id}`);
      }
      setSessions(records);
      setSessionId(previous => records.some(session => session.id === previous) ? previous : records[0]?.id ?? '');
    } catch (reason) { if (version === restoreVersion.current && mounted.current) setError(reason instanceof Error ? reason.message : '助手会话读取失败。'); }
    finally { if (version === restoreVersion.current && mounted.current) setLoading(false); }
  }, []);

  useEffect(() => { void restore(); }, [restore]);

  useEffect(() => {
    for (const session of sessions) for (const request of session.requests ?? []) {
      const result: AssistantResult = {
        project_id: request.result?.project_id || request.project_id || undefined,
        task_id: request.result?.task_id || request.task_id,
        asset_id: request.result?.asset_id || request.asset_id,
      };
      if (request.presentation_target || request.events?.length || (!result.task_id && !result.asset_id && !request.result?.project_id)) continue;
      const key = `${session.id}:${request.id}:${result.project_id}:${result.task_id}:${result.asset_id}`;
      if (!reported.current.has(key)) { reported.current.add(key); onResultRef.current?.(result); }
    }
  }, [sessions]);

  const presentQueued = useCallback(async (): Promise<void> => {
    if (presenting.current || !mounted.current) return;
    presenting.current = true;
    try {
      while (presentationJobs.current.length && mounted.current) {
        const job = presentationJobs.current[0];
        const key = `${job.owner}/${job.requestId}`;
        if (!job.receipt) {
          try {
            job.receipt = takeovers.current.has(key) ? { status: 'takeover', message: '你已手动接管创作，后续页面动作已停止。' }
              : onPresentationRef.current ? await onPresentationRef.current(job.event)
                : { status: 'not_presented', message: '当前页面没有可用的呈现入口。' };
          } catch (reason) { job.receipt = { status: 'not_presented', message: reason instanceof Error ? reason.message : '页面未完成呈现。' }; }
          if (job.receipt.status === 'takeover') takeovers.current.add(key);
        }
        if (!mounted.current) return;
        try {
          await readJson(`${requestPath(job.owner, job.requestId)}/presentation`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ target: presentationTarget, seq: job.event.seq, ...job.receipt }) });
          presentationJobs.current.shift();
        } catch (reason) {
          setError(`${reason instanceof Error ? reason.message : '页面回执保存失败。'} 将重试保存回执，页面动作不会重复执行。`);
          presentationTimer.current = window.setTimeout(() => void presentQueued(), 1800);
          return;
        }
      }
    } finally { presenting.current = false; }
  }, [presentationTarget]);

  useEffect(() => {
    for (const session of sessions) for (const request of session.requests ?? []) {
      if (request.presentation_target !== presentationTarget) continue;
      const key = `${session.id}/${request.id}`;
      const events = [...(request.events ?? [])].sort((left, right) => left.seq - right.seq);
      for (const event of events) {
        if (!Number.isInteger(event.seq) || event.seq <= (seenEvents.current.get(key) ?? 0)) continue;
        seenEvents.current.set(key, event.seq);
        presentationJobs.current.push({ owner: session.id, requestId: request.id, event });
      }
    }
    void presentQueued();
  }, [sessions, presentationTarget, presentQueued]);

  const pending = sessions.flatMap(session => (session.requests ?? []).filter(request => activeStatuses.has(request.status)).map(request => ({ owner: session.id, id: request.id })));
  const pendingKey = pending.map(request => `${request.owner}/${request.id}`).join('|');
  useEffect(() => {
    if (!pendingKey) return;
    let disposed = false;
    let timer: number;
    async function poll() {
      for (const request of pending) {
        if (disposed) return;
        try {
          const data = await readJson(requestPath(request.owner, request.id));
          if (data.id !== request.id || typeof data.status !== 'string') throw new Error('助手回执尚未完整返回，请继续查询。');
          if (!disposed) rememberRequest(request.owner, data);
        } catch (reason) {
          if (!disposed) setError(`${reason instanceof Error ? reason.message : '助手进度读取失败。'} 原请求仍保留，将继续查询。`);
        }
      }
      if (!disposed) timer = window.setTimeout(() => void poll(), 1800);
    }
    void poll();
    return () => { disposed = true; window.clearTimeout(timer); };
    // The key tracks the stable request identities; receipt updates must not start overlapping polls.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pendingKey, rememberRequest]);

  async function send() {
    if (!draft.trim() || loading || sendingRef.current || pending.length) return;
    sendingRef.current = true; setSending(true);
    const text = draft;
    const contextProject = projectId;
    let owner = sessionId;
    try {
      if (!owner) {
        let session: AssistantSession;
        try {
          session = await readJson('/api/assistant/sessions', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ project_id: contextProject }) });
          if (!session.id) throw new Error('助手会话尚未确认。');
        } catch (reason) {
          // Recover a potentially created session by reading; never repeat the unknown POST.
          await restore();
          throw reason;
        }
        owner = session.id;
        setSessions(previous => [...previous, session]); setSessionId(owner);
      }
      const requestId = crypto.randomUUID();
      rememberRequest(owner, { id: requestId, status: 'queued', text, project_id: contextProject, message: '正在确认助手请求…' });
      setDraft(previous => previous === text ? '' : previous);
      try {
        const request = await readJson(`/api/assistant/sessions/${encodeURIComponent(owner)}/requests`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ request_id: requestId, text, project_id: contextProject, presentation_target: presentationTarget }) });
        if (request.id !== requestId || typeof request.status !== 'string') throw new Error('助手提交回执不完整。');
        rememberRequest(owner, { ...request, text });
      } catch (reason) {
        setError(`${reason instanceof Error ? reason.message : '提交响应未收到。'} 正按原请求编号查询，未重复提交。`);
        try {
          const request = await readJson(requestPath(owner, requestId));
          if (request.id !== requestId || typeof request.status !== 'string') throw new Error('原请求回执尚未确认。');
          rememberRequest(owner, { ...request, text });
        } catch {
          const rejected = reason instanceof HttpResponseError && reason.status >= 400 && reason.status < 500 && reason.status !== 408 && reason.status !== 409;
          rememberRequest(owner, { id: requestId, status: rejected ? 'failed' : 'queued', text, project_id: contextProject, message: rejected ? '服务器拒绝了这次请求，可以修改后继续。' : '提交结果尚未确认，正在查询原请求；请勿重复发送。' });
        }
      }
    } catch (reason) { setError(reason instanceof Error ? reason.message : '助手请求失败，原文仍保留，可继续。'); }
    finally { sendingRef.current = false; setSending(false); }
  }

  async function cancel(owner: string, request: AssistantRequest) {
    if (cancelling) return;
    setCancelling(request.id);
    try {
      const data = await readJson(`${requestPath(owner, request.id)}/cancel`, { method: 'POST' });
      if (data.id !== request.id || typeof data.status !== 'string') throw new Error('取消回执尚未确认，请继续查询原请求。');
      rememberRequest(owner, data);
    } catch (reason) { setError(reason instanceof Error ? reason.message : '取消助手准备失败，请查询进度。'); }
    finally { setCancelling(''); }
  }

  const selected = sessions.find(session => session.id === sessionId);
  const selectedRequests = selected?.requests ?? [];
  return <aside className="director-operation-panel" aria-label="创作助手" style={{ position: 'fixed', right: 16, bottom: 16, zIndex: 60, width: expanded ? 'min(420px, calc(100vw - 32px))' : 'auto', maxHeight: 'calc(100dvh - 32px)', overflow: 'auto', boxShadow: 'var(--shadow)' }}>
    <button type="button" className="text-button" aria-expanded={expanded} aria-controls="creation-assistant-content" onClick={() => setExpanded(value => !value)}><strong>创作助手</strong> {expanded ? '收起' : '聊聊想法'}{pending.length > 0 ? ` · ${pending.length} 条处理中` : error ? ' · 有提示' : ''}</button>
    {expanded && <div id="creation-assistant-content">
      <p className="director-muted">说说你想拍什么，默认先做一段 5 秒横屏。可以说“先解释”“只写草稿”“先预检”，或明确说“授权生成”。</p>
      <small className="director-muted">云端只处理输入文字和必要创作事实，不传密钥、token 或音视频。</small>
      {sessions.length > 1 && <label className="director-review-note">历史对话<select aria-label="历史对话" value={sessionId} onChange={event => setSessionId(event.target.value)}>{sessions.map((session, index) => <option key={session.id} value={session.id}>{session.messages?.find(message => message.role === 'user')?.content.slice(0, 28) || `对话 ${index + 1}`}</option>)}</select></label>}
      <section aria-label="助手对话记录" style={{ maxHeight: '38dvh', overflow: 'auto', overflowWrap: 'anywhere' }}>
        {(selected?.messages ?? []).map((message, index) => <div key={`message-${index}`}><strong>{message.role === 'user' ? '你' : '助手'}</strong><p style={{ whiteSpace: 'pre-wrap' }}>{message.content}</p></div>)}
        {selectedRequests.map(request => {
          const result = { project_id: request.result?.project_id || request.project_id || undefined, task_id: request.result?.task_id || request.task_id, asset_id: request.result?.asset_id || request.asset_id };
          const savedText = (selected?.messages ?? []).some(message => message.role === 'user' && message.content === request.text);
          return <article key={request.id} aria-label={`助手请求 ${request.id}`}>
            {request.text && !savedText && <><strong>你</strong><p style={{ whiteSpace: 'pre-wrap' }}>{request.text}</p></>}
            <p role="status"><strong>{statusLabels[request.status] || request.status}</strong></p>
            {request.message && <p style={{ whiteSpace: 'pre-wrap' }}>{request.message}</p>}
            {(request.steps ?? []).length > 0 && <ol>{request.steps!.map((step, index) => <li key={`${step.name}-${index}`}>{step.name} · {statusLabels[step.status] || step.status}{step.message ? `：${step.message}` : ''}</li>)}</ol>}
            {readable(request.error) && <p role="alert" className="director-error">{readable(request.error)}</p>}
            {readable(request.next_action) && <p>下一步：{readable(request.next_action)}</p>}
            {request.result?.answer && <p style={{ whiteSpace: 'pre-wrap' }}>{request.result.answer}</p>}
            {request.result?.message && request.result.message !== request.message && <p style={{ whiteSpace: 'pre-wrap' }}>{request.result.message}</p>}
            {result.task_id && <p>任务编号：<code>{result.task_id}</code><br /><small>任务已提交；取消助手准备不会停止这个 GPU 任务，请在任务队列中操作。</small></p>}
            {(result.task_id || result.asset_id || request.result?.project_id) && <button type="button" className="button secondary compact" onClick={() => onResultRef.current?.(result)}>{result.asset_id ? `查看候选 ${result.asset_id}` : result.task_id ? '查看任务' : '打开作品'}</button>}
            {activeStatuses.has(request.status) && !result.task_id && <button type="button" className="button secondary compact" disabled={Boolean(cancelling)} onClick={() => void cancel(selected!.id, request)}>{cancelling === request.id ? '正在取消…' : '取消助手准备'}</button>}
          </article>;
        })}
      </section>
      {error && <div className="director-error"><p role="alert">{error}</p><button type="button" className="text-button" onClick={() => setError('')}>收起提示</button></div>}
      {loading && <p role="status">正在恢复对话…</p>}
      <form onSubmit={event => { event.preventDefault(); void send(); }}>
        <label className="director-review-note">你的想法<textarea aria-label="你的想法" rows={3} value={draft} onChange={event => setDraft(event.target.value)} placeholder="例如：先写一段猫咪下班回家的草稿" /></label>
        <small className="director-muted">{projectId ? '下一条消息将使用当前作品。' : '还没有作品也可以开始聊。'} 历史请求保留原来的作品归属。</small>
        <div className="director-panel-actions"><button type="submit" className="button primary compact" disabled={loading || sending || Boolean(pending.length) || !draft.trim()}>{sending ? '正在发送…' : pending.length ? '等待当前请求回执' : '发送'}</button><button type="button" className="button secondary compact" disabled={loading || sending || Boolean(pending.length)} onClick={() => void restore()}>重新读取对话</button></div>
      </form>
    </div>}
  </aside>;
}
