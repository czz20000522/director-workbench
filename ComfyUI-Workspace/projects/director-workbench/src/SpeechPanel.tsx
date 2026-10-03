import { useEffect, useId, useRef, useState } from 'react';
import TaskQueueStatus, { type QueueReceipt } from './TaskQueueStatus';
import MaterialSelect, { type MaterialOption } from './MaterialSelect';

type Submission = { gen_seconds?: number; seed?: number; text: string; mode?: 'reference' | 'design'; voice_reference?: string; voice_description?: string; idempotency_key: string };
type Task = { id: string; status: string; queue?: QueueReceipt['queue']; error?: string; result?: { output?: string } };
type Saved = { gen_seconds: number; seed: number; text: string; mode: 'reference' | 'design'; voice_reference: string; voice_description: string; pending?: Submission };
class RequestError extends Error {
  constructor(message: string, readonly status: number) { super(message); }
}
const terminal = ['succeeded', 'failed', 'stopped'];
const labels: Record<string, string> = { scheduler_waiting: '已接受 · 等待资源', batch_waiting: '已接受 · 等待依赖', submitting: '正在生成', succeeded: '已生成 · 待试听', failed: '生成失败', stopped: '已停止', stop_requested: '正在停止', stopping: '正在停止', needs_reconcile: '需要核对结果' };

