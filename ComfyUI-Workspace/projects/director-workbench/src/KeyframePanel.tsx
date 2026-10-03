import { useEffect, useId, useRef, useState } from 'react';
import TaskQueueStatus, { type QueueReceipt } from './TaskQueueStatus';
import MaterialSelect, { type MaterialOption } from './MaterialSelect';

type Mode = 'text' | 'reference';
type Submission = { mode?: Mode; width?: number; height?: number; prompt: string; reference_image?: string; seed: number; idempotency_key: string };
type Task = { id: string; status: string; queue?: QueueReceipt['queue']; error?: string; result?: { output?: string } };
type Saved = { mode: Mode; prompt: string; reference_image: string; seed: number; pending?: Submission };
class RequestError extends Error {
  constructor(message: string, readonly status: number) { super(message); }
}
const terminal = ['succeeded', 'failed', 'stopped'];
const labels: Record<string, string> = { scheduler_waiting: '已接受 · 等待资源', batch_waiting: '已接受 · 等待依赖', submitting: '正在提交', queued: '排队中', running: '生成中', succeeded: '已生成 · 待审阅', failed: '生成失败', stopped: '已停止', stop_requested: '正在停止', stopping: '正在停止', needs_reconcile: '需要核对结果' };

export default function KeyframePanel({ projectId, options, onBrowse, onComplete }: {
  projectId: string; options: MaterialOption[]; onBrowse: () => void; onComplete: () => void;
}) {
  const storageKey = `director-workbench:${projectId}:keyframe:v1`;
  const promptId = useId();
  const modeId = useId();
  const [saved, setSaved] = useState<Saved>(() => {
    try {
      const value = JSON.parse(localStorage.getItem(storageKey) || '{}');
      return { mode: value.pending ? (value.pending.mode === 'text' ? 'text' : 'reference') : value.mode === 'text' ? 'text' : 'reference', prompt: typeof value.prompt === 'string' ? value.prompt : '', reference_image: typeof value.reference_image === 'string' ? value.reference_image : '',
        seed: Number.isInteger(value.seed) && value.seed >= 0 && value.seed <= 4294967295 ? value.seed : 340921,
        pending: value.pending && typeof value.pending.idempotency_key === 'string' && typeof value.pending.prompt === 'string' && (value.pending.mode === 'text' || typeof value.pending.reference_image === 'string') && Number.isInteger(value.pending.seed) ? value.pending : undefined };
    } catch { return { mode: 'reference', prompt: '', reference_image: '', seed: 340921 }; }
  });
  const [task, setTask] = useState<Task | null>(null);
  const pendingKey = useRef(saved.pending?.idempotency_key);
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);
  const [capability, setCapability] = useState<{ mode: Mode; available: boolean; missing_nodes?: string[]; missing_models?: string[]; reason?: string; requires_reference_image?: boolean } | null>(null);
  const mounted = useRef(true);
  const working = useRef(false);
  const completed = useRef('');
  const completion = useRef(onComplete);
  completion.current = onComplete;
  const base = `/api/projects/${encodeURIComponent(projectId)}`;
  function persist(next: Saved) {
    try { localStorage.setItem(storageKey, JSON.stringify(next)); pendingKey.current = next.pending?.idempotency_key; setSaved(next); return true; }
    catch { setMessage('本机无法保存提交凭据，请恢复浏览器存储后重试。'); return false; }
  }
  function accept(next: Task, expectedKey: string | undefined) {
    if (!mounted.current || !expectedKey || pendingKey.current !== expectedKey) return;
    setTask(next); setMessage('');
    if (next.status === 'succeeded' && completed.current !== next.id) {
      completed.current = next.id; completion.current();
    }
  }
  async function request(url: string, init?: RequestInit): Promise<Task> {
    const response = await fetch(url, init);
    const data = await response.json();
    if (!response.ok) throw new RequestError(typeof data.detail === 'string' ? data.detail : data.detail?.message || '请求未获确认，请核对原任务。', response.status);
    return data;
  }
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);
  useEffect(() => {
    let active = true;
    setCapability(null);
    fetch(`/api/keyframe-capability?mode=${saved.mode}`).then(response => {
      if (!response.ok) throw new Error('能力检查失败');
      return response.json();
    }).then(data => {
      if (active) setCapability({ ...data, mode: saved.mode });
    }).catch(() => {
      if (active) setCapability({ mode: saved.mode, available: false, reason: '无法检查生成环境，请检查连接后刷新。' });
    });
    return () => { active = false; };
  }, [saved.mode]);
  useEffect(() => {
    if (!saved.pending) return;
    let active = true;
    let polling = false;
    async function poll() {
      if (polling) return;
      polling = true;
      try {
        const next = await request(task ? `${base}/tasks/${encodeURIComponent(task.id)}` : `${base}/submission-receipt?key=${encodeURIComponent(saved.pending!.idempotency_key)}`);
        if (active) accept(next, saved.pending?.idempotency_key);
      } catch (error) {
        if (active && pendingKey.current === saved.pending?.idempotency_key && !working.current) setMessage(error instanceof Error ? error.message : '暂时无法核对任务');
      }
      finally { polling = false; }
    }
    void poll();
    const timer = window.setInterval(() => { if (!task || !terminal.includes(task.status)) void poll(); }, 3000);
    return () => { active = false; window.clearInterval(timer); };
  }, [saved.pending?.idempotency_key, task?.id, task?.status]);
  async function act(action: 'submit' | 'stop' | 'reconcile' | 'check') {
    if (working.current) return;
    working.current = true; setBusy(true);
    let operationKey = saved.pending?.idempotency_key;
    try {
      let next: Task;
      if (action === 'submit') {
        const pending: Submission = saved.pending || { mode: saved.mode, prompt: saved.prompt.trim(), ...(saved.mode === 'text' ? { width: 768, height: 1344 } : { reference_image: saved.reference_image }), seed: saved.seed, idempotency_key: `keyframe-${crypto.randomUUID()}` };
        if (!persist({ ...saved, pending })) return;
        operationKey = pending.idempotency_key;
        next = await request(`${base}/keyframe-tasks`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(pending) });
      } else if (action === 'check') {
        next = await request(`${base}/submission-receipt?key=${encodeURIComponent(saved.pending!.idempotency_key)}`);
      } else {
        next = await request(`${base}/tasks/${encodeURIComponent(task!.id)}/${action}`, { method: 'POST' });
      }
      accept(next, operationKey);
    } catch (error) {
      if (mounted.current) {
        // Validation failures occur before a task is admitted; ambiguous failures retain the receipt.
        if (action === 'submit' && error instanceof RequestError && error.status === 422) {
          persist({ mode: saved.mode, prompt: saved.prompt, reference_image: saved.reference_image, seed: saved.seed });
        }
        setMessage(error instanceof Error ? error.message : '请求未获确认，请核对原任务。');
      }
    }
    finally { working.current = false; if (mounted.current) setBusy(false); }
  }
  return <details className="director-operation-panel" aria-label="生成关键帧">
    <summary>生成关键帧 {task && `· ${labels[task.status] || task.status}`}</summary>
    <div className="director-creation-grid">
      <div><label htmlFor={modeId}>生成方式</label><select id={modeId} value={saved.mode} disabled={Boolean(saved.pending)} onChange={event => persist({ ...saved, mode: event.target.value as Mode })}><option value="text">文字生图</option><option value="reference">参考图改图</option></select></div>
      {saved.mode === 'text' && <span className="director-muted">竖屏 · 768 × 1344</span>}
      <div className="director-creation-wide"><label htmlFor={promptId}>画面描述</label><textarea id={promptId} rows={3} maxLength={6000} value={saved.prompt} disabled={Boolean(saved.pending)} placeholder="描述角色、场景、姿态和构图" onChange={event => persist({ ...saved, prompt: event.target.value })} /></div>
      {saved.mode === 'reference' && <fieldset className="director-creation-wide" disabled={Boolean(saved.pending)}><MaterialSelect label="参考图片" kind="image" value={saved.reference_image} options={options} onChange={value => persist({ ...saved, reference_image: value })} onBrowse={onBrowse} /></fieldset>}
      <details className="director-creation-wide"><summary>高级：固定种子</summary><label>种子<input type="number" min={0} max={4294967295} step={1} value={saved.seed} disabled={Boolean(saved.pending)} onChange={event => persist({ ...saved, seed: Number(event.target.value) })} /></label></details>
    </div>
    <p className="director-muted">生成后先审阅，再选择为分镜首帧或尾帧。现有画面会保留。</p>
    {(!capability?.available || capability.mode !== saved.mode) && <p role="status">{!capability || capability.mode !== saved.mode ? '正在检查生成环境…' : capability.reason || '关键帧生成环境尚未就绪。'}</p>}
    {capability && !capability.available && <details><summary>查看缺少的资源</summary><p>{[...(capability.missing_nodes || []), ...(capability.missing_models || [])].join("、") || "请检查生成服务连接。"}</p></details>}
    {message && <p role="status">{message}</p>}
    {task?.error && <p role="status">{task.error}</p>}
    <div className="director-form-actions">
      {!saved.pending && <button className="button primary compact" disabled={busy || (!capability?.available || capability.mode !== saved.mode) || !saved.prompt.trim() || (saved.mode === 'reference' && !saved.reference_image) || !Number.isInteger(saved.seed) || saved.seed < 0 || saved.seed > 4294967295} onClick={() => void act('submit')}>生成关键帧候选</button>}
      {saved.pending && !task && <><button className="button secondary compact" disabled={busy} onClick={() => void act('check')}>核对关键帧提交</button><button className="button secondary compact" disabled={busy || (!capability?.available || capability.mode !== saved.mode)} onClick={() => void act('submit')}>使用原凭据重试</button></>}
      {task && !terminal.includes(task.status) && task.status !== 'needs_reconcile' && <button className="button secondary compact" disabled={busy} onClick={() => void act('stop')}>停止关键帧任务</button>}
      {task?.status === 'needs_reconcile' && <button className="button secondary compact" disabled={busy} onClick={() => void act('reconcile')}>核对关键帧结果</button>}
      {task && terminal.includes(task.status) && <button className="button secondary compact" disabled={busy} onClick={() => { if (persist({ mode: saved.mode, prompt: saved.prompt, reference_image: saved.reference_image, seed: saved.seed })) setTask(null); }}>准备下一张关键帧</button>}
      {task && <TaskQueueStatus task={{ ...task, asset_id: '关键帧' }} />}
      {task && <span>{labels[task.status] || task.status}</span>}
    </div>
  </details>;
}
