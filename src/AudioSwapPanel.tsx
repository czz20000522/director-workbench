import { useEffect, useId, useRef, useState } from 'react';
import TaskQueueStatus, { type QueueReceipt } from './TaskQueueStatus';
import { ApiError, mediaUrl, request as apiRequest } from './api/client';

// 成片后只改音频：画面流直接复制，不重新渲染。原声可保留调音量，也可静音；
// 还能把项目里的配乐/配音音轨叠上去。和锚点配音的区别是不需要台词。
type VideoAsset = { id: string; title: string };
type AudioAsset = { id: string; title: string };
type Draft = { source_asset_id: string; overlay_asset_id: string; original_audio: 'keep' | 'mute';
  original_gain: number; overlay_gain: number; start_seconds: number; overlay_loop: boolean;
  fade_in_seconds: number; fade_out_seconds: number };
type Submission = Omit<Draft, 'overlay_asset_id'> & { overlay_asset_id: string | null; idempotency_key: string };
type Saved = { draft: Draft; pending?: Submission; completedTaskId?: string };
type Task = { id: string; status: string; queue?: QueueReceipt['queue']; error?: string | null; result?: unknown };
type Capability = { available: boolean; missing?: string[]; reason?: string; note?: string };
type Receipt = { output?: string; audio_output?: string; duration_seconds?: number; audio_duration_seconds?: number };

const terminal = ['succeeded', 'failed', 'stopped'];
const pollTerminal = [...terminal, 'needs_reconcile'];
const labels: Record<string, string> = { scheduler_waiting: '已接受 · 等待资源', batch_waiting: '已接受 · 等待依赖',
  submitting: '正在处理', succeeded: '已换好 · 待检查', failed: '处理失败', stopped: '已停止',
  needs_reconcile: '需要核对结果', stop_requested: '正在停止', stopping: '正在停止' };
const initial: Draft = { source_asset_id: '', overlay_asset_id: '', original_audio: 'mute', original_gain: 1,
  overlay_gain: 1, start_seconds: 0, overlay_loop: true, fade_in_seconds: 0, fade_out_seconds: 0 };

function load(storageKey: string): Saved {
  try {
    const value = JSON.parse(localStorage.getItem(storageKey) || '{}');
    const raw = value.draft && typeof value.draft === 'object' ? value.draft : {};
    const num = (input: unknown, low: number, high: number, fallback: number) =>
      typeof input === 'number' && Number.isFinite(input) && input >= low && input <= high ? input : fallback;
    return {
      draft: { source_asset_id: typeof raw.source_asset_id === 'string' ? raw.source_asset_id : '',
        overlay_asset_id: typeof raw.overlay_asset_id === 'string' ? raw.overlay_asset_id : '',
        original_audio: raw.original_audio === 'keep' ? 'keep' : 'mute',
        original_gain: num(raw.original_gain, 0, 3, 1), overlay_gain: num(raw.overlay_gain, 0, 3, 1),
        start_seconds: num(raw.start_seconds, 0, 86400, 0), overlay_loop: raw.overlay_loop !== false,
        fade_in_seconds: num(raw.fade_in_seconds, 0, 10, 0), fade_out_seconds: num(raw.fade_out_seconds, 0, 10, 0) },
      pending: value.pending && typeof value.pending.idempotency_key === 'string' ? value.pending : undefined,
      completedTaskId: typeof value.completedTaskId === 'string' ? value.completedTaskId : undefined,
    };
  } catch { return { draft: { ...initial } }; }
}
function valid(draft: Draft): boolean {
  if (!draft.source_asset_id) return false;
  const gain = (value: number) => Number.isFinite(value) && value >= 0 && value <= 3;
  return gain(draft.original_gain) && gain(draft.overlay_gain)
    && Number.isFinite(draft.start_seconds) && draft.start_seconds >= 0
    && Number.isFinite(draft.fade_in_seconds) && draft.fade_in_seconds >= 0 && draft.fade_in_seconds <= 10
    && Number.isFinite(draft.fade_out_seconds) && draft.fade_out_seconds >= 0 && draft.fade_out_seconds <= 10;
}
class RequestError extends Error { constructor(message: string, readonly status: number) { super(message); } }