export default function SpeechPanel({ projectId, options, onBrowse, onComplete }: {
  projectId: string; options: MaterialOption[]; onBrowse: () => void; onComplete: () => void;
}) {
  const storageKey = `director-workbench:${projectId}:speech:v1`;
  const textId = useId();
  const durationId = useId();
  const seedId = useId();
  const modeId = useId();
  const descriptionId = useId();
  const [saved, setSaved] = useState<Saved>(() => {
    try {
      const value = JSON.parse(localStorage.getItem(storageKey) || '{}');
      return { gen_seconds: typeof value.gen_seconds === 'number' && value.gen_seconds > 0 && value.gen_seconds <= 30 ? value.gen_seconds : 4.5, seed: Number.isInteger(value.seed) && value.seed >= 0 && value.seed <= 4294967295 ? value.seed : 20260923, text: typeof value.text === 'string' ? value.text : '', voice_reference: typeof value.voice_reference === 'string' ? value.voice_reference : '',
        mode: value.mode === 'design' ? 'design' : 'reference', voice_description: typeof value.voice_description === 'string' ? value.voice_description : '',
        pending: value.pending && typeof value.pending.idempotency_key === 'string' && typeof value.pending.text === 'string' && (value.pending.mode === 'design' ? typeof value.pending.voice_description === 'string' : typeof value.pending.voice_reference === 'string') ? value.pending : undefined };
    } catch { return { gen_seconds: 4.5, seed: 20260923, text: '', mode: 'reference', voice_reference: '', voice_description: '' }; }
  });
  const [task, setTask] = useState<Task | null>(null);
  const pendingKey = useRef(saved.pending?.idempotency_key);
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);
  const [capability, setCapability] = useState<{ available: boolean; missing?: string[]; reason?: string } | null>(null);
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
    fetch('/api/speech-capability').then(response => {
      if (!response.ok) throw new Error('能力检查失败');
      return response.json();
    }).then(data => {
      if (mounted.current) setCapability(data);
    }).catch(() => {
      if (mounted.current) setCapability({ available: false, reason: '无法检查声音环境，请检查连接后刷新。' });
    });
    return () => { mounted.current = false; };
  }, []);
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
        const pending = saved.pending || { gen_seconds: saved.gen_seconds, seed: saved.seed, text: saved.text.trim(), mode: saved.mode,
          ...(saved.mode === 'design' ? { voice_description: saved.voice_description.trim() } : { voice_reference: saved.voice_reference }), idempotency_key: `speech-${crypto.randomUUID()}` };
        if (!persist({ ...saved, pending })) return;
        operationKey = pending.idempotency_key;
        next = await request(`${base}/speech-tasks`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(pending) });
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
          persist({ ...saved, pending: undefined });
        }
        setMessage(error instanceof Error ? error.message : '请求未获确认，请核对原任务。');
      }
    }
    finally { working.current = false; if (mounted.current) setBusy(false); }
  }
  return <details className="director-operation-panel" aria-label="生成声音">
    <summary>生成声音 {task && `· ${labels[task.status] || task.status}`}</summary>
    <div className="director-creation-grid">
      <div className="director-creation-wide"><label htmlFor={textId}>台词</label><textarea id={textId} rows={3} maxLength={2000} value={saved.text} disabled={Boolean(saved.pending)} placeholder="输入这段作品需要说的台词" onChange={event => persist({ ...saved, text: event.target.value })} /></div>
      <div className="director-creation-wide"><label htmlFor={modeId}>声音来源</label><select id={modeId} value={saved.mode} disabled={Boolean(saved.pending)} onChange={event => persist({ ...saved, mode: event.target.value === 'design' ? 'design' : 'reference' })}><option value="reference">沿用参考声音</option><option value="design">设计新声音</option></select></div>
      {saved.mode === 'design'
        ? <div className="director-creation-wide"><label htmlFor={descriptionId}>音色描述</label><textarea id={descriptionId} rows={2} maxLength={2000} value={saved.voice_description} disabled={Boolean(saved.pending)} placeholder="例如：年长男性，声音低沉沙哑，语速舒缓、克制" onChange={event => persist({ ...saved, voice_description: event.target.value })} /></div>
        : <fieldset className="director-creation-wide" disabled={Boolean(saved.pending)}><MaterialSelect label="音色参考" kind="audio" value={saved.voice_reference} options={options} onChange={value => persist({ ...saved, voice_reference: value })} onBrowse={onBrowse} /></fieldset>}
      <div><label htmlFor={durationId}>请求时长（秒）</label><input id={durationId} type="number" min={0} max={30} step="any" value={saved.pending ? saved.pending.gen_seconds ?? '' : saved.gen_seconds} placeholder="沿用原提交参数" disabled={Boolean(saved.pending)} onChange={event => persist({ ...saved, gen_seconds: Number(event.target.value) })} /></div>
      <details><summary>高级：固定种子</summary><label htmlFor={seedId}>种子</label><input id={seedId} type="number" min={0} max={4294967295} step={1} value={saved.pending ? saved.pending.seed ?? '' : saved.seed} placeholder="沿用原提交参数" disabled={Boolean(saved.pending)} onChange={event => persist({ ...saved, seed: Number(event.target.value) })} /></details>
    </div>
    <p className="director-muted">{saved.mode === 'design' ? '描述希望的音色，无需参考音频；满意的候选可用作后续台词的参考。' : '参考音频与请求时长合计须不超过 30 秒。'}请求时长不保证台词完整。</p>
    <p className="director-muted">生成后先试听，再在分镜中选择。现有分镜音频会保留。</p>
    {!capability?.available && <p role="status">{capability ? capability.reason || '声音生成环境尚未就绪。' : '正在检查声音环境…'}</p>}
    {message && <p role="status">{message}</p>}
    {task?.error && <p role="status">{task.error}</p>}
    <div className="director-form-actions">
      {!saved.pending && <button className="button primary compact" disabled={busy || !capability?.available || !saved.text.trim() || (saved.mode === 'design' ? !saved.voice_description.trim() : !saved.voice_reference) || !Number.isFinite(saved.gen_seconds) || saved.gen_seconds <= 0 || saved.gen_seconds > 30 || !Number.isInteger(saved.seed) || saved.seed < 0 || saved.seed > 4294967295} onClick={() => void act('submit')}>生成声音候选</button>}
      {saved.pending && !task && <><button className="button secondary compact" disabled={busy} onClick={() => void act('check')}>核对声音提交</button><button className="button secondary compact" disabled={busy || !capability?.available} onClick={() => void act('submit')}>使用原凭据重试</button></>}
      {task && !terminal.includes(task.status) && task.status !== 'needs_reconcile' && <button className="button secondary compact" disabled={busy} onClick={() => void act('stop')}>停止声音任务</button>}
      {task?.status === 'needs_reconcile' && <button className="button secondary compact" disabled={busy} onClick={() => void act('reconcile')}>核对声音结果</button>}
      {task && terminal.includes(task.status) && <button className="button secondary compact" disabled={busy} onClick={() => { if (persist({ ...saved, pending: undefined })) setTask(null); }}>准备下一条台词</button>}
      {task && <TaskQueueStatus task={{ ...task, asset_id: '声音' }} />}
      {task && <span>{labels[task.status] || task.status}</span>}
    </div>
  </details>;
}
