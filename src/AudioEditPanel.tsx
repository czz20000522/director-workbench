import { useEffect, useId, useRef, useState } from 'react';
import TaskQueueStatus, { type QueueReceipt } from './TaskQueueStatus';

type Props = { projectId: string; audioAssets: Array<{ id: string; title: string }>; onComplete: () => void; onBrowse: () => void };
type Draft = { source_asset_id: string; background_asset_id?: string; start_seconds: number; duration_seconds: number | null; pad_silence: boolean;
  gain: number; fade_in_seconds: number; fade_out_seconds: number; background_gain: number; background_offset_seconds: number };
type Submission = Draft & { idempotency_key: string };
type Saved = { draft: Draft; pending?: Submission; completedTaskId?: string };
type Task = { id: string; status: string; queue?: QueueReceipt['queue']; error?: string | null; result?: unknown };
const initial: Draft = { source_asset_id: '', start_seconds: 0, duration_seconds: null, pad_silence: false, gain: 1, fade_in_seconds: 0,
  fade_out_seconds: 0, background_gain: .25, background_offset_seconds: 0 };
const terminal = ['succeeded', 'failed', 'stopped'];
const labels: Record<string, string> = { scheduler_waiting: '已接受 · 等待资源', batch_waiting: '已接受 · 等待依赖', submitting: '正在处理', queued: '等待处理', running: '正在处理', succeeded: '已完成 · 待试听',
  failed: '处理失败', stopped: '已停止', needs_reconcile: '需要核对结果', stop_requested: '等待处理结束', stopping: '等待处理结束' };
function valid(d: Draft): boolean {
  const finite = (v: unknown, max: number) => typeof v === 'number' && Number.isFinite(v) && v >= 0 && v <= max;
  return typeof d?.source_asset_id === 'string' && Boolean(d.source_asset_id)
    && (d.background_asset_id === undefined || typeof d.background_asset_id === 'string')
    && (d.pad_silence === undefined || typeof d.pad_silence === 'boolean') && finite(d.start_seconds, 86400) && (d.duration_seconds === null || (finite(d.duration_seconds, 120) && d.duration_seconds > 0))
    && finite(d.gain, 8) && finite(d.background_gain, 8) && finite(d.fade_in_seconds, 120) && finite(d.fade_out_seconds, 120)
    && finite(d.background_offset_seconds, 120) && (Boolean(d.background_asset_id) || d.background_offset_seconds === 0)
    && (d.duration_seconds === null || (d.fade_in_seconds + d.fade_out_seconds <= d.duration_seconds && d.background_offset_seconds < d.duration_seconds));
}
class RequestError extends Error { constructor(message: string, readonly status: number) { super(message); } }

// Keyed inner state prevents a pending request from following a project switch.
export default function AudioEditPanel(props: Props) { return <ProjectAudioEditPanel key={props.projectId} {...props} />; }

