import { useEffect, useId, useRef, useState } from 'react';
import TaskQueueStatus, { type QueueReceipt } from './TaskQueueStatus';

type Operation = 'silence' | 'video_qc' | 'transcribe' | 'silent_video';
type Props = { projectId: string; assets: Array<{ id: string; title: string; kind: string }>; onComplete: () => void; onBrowse: () => void };
type Draft = { operation: Operation; source_asset_id: string; duration_seconds: number; sample_rate: number; channels: number; sample_count: number; expected_text: string; language: string; word_timestamps: boolean };
type Submission = Record<string, unknown> & { idempotency_key: string };
type Saved = { draft: Draft; pending?: Submission; completedTaskId?: string };
type Task = { id: string; status: string; queue?: QueueReceipt['queue']; error?: string | null; result?: { outputs?: Array<{ kind: string; path: string; title?: string }>; report?: Record<string, unknown> } };
const initial: Draft = { operation: 'silence', source_asset_id: '', duration_seconds: 5, sample_rate: 24000, channels: 1, sample_count: 12, expected_text: '', language: 'zh', word_timestamps: true };
const terminal = ['succeeded', 'failed', 'stopped'];
const labels: Record<string, string> = { scheduler_waiting: '已接受 · 等待资源', batch_waiting: '已接受 · 等待依赖', preparing: '准备中', submitting: '正在处理', queued: '等待处理', running: '正在处理', succeeded: '已完成 · 待检查', failed: '处理失败', stopped: '已停止', needs_reconcile: '需要核对结果', stop_requested: '等待处理结束', stopping: '等待处理结束' };
const operations: Record<Operation, string> = { silence: '生成静音', video_qc: '视频技术检查与抽帧', transcribe: '转写与台词核对', silent_video: '导出无声审片版' };
function payload(d: Draft): Record<string, unknown> {
  if (d.operation === 'silence') return { operation: d.operation, duration_seconds: d.duration_seconds, sample_rate: d.sample_rate, channels: d.channels };
  const common = { operation: d.operation, source_asset_id: d.source_asset_id };
  if (d.operation === 'video_qc') return { ...common, sample_count: d.sample_count };
  if (d.operation === 'transcribe') return { ...common, expected_text: d.expected_text, language: d.language, word_timestamps: d.word_timestamps };
  return common;
}
function valid(d: Draft): boolean {
  if (d.operation === 'silence') return Number.isFinite(d.duration_seconds) && d.duration_seconds > 0 && d.duration_seconds <= 120;
  if (!d.source_asset_id) return false;
  if (d.operation === 'video_qc') return Number.isInteger(d.sample_count) && d.sample_count >= 3 && d.sample_count <= 60;
  return d.operation !== 'transcribe' || d.expected_text.length <= 2000;
}
class RequestError extends Error { constructor(message: string, readonly status: number) { super(message); } }

// Keyed inner state prevents a pending request from following a project switch.
export default function MediaToolsPanel(props: Props) { return <ProjectMediaToolsPanel key={props.projectId} {...props} />; }