export default function AudioSwapPanel({ projectId, videos, audioAssets, onComplete }: {
  projectId: string; videos: VideoAsset[]; audioAssets: AudioAsset[]; onComplete: () => void;
}) {
  const storageKey = `director-workbench:${projectId}:audio-swap:v1`;
  const prefix = useId();
  const [saved, setSaved] = useState<Saved>(() => load(storageKey));
  const current = useRef(saved);
  const alive = useRef(true);
  const working = useRef(false);
  const completion = useRef(onComplete);
  completion.current = onComplete;
  const [task, setTask] = useState<Task | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const [capability, setCapability] = useState<Capability | null>(null);
  const base = `/api/projects/${encodeURIComponent(projectId)}`;
  const pendingKey = saved.pending?.idempotency_key;
  const draft = saved.draft;
  const locked = Boolean(saved.pending);
  const result = task?.status === 'succeeded' && task.result && typeof task.result === 'object' ? task.result as Receipt : null;
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
    try {
      return await apiRequest<Task>(url, { init });
    } catch (cause) {
      if (cause instanceof ApiError) throw new RequestError(cause.message, cause.status);
      throw cause;
    }
  }
  useEffect(() => {
    alive.current = true;
    fetch('/api/audio-swap-capability').then(response => {
      if (!response.ok) throw new Error('能力检查失败');
      return response.json();
    }).then(data => { if (alive.current) setCapability(data); })
      .catch(() => { if (alive.current) setCapability({ available: false, reason: '无法检查换音轨环境，请检查连接后刷新。' }); });
    return () => { alive.current = false; };
  }, []);
  useEffect(() => {
    if (!pendingKey) return;
    let active = true; let polling = false;
    async function poll() {
      if (polling) return;
      polling = true;
      try {
        const next = await request(task ? `${base}/tasks/${encodeURIComponent(task.id)}` : `${base}/submission-receipt?key=${encodeURIComponent(pendingKey!)}`);
        if (active) accept(next, pendingKey!);
      } catch (error) {
        if (active && !working.current && current.current.pending?.idempotency_key === pendingKey) setMessage(error instanceof Error ? error.message : '暂时无法核对任务');
      } finally { polling = false; }
    }
    void poll();
    const timer = window.setInterval(() => { if (!task || !pollTerminal.includes(task.status)) void poll(); }, 3000);
    return () => { active = false; window.clearInterval(timer); };
  }, [pendingKey, task?.id, task?.status, base]);
  async function act(action: 'submit' | 'check' | 'stop' | 'reconcile') {
    if (working.current) return;
    working.current = true; setBusy(true);
    let key = current.current.pending?.idempotency_key;
    try {
      let next: Task;
      if (action === 'submit') {
        const body = current.current.pending || { ...current.current.draft,
          overlay_asset_id: current.current.draft.overlay_asset_id || null,
          idempotency_key: `audio-swap-${crypto.randomUUID()}` };
        if (!valid(current.current.draft)) throw new Error('请选择视频，并确认音量、开始时间与淡入淡出在允许范围内。');
        if (!persist({ ...current.current, pending: body })) return;
        key = body.idempotency_key;
        next = await request(`${base}/audio-swap-tasks`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
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
  function change<K extends keyof Draft>(name: K, value: Draft[K]) {
    if (current.current.pending) return;
    persist({ ...current.current, draft: { ...current.current.draft, [name]: value } });
  }
  return <section className="sf-post-tools" aria-label="换音轨">
    <section className="sf-post-section" aria-label="换音轨设置">
      <div className="sf-post-section-head"><h2 className="sf-post-title">换音轨</h2>
        <p className="sf-post-status">{task ? (labels[task.status] || task.status) : '画面不动，只改声音'}</p></div>
      <p className="sf-post-hint">选一条已生成的视频，单独改它的音轨：原声可以保留并调音量，也可以静音；再把项目里的配乐或配音音轨叠上去。画面流直接复制，不重新渲染、不占显卡。</p>
      <div className="sf-post-fields">
        <div className="sf-post-field"><label htmlFor={`${prefix}-video`}>视频</label>
          <select id={`${prefix}-video`} value={draft.source_asset_id} disabled={locked} onChange={event => change('source_asset_id', event.target.value)}>
            <option value="">选择视频</option>
            {videos.map(video => <option key={video.id} value={video.id}>{video.title}</option>)}</select></div>
        <div className="sf-post-field"><label htmlFor={`${prefix}-original`}>原声</label>
          <select id={`${prefix}-original`} value={draft.original_audio} disabled={locked} onChange={event => change('original_audio', event.target.value === 'keep' ? 'keep' : 'mute')}>
            <option value="mute">静音，只用叠加的音频</option><option value="keep">保留原声并叠加</option></select></div>
        {draft.original_audio === 'keep' && <div className="sf-post-field"><label htmlFor={`${prefix}-ogain`}>原声音量（倍）</label>
          <input id={`${prefix}-ogain`} type="number" min={0} max={3} step="any" disabled={locked} value={draft.original_gain} onChange={event => change('original_gain', Number(event.target.value))} /></div>}
        <div className="sf-post-field"><label htmlFor={`${prefix}-overlay`}>叠加音频</label>
          <select id={`${prefix}-overlay`} value={draft.overlay_asset_id} disabled={locked} onChange={event => change('overlay_asset_id', event.target.value)}>
            <option value="">不叠加（只保留原声或纯静音）</option>
            {audioAssets.map(asset => <option key={asset.id} value={asset.id}>{asset.title || asset.id}</option>)}</select></div>
        {draft.overlay_asset_id && <>
          <div className="sf-post-field"><label htmlFor={`${prefix}-bgain`}>叠加音量（倍）</label>
            <input id={`${prefix}-bgain`} type="number" min={0} max={3} step="any" disabled={locked} value={draft.overlay_gain} onChange={event => change('overlay_gain', Number(event.target.value))} /></div>
          <div className="sf-post-field"><label htmlFor={`${prefix}-start`}>从第几秒开始放</label>
            <input id={`${prefix}-start`} type="number" min={0} step="any" disabled={locked} value={draft.start_seconds} onChange={event => change('start_seconds', Number(event.target.value))} /></div>
          <div className="sf-post-field"><label htmlFor={`${prefix}-loop`}>音频比视频短时</label>
            <select id={`${prefix}-loop`} value={draft.overlay_loop ? 'loop' : 'once'} disabled={locked} onChange={event => change('overlay_loop', event.target.value === 'loop')}>
              <option value="loop">循环补满到片尾</option><option value="once">放一遍，到哪算哪</option></select></div>
        </>}
      </div>
      <details className="sf-post-subblock"><summary className="sf-post-subtitle">更多设置 · 淡入淡出</summary>
        <div className="sf-post-fields">
          <div className="sf-post-field"><label htmlFor={`${prefix}-fadein`}>叠加音频淡入（秒）</label>
            <input id={`${prefix}-fadein`} type="number" min={0} max={10} step="any" disabled={locked} value={draft.fade_in_seconds} onChange={event => change('fade_in_seconds', Number(event.target.value))} /></div>
          <div className="sf-post-field"><label htmlFor={`${prefix}-fadeout`}>整条音轨淡出（秒）</label>
            <input id={`${prefix}-fadeout`} type="number" min={0} max={10} step="any" disabled={locked} value={draft.fade_out_seconds} onChange={event => change('fade_out_seconds', Number(event.target.value))} /></div>
        </div>
      </details>
      <p className="sf-post-hint">输出两个候选：换过音轨的视频（画面原样复制）和一条完整音轨。原来的视频不会被覆盖，改坏了重来就行。</p>
      {capability && !capability.available && <p className="sf-post-message" role="status" aria-live="polite">换音轨未就绪：{(capability.missing || []).join('、') || capability.reason || '缺少运行环境。'}</p>}
      {message && <p className="sf-post-message" role="status" aria-live="polite">{message}</p>}
      {task?.error && <p className="sf-post-message" role="status" aria-live="polite">{task.error}</p>}
      {result?.output && <div className="sf-post-subblock">
        <video controls preload="metadata" aria-label="换音轨候选视频" src={mediaUrl(result.output)} />
        {result.audio_output && <audio controls preload="metadata" aria-label="换音轨完整音轨" src={mediaUrl(result.audio_output)} />}
        <p className="sf-post-hint">{result.duration_seconds?.toFixed(2) ?? '未知'} 秒 · 画面未重新渲染。试听确认后再在分镜或装配里选用。</p>
      </div>}
      <div className="sf-post-actions">
        {!locked && <button className="button primary compact" disabled={busy || !capability?.available || !valid(draft)} onClick={() => void act('submit')}>生成换音轨候选</button>}
        {locked && !task && <><button className="button secondary compact" disabled={busy} onClick={() => void act('check')}>核对提交</button><button className="button secondary compact" disabled={busy || !capability?.available} onClick={() => void act('submit')}>使用原凭据重试</button></>}
        {task && !pollTerminal.includes(task.status) && <button className="button secondary compact" disabled={busy} onClick={() => void act('stop')}>停止任务</button>}
        {task?.status === 'needs_reconcile' && <button className="button secondary compact" disabled={busy} onClick={() => void act('reconcile')}>核对结果</button>}
        {task && terminal.includes(task.status) && <button className="button secondary compact" disabled={busy} onClick={() => { if (persist({ ...current.current, pending: undefined })) { setTask(null); setMessage(''); } }}>再换一版音轨</button>}
        {task && <TaskQueueStatus task={{ ...task, asset_id: '换音轨' }} />}
      </div>
    </section>
  </section>;
}