function ProjectAudioEditPanel({ projectId, audioAssets, onComplete, onBrowse }: Props) {
  const storageKey = `director-workbench:${projectId}:audio-edit:v1`;
  const prefix = useId();
  const [saved, setSaved] = useState<Saved>(() => {
    try {
      const value = JSON.parse(localStorage.getItem(storageKey) || '{}');
      const draft = value.draft && typeof value.draft === 'object' ? { ...initial, ...value.draft } : { ...initial };
      const pending = value.pending && typeof value.pending.idempotency_key === 'string' ? value.pending : undefined;
      return { draft, pending, completedTaskId: typeof value.completedTaskId === 'string' ? value.completedTaskId : undefined };
    } catch { return { draft: { ...initial } }; }
  });
  const current = useRef(saved);
  const alive = useRef(true);
  const working = useRef(false);
  const completion = useRef(onComplete);
  completion.current = onComplete;
  const [task, setTask] = useState<Task | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const base = `/api/projects/${encodeURIComponent(projectId)}`;
  const pendingKey = saved.pending?.idempotency_key;
  const draft = saved.pending || saved.draft;
  const locked = Boolean(saved.pending);
  const result = task?.status === 'succeeded' && task.result && typeof task.result === 'object' ? task.result as { output?: string; duration_seconds?: number; report?: { peak?: number; clipped_samples?: number } } : null;
  function persist(next: Saved) {
    if (!alive.current) return false;
    try { localStorage.setItem(storageKey, JSON.stringify(next)); current.current = next; setSaved(next); return true; }
    catch { setMessage('无法保存提交凭据，请恢复浏览器存储后重试。'); return false; }
  }
  function accept(next: Task, key: string) {
    if (!alive.current || current.current.pending?.idempotency_key !== key) return;
    setTask(next); setMessage('');
    if (next.status === 'succeeded' && current.current.completedTaskId !== next.id) {
      if (persist({ ...current.current, completedTaskId: next.id })) completion.current();
    }
  }
  async function request(url: string, init?: RequestInit): Promise<Task> {
    const response = await fetch(url, init);
    const data = await response.json();
    if (!response.ok) throw new RequestError(typeof data.detail === 'string' ? data.detail : data.detail?.message || '请求未获确认，请核对原任务。', response.status);
    if (typeof data.id !== 'string' || typeof data.status !== 'string') throw new Error('任务回执不完整，请核对原提交。');
    return data;
  }
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  useEffect(() => {
    if (!pendingKey) return;
    let active = true; let polling = false;
    async function poll() {
      if (polling) return;
      polling = true;
      try {
        const result = await request(task ? `${base}/tasks/${encodeURIComponent(task.id)}` : `${base}/submission-receipt?key=${encodeURIComponent(pendingKey!)}`);
        if (active) accept(result, pendingKey!);
      } catch (error) {
        if (active && !working.current && current.current.pending?.idempotency_key === pendingKey) setMessage(error instanceof Error ? error.message : '暂时无法核对任务');
      } finally { polling = false; }
    }
    void poll();
    const timer = window.setInterval(() => { if (!task || !terminal.includes(task.status)) void poll(); }, 3000);
    return () => { active = false; window.clearInterval(timer); };
  }, [pendingKey, task?.id, task?.status, base]);
  async function act(action: 'submit' | 'check' | 'reconcile' | 'stop' | 'resume') {
    if (working.current) return;
    working.current = true; setBusy(true);
    let key = current.current.pending?.idempotency_key;
    try {
      let next: Task;
      if (action === 'submit') {
        const submission = current.current.pending || { ...current.current.draft, idempotency_key: `audio-edit-${crypto.randomUUID()}` };
        if (!valid(submission)) throw new Error('请检查素材和编辑参数；已有提交请先核对。');
        if (!persist({ ...current.current, pending: submission })) return;
        key = submission.idempotency_key;
        next = await request(`${base}/audio-edit-tasks`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(submission) });
      } else if (action === 'check') {
        next = await request(`${base}/submission-receipt?key=${encodeURIComponent(key!)}`);
      } else {
        next = await request(`${base}/tasks/${encodeURIComponent(task!.id)}/${action}`, { method: 'POST' });
      }
      accept(next, key!);
    } catch (error) {
      if (alive.current && (!key || current.current.pending?.idempotency_key === key)) {
        if (action === 'submit' && error instanceof RequestError && error.status === 422) persist({ ...current.current, pending: undefined });
        setMessage(error instanceof Error ? error.message : '请求未获确认，请核对原任务。');
      }
    } finally { working.current = false; if (alive.current) setBusy(false); }
  }
  function change<K extends keyof Draft>(name: K, value: Draft[K]) { persist({ ...current.current, draft: { ...current.current.draft, [name]: value } }); }
  const numberInput = (name: Exclude<keyof Draft, 'source_asset_id' | 'background_asset_id' | 'pad_silence'>, label: string, max: number) =>
    <div key={name}><label htmlFor={`${prefix}-${name}`}>{label}</label><input id={`${prefix}-${name}`} type="number" min={0} max={max} step="any" disabled={locked} value={draft[name] ?? ''}
      placeholder={name === 'duration_seconds' ? '留空：到结尾' : undefined}
      onChange={event => change(name, event.target.value === '' && name === 'duration_seconds' ? null : Number(event.target.value))} /></div>;
  return <details className="director-operation-panel" aria-label="编辑音频">
    <summary>编辑音频 {task && `· ${labels[task.status] || '处理中'}`}</summary>
    <p>最长120秒；PCM16 WAV或MP3，MP3转为48kHz双声道。增益和淡变产生新候选，不自动替换母版或采用。短源默认拒绝，可明确补静音；长源用取片时长裁切。完整母版应包含需要保留的对白和音效。</p>
    <div className="director-creation-grid">
      <div><label htmlFor={`${prefix}-source`}>原音频</label><select id={`${prefix}-source`} value={draft.source_asset_id} disabled={locked} onChange={event => change('source_asset_id', event.target.value)}>
        <option value="">选择音频</option>{draft.source_asset_id && !audioAssets.some(a => a.id === draft.source_asset_id) && <option value={draft.source_asset_id}>原提交素材（当前不可选）</option>}
        {audioAssets.map(asset => <option key={asset.id} value={asset.id}>{asset.title}</option>)}</select></div>
      <div><label htmlFor={`${prefix}-background`}>背景音（可选）</label><select id={`${prefix}-background`} value={draft.background_asset_id || ''} disabled={locked} onChange={event => persist({ ...current.current, draft: { ...current.current.draft, background_asset_id: event.target.value || undefined, background_offset_seconds: 0 } })}>
        <option value="">不混入背景音</option>{draft.background_asset_id && !audioAssets.some(a => a.id === draft.background_asset_id) && <option value={draft.background_asset_id}>原提交背景音（当前不可选）</option>}
        {audioAssets.map(asset => <option key={asset.id} value={asset.id}>{asset.title}</option>)}</select></div>
      <label><input type="checkbox" checked={draft.pad_silence ?? false} disabled={locked} onChange={event => change('pad_silence', event.target.checked)} />源不足所选时长时，明确在尾部补静音</label>
      <details className="director-creation-wide"><summary>高级：裁切、音量与淡变</summary><div className="director-creation-grid">
        {numberInput('start_seconds', '起点（秒）', 86400)}{numberInput('duration_seconds', '保留时长（秒）', 120)}
        {numberInput('gain', '原音频音量（倍）', 8)}{numberInput('fade_in_seconds', '淡入（秒）', 120)}{numberInput('fade_out_seconds', '淡出（秒）', 120)}
        {draft.background_asset_id && <>{numberInput('background_gain', '背景音音量（倍）', 8)}{numberInput('background_offset_seconds', '背景音开始位置（秒）', 120)}</>}
      </div></details>
    </div>
    <p className="director-muted">PCM16 WAV，最多 120 秒；混音须同采样率、同声道。保存为新候选，原音频保留。</p>
    {message && <p role="status">{message}</p>}{task?.error && <p role="status">{task.error}</p>}
    {result?.output && <div><audio controls preload="metadata" aria-label="音频编辑候选试听" src={`/media-file?path=${encodeURIComponent(result.output)}`} /><p>新候选实测 {result.duration_seconds ?? '未知'} 秒；试听后再选择为完整母版，不会自动替换原音。</p></div>}
    <div className="director-form-actions">
      <button className="button secondary compact" onClick={onBrowse}>添加素材</button>
      {!locked && <button className="button primary compact" disabled={busy || !valid(draft) || !audioAssets.some(a => a.id === draft.source_asset_id)} onClick={() => void act('submit')}>保存编辑候选</button>}
      {locked && !task && <><button className="button secondary compact" disabled={busy} onClick={() => void act('check')}>核对原提交</button><button className="button secondary compact" disabled={busy} onClick={() => void act('submit')}>使用原凭据重试</button></>}
      {task?.status === 'needs_reconcile' && <button className="button secondary compact" disabled={busy} onClick={() => void act('reconcile')}>核对编辑结果</button>}
      {task && !terminal.includes(task.status) && task.status !== 'needs_reconcile' && <button className="button secondary compact" disabled={busy} onClick={() => void act('stop')}>停止音频编辑</button>}
      {task && ['stopped', 'failed'].includes(task.status) && <button className="button secondary compact" disabled={busy} onClick={() => void act('resume')}>恢复原编辑任务</button>}
      {task && terminal.includes(task.status) && <button className="button secondary compact" disabled={busy} onClick={() => { if (persist({ ...current.current, pending: undefined })) { setTask(null); setMessage(''); } }}>继续编辑</button>}
      {task && <TaskQueueStatus task={{ ...task, asset_id: '音频编辑' }} />}
      {task && <span role="status">{labels[task.status] || '处理中'}</span>}
    </div>
  </details>;
}