function ProjectMediaToolsPanel({ projectId, assets, onComplete, onBrowse }: Props) {
  const storageKey = `director-workbench:${projectId}:media-tools:v1`;
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
  const draft = saved.draft;
  const [capability, setCapability] = useState<Record<string, { available: boolean; missing?: unknown[] }> | null>(null);
  useEffect(() => { let active = true; void fetch('/api/media-capability').then(r => { if (!r.ok) throw new Error(); return r.json(); }).then(data => { if (active) setCapability(data.operations); }).catch(() => { if (active) setMessage('媒体能力状态读取失败，请刷新页面。'); }); return () => { active = false; }; }, []);
  const locked = Boolean(saved.pending);
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
        const submission = current.current.pending || { ...payload(current.current.draft), idempotency_key: `media-${crypto.randomUUID()}` };
        if (!current.current.pending && !valid(current.current.draft)) throw new Error('请检查素材和编辑参数；已有提交请先核对。');
        if (!persist({ ...current.current, pending: submission })) return;
        key = submission.idempotency_key;
        next = await request(`${base}/media-tasks`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(submission) });
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
  const eligible = assets.filter(a => draft.operation === 'transcribe' ? ['audio', 'video'].includes(a.kind) : a.kind === 'video');
  const available = capability?.[draft.operation]?.available === true;
  const outputs = task?.result?.outputs ?? [];
  const report = task?.result?.report;
  const segments = Array.isArray(report?.segments) ? report.segments as Array<{ start?: number; end?: number; text?: string }> : [];
  const words = Array.isArray(report?.words) ? report.words as Array<{ start?: number; end?: number; word?: string }> : [];
  const mediaUrl = (path: string) => `/media-file?path=${encodeURIComponent(path)}`;
  return <details className="director-operation-panel" aria-label="媒体工具">
    <summary>媒体工具 {task && `· ${labels[task.status] || task.status}`}</summary>
    <div className="director-creation-grid">
      <label htmlFor={`${prefix}-operation`}>操作<select id={`${prefix}-operation`} value={draft.operation} disabled={locked} onChange={e => change('operation', e.target.value as Operation)}>{Object.entries(operations).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>
      {draft.operation !== 'silence' && <label htmlFor={`${prefix}-source`}>来源素材<select id={`${prefix}-source`} value={draft.source_asset_id} disabled={locked} onChange={e => change('source_asset_id', e.target.value)}><option value="">选择当前作品素材</option>{draft.source_asset_id && !eligible.some(a => a.id === draft.source_asset_id) && <option value={draft.source_asset_id}>原提交素材（当前不可选）</option>}{eligible.map(a => <option key={a.id} value={a.id}>{a.title}</option>)}</select></label>}
      {draft.operation === 'silence' && <>
        <label>时长（秒）<input type="number" min={0.01} max={120} step="any" value={draft.duration_seconds} disabled={locked} onChange={e => change('duration_seconds', Number(e.target.value))} /></label>
        <label>采样率<select value={draft.sample_rate} disabled={locked} onChange={e => change('sample_rate', Number(e.target.value))}>{[16000, 24000, 48000].map(n => <option key={n} value={n}>{n} Hz</option>)}</select></label>
        <label>声道<select value={draft.channels} disabled={locked} onChange={e => change('channels', Number(e.target.value))}><option value={1}>单声道</option><option value={2}>双声道</option></select></label>
      </>}
      {draft.operation === 'video_qc' && <label>抽帧数量<input type="number" min={3} max={60} step={1} value={draft.sample_count} disabled={locked} onChange={e => change('sample_count', Number(e.target.value))} /></label>}
      {draft.operation === 'transcribe' && <>
        <label>预期台词（可选）<textarea maxLength={2000} value={draft.expected_text} disabled={locked} onChange={e => change('expected_text', e.target.value)} /></label>
        <label>语言<select value={draft.language} disabled={locked} onChange={e => change('language', e.target.value)}><option value="zh">中文</option><option value="en">英语</option><option value="auto">自动识别</option></select></label>
        <label><input type="checkbox" checked={draft.word_timestamps} disabled={locked} onChange={e => change('word_timestamps', e.target.checked)} />逐词时间戳</label>
      </>}
    </div>
    <p className="director-muted">检查报告与转写帮助定位问题，画面、表演和音色仍由导演判断。所有结果保存为本作品独立结果；质检图仅用于诊断。</p>
    {!available && <p role="status">{capability ? '当前操作不可用：' + (capability[draft.operation]?.missing?.join('、') || '请检查资源与连接') : '正在读取媒体能力…'}</p>}
    {message && <p role="status">{message}</p>}{task?.error && <p role="status">{task.error}</p>}
    <div className="director-form-actions">
      <button className="button secondary compact" onClick={onBrowse}>添加素材</button>
      {!locked && <button className="button primary compact" disabled={busy || !available || !valid(draft) || (draft.operation !== 'silence' && !eligible.some(a => a.id === draft.source_asset_id))} onClick={() => void act('submit')}>开始处理</button>}
      {locked && <button className="button secondary compact" disabled={busy} onClick={() => void act('check')}>核对原提交</button>}
      {locked && !task && <button className="button secondary compact" disabled={busy} onClick={() => void act('submit')}>使用原凭据重试</button>}
      {task?.status === 'needs_reconcile' && <button className="button secondary compact" disabled={busy} onClick={() => void act('reconcile')}>核对处理结果</button>}
      {task && ['scheduler_waiting', 'batch_waiting', 'preparing', 'submitting', 'queued', 'running'].includes(task.status) && <button className="button secondary compact" disabled={busy} onClick={() => void act('stop')}>停止处理</button>}
      {task && ['stopped', 'failed'].includes(task.status) && <button className="button secondary compact" disabled={busy} onClick={() => void act('resume')}>恢复原任务</button>}
      {task && terminal.includes(task.status) && <button className="button secondary compact" disabled={busy} onClick={() => { if (persist({ ...current.current, pending: undefined })) { setTask(null); setMessage(''); } }}>继续处理</button>}
      {task && <TaskQueueStatus task={{ ...task, asset_id: '媒体处理' }} />}
      {task && <span role="status">{labels[task.status] || task.status}</span>}
    </div>
    {report && <section aria-label="媒体检查报告"><h3>处理报告</h3>
      {typeof report.duration_seconds === 'number' && <p>时长 {report.duration_seconds.toFixed(2)} 秒{typeof report.width === 'number' ? ` · ${report.width} × ${report.height}` : ''}{typeof report.frame_count === 'number' ? ` · ${report.frame_count} 帧` : ''}{typeof report.sample_rate === 'number' ? ` · ${report.sample_rate} Hz` : ''}{typeof report.channels === 'number' ? ` · ${report.channels} 声道` : ''}</p>}
      {typeof report.text === 'string' && <><h4>识别台词</h4><p>{report.text || '未识别到台词'}</p></>}
      {typeof report.expected_text === 'string' && report.expected_text && <><h4>预期台词</h4><p>{report.expected_text}</p>{typeof report.text_similarity === 'number' && <p>文字相似度 {(report.text_similarity * 100).toFixed(1)}% · 请对照试听，识别可能出错</p>}</>}
      {segments.length > 0 && <details><summary>分段时间戳</summary><ul>{segments.map((s, i) => <li key={i}>{s.start?.toFixed(2)}–{s.end?.toFixed(2)} 秒：{s.text}</li>)}</ul></details>}
      {words.length > 0 && <details><summary>逐词时间戳</summary><ul>{words.map((w, i) => <li key={i}>{w.start?.toFixed(2)}–{w.end?.toFixed(2)} 秒：{w.word}</li>)}</ul></details>}
      {typeof report.note === 'string' && <p className="director-muted">{report.note}</p>}
      <details><summary>完整技术数据</summary><pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{JSON.stringify(report, null, 2)}</pre></details>
    </section>}
    {outputs.map((output, index) => <figure key={`${output.path}-${index}`}><figcaption>{output.title || output.kind}</figcaption>{output.kind === 'image' ? <img src={mediaUrl(output.path)} alt={output.title || '视频抽帧'} style={{ maxWidth: '100%' }} /> : output.kind === 'video' ? <video src={mediaUrl(output.path)} controls preload="metadata" style={{ maxWidth: '100%' }} /> : output.kind === 'audio' ? <audio src={mediaUrl(output.path)} controls preload="metadata" /> : <a href={mediaUrl(output.path)} target="_blank" rel="noreferrer">查看报告文件</a>}</figure>)}
  </details>;
}
