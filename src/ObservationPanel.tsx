import { useEffect, useId, useRef, useState } from 'react';
import TaskQueueStatus, { type QueueReceipt } from './TaskQueueStatus';
import MaterialSelect, { type MaterialOption } from './MaterialSelect';
import { ApiError, mediaUrl, request as apiRequest } from './api/client';

type SourceKind = 'image' | 'video';
type Provider = 'local-ollama' | 'deepseek-vision';
type ProviderState = { available: boolean; label?: string; model?: string; service?: string; missing?: string[]; reason?: string; note?: string };
type Draft = { source_kind: SourceKind; provider: Provider; image_source: string; video_source: string; frame_seconds: number;
  prompt: string; temperature: number; num_predict: number };
type Submission = { source: string; provider: Provider; frame_seconds: number | null; prompt: string; temperature: number;
  num_predict: number; idempotency_key: string };
type Saved = { draft: Draft; pending?: Submission; completedTaskId?: string };
type Task = { id: string; status: string; queue?: QueueReceipt['queue']; error?: string | null; result?: unknown };
type Capability = { available: boolean; missing?: string[]; model?: string; service?: string; reason?: string; note?: string;
  default_provider?: Provider; providers?: Record<Provider, ProviderState> };
type Receipt = { observation?: string; observation_frame?: string; prompt?: string; model?: string; provider?: Provider;
  eval_count?: number; done_reason?: string; frame_seconds?: number | null; source_kind?: SourceKind };

const terminal = ['succeeded', 'failed', 'stopped'];
const pollTerminal = [...terminal, 'needs_reconcile'];
const labels: Record<string, string> = { scheduler_waiting: '已接受 · 等待资源', batch_waiting: '已接受 · 等待依赖',
  submitting: '正在观察', running: '正在观察', succeeded: '已生成观察 · 待人工核对', failed: '观察失败', stopped: '已停止',
  needs_reconcile: '需要核对结果', stop_requested: '正在停止', stopping: '正在停止' };
const initial: Draft = { source_kind: 'image', provider: 'local-ollama', image_source: '', video_source: '', frame_seconds: 0,
  prompt: '', temperature: 0.8, num_predict: 512 };

function finite(value: unknown, low: number, high: number): boolean {
  return typeof value === 'number' && Number.isFinite(value) && value >= low && value <= high;
}
function load(storageKey: string): Saved {
  try {
    const value = JSON.parse(localStorage.getItem(storageKey) || '{}');
    const draft = value.draft && typeof value.draft === 'object' ? { ...initial, ...value.draft } : { ...initial };
    if (draft.source_kind !== 'video') draft.source_kind = 'image';
    if (draft.provider !== 'deepseek-vision') draft.provider = 'local-ollama';
    const pending = value.pending && typeof value.pending.idempotency_key === 'string' ? value.pending : undefined;
    return { draft: { ...draft, source_kind: draft.source_kind === 'video' ? 'video' : 'image' }, pending,
      completedTaskId: typeof value.completedTaskId === 'string' ? value.completedTaskId : undefined };
  } catch { return { draft: { ...initial } }; }
}
function valid(draft: Draft): boolean {
  return submissionValid(bodyOf(draft));
}
function submissionValid(sub: Submission): boolean {
  return Boolean(sub.source) && Boolean(sub.prompt.trim()) && sub.prompt.length <= 2000
    && (sub.frame_seconds === null || finite(sub.frame_seconds, 0, 86400))
    && finite(sub.temperature, 0, 2)
    && Number.isInteger(sub.num_predict) && sub.num_predict >= 16 && sub.num_predict <= 2048;
}
function bodyOf(draft: Draft): Submission {
  return { source: (draft.source_kind === 'video' ? draft.video_source : draft.image_source).trim(),
    provider: draft.provider,
    frame_seconds: draft.source_kind === 'video' ? draft.frame_seconds : null,
    prompt: draft.prompt.trim(), temperature: draft.temperature, num_predict: draft.num_predict,
    idempotency_key: '' };
}
class RequestError extends Error { constructor(message: string, readonly status: number) { super(message); } }

export default function ObservationPanel({ projectId, options, onBrowse }: {
  projectId: string; options: MaterialOption[]; onBrowse: () => void; onComplete?: () => void;
}) {
  const storageKey = `director-workbench:${projectId}:observation:v1`;
  const prefix = useId();
  const [saved, setSaved] = useState<Saved>(() => load(storageKey));
  const current = useRef(saved);
  const alive = useRef(true);
  const working = useRef(false);
  const [task, setTask] = useState<Task | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const [capability, setCapability] = useState<Capability | null>(null);
  const base = `/api/projects/${encodeURIComponent(projectId)}`;
  const pendingKey = saved.pending?.idempotency_key;
  const draft = saved.draft;
  const locked = Boolean(saved.pending);
  const providerState = capability?.providers?.[draft.provider];
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
      persist({ ...current.current, completedTaskId: next.id });
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
    fetch('/api/vlm-capability').then(response => {
      if (!response.ok) throw new Error('能力检查失败');
      return response.json();
    }).then(data => { if (alive.current) setCapability(data); })
      .catch(() => { if (alive.current) setCapability({ available: false, reason: '无法检查观察环境，请检查连接后刷新。' }); });
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
        const submission = current.current.pending || { ...bodyOf(current.current.draft), idempotency_key: `vlm-${crypto.randomUUID()}` };
        if (!submissionValid(submission)) throw new Error('请确认画面、取帧时间点与观察问题；已有提交请先核对。');
        if (!persist({ ...current.current, pending: submission })) return;
        key = submission.idempotency_key;
        next = await request(`${base}/vlm-tasks`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(submission) });
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
  return <section className="sf-post-tools" aria-label="画面观察">
    <section className="sf-post-section" aria-label="画面观察设置">
      <div className="sf-post-section-head"><h2 className="sf-post-title">画面观察</h2>
        <p className="sf-post-status">{task ? (labels[task.status] || task.status) : '只读观察'}</p></div>
      <p className="sf-post-hint">指定一张图片或视频里的某一个时间点，再写下你要问的问题。模型只描述这一帧，回答只作为观察留在任务里：不写入分镜、不登记素材、不会替你采用。</p>
      <div className="sf-post-fields">
        <div className="sf-post-field"><label htmlFor={`${prefix}-provider`}>模型来源</label>
          <select id={`${prefix}-provider`} disabled={locked} value={draft.provider}
            onChange={event => change('provider', event.target.value === 'deepseek-vision' ? 'deepseek-vision' : 'local-ollama')}>
            {Object.entries(capability?.providers || { 'local-ollama': { available: false, label: '本机 Ollama' } }).map(([key, state]) =>
              <option key={key} value={key} disabled={!state.available}>{state.label || key}{state.available ? '' : '（未就绪）'}</option>)}</select></div>
        <div className="sf-post-field"><label htmlFor={`${prefix}-kind`}>观察对象</label>
          <select id={`${prefix}-kind`} value={draft.source_kind} disabled={locked} onChange={event => change('source_kind', event.target.value === 'video' ? 'video' : 'image')}>
            <option value="image">图片</option><option value="video">视频的某一帧</option></select></div>
        <fieldset className="sf-post-field sf-post-field--wide" disabled={locked}>
          {draft.source_kind === 'image'
            ? <MaterialSelect label="观察图片" kind="image" value={draft.image_source} options={options} onChange={value => change('image_source', value)} onBrowse={onBrowse} />
            : <MaterialSelect label="观察视频" kind="video" value={draft.video_source} options={options} onChange={value => change('video_source', value)} onBrowse={onBrowse} />}
        </fieldset>
        {draft.source_kind === 'video' && <div className="sf-post-field"><label htmlFor={`${prefix}-at`}>取帧时间点（秒）</label>
          <input id={`${prefix}-at`} type="number" min={0} max={86400} step="any" disabled={locked} value={draft.frame_seconds} onChange={event => change('frame_seconds', Number(event.target.value))} /></div>}
        <div className="sf-post-field sf-post-field--wide"><label htmlFor={`${prefix}-prompt`}>观察问题</label>
          <textarea id={`${prefix}-prompt`} rows={3} maxLength={2000} disabled={locked} value={draft.prompt} placeholder="例如：画面里有几个人物？机位与景别是什么？光线从哪个方向来？只描述看得到的内容" onChange={event => change('prompt', event.target.value)} /></div>
      </div>
      {draft.provider === 'deepseek-vision'
        ? <p className="sf-post-alert" role="status">云端来源：这一帧画面会被发送到 {capability?.providers?.['deepseek-vision']?.service || '外部 API'}（模型 {capability?.providers?.['deepseek-vision']?.model || '—'}）。平台有自动内容风控，命中会直接拒答并可能影响账号；成人向或与未成年人相关的画面不要用这一路。</p>
        : <p className="sf-post-hint">本机来源：画面不出这台机器，模型 {capability?.providers?.['local-ollama']?.model || '未就绪'}。{providerState?.reason || ''}</p>}
      <details className="sf-post-subblock"><summary className="sf-post-subtitle">更多设置</summary><div className="sf-post-fields">
        <div className="sf-post-field"><label htmlFor={`${prefix}-temperature`}>采样温度</label>
          <input id={`${prefix}-temperature`} type="number" min={0} max={2} step="any" disabled={locked} value={draft.temperature} onChange={event => change('temperature', Number(event.target.value))} /></div>
        <div className="sf-post-field"><label htmlFor={`${prefix}-length`}>回答长度上限（词）</label>
          <input id={`${prefix}-length`} type="number" min={16} max={2048} step={1} disabled={locked} value={draft.num_predict} onChange={event => change('num_predict', Math.trunc(Number(event.target.value)))} /></div>
      </div><p className="sf-post-hint">回答长度是上限，不保证模型说满；云端来源还会按 token 计费。</p></details>
      {capability && !capability.available && <p className="sf-post-message" role="status" aria-live="polite">画面观察未就绪：{(capability.missing || []).join('、') || capability.reason || '缺少运行环境。'}{capability.reason && ` ${capability.reason}`}</p>}
      {providerState && !providerState.available && <p className="sf-post-message" role="status" aria-live="polite">{providerState.label || draft.provider} 未就绪：{(providerState.missing || []).join('、') || providerState.reason || '缺少配置。'}</p>}
      {message && <p className="sf-post-message" role="status" aria-live="polite">{message}</p>}
      {task?.error && <p className="sf-post-message" role="status" aria-live="polite">{task.error}</p>}
      {result?.observation && <div className="sf-post-subblock">
        <div className="sf-post-section-head"><h3 className="sf-post-subtitle">模型观察</h3><span className="rw-chip">模型输出 · 待人工核对</span></div>
        {result.observation_frame && <figure className="sf-post-figure"><img src={mediaUrl(result.observation_frame)} alt="本次观察的画面" loading="lazy" /><figcaption>本次观察的画面</figcaption></figure>}
        <p className="sf-post-observation">{result.observation}</p>
        <p className="sf-post-hint">问的是：{result.prompt}{result.frame_seconds !== null && result.frame_seconds !== undefined ? ` · 第 ${result.frame_seconds} 秒` : ''} · {result.provider === 'deepseek-vision' ? '云端 DeepSeek' : '本机 Ollama'} {result.model} · {result.eval_count ?? '未知'} 词 · 结束原因 {result.done_reason || '未知'}</p>
        <p className="sf-post-hint">这段回答只是观察证据，需要你自己判断后再决定是否写进分镜。</p>
      </div>}
      <div className="sf-post-actions">
        <button className="button secondary compact" type="button" onClick={onBrowse}>添加素材</button>
        {!locked && <button className="button primary compact" disabled={busy || !providerState?.available || !valid(draft)} onClick={() => void act('submit')}>生成画面观察</button>}
        {locked && !task && <><button className="button secondary compact" disabled={busy} onClick={() => void act('check')}>核对观察提交</button><button className="button secondary compact" disabled={busy || !providerState?.available} onClick={() => void act('submit')}>使用原凭据重试</button></>}
        {task && !pollTerminal.includes(task.status) && <button className="button secondary compact" disabled={busy} onClick={() => void act('stop')}>停止观察</button>}
        {task?.status === 'needs_reconcile' && <button className="button secondary compact" disabled={busy} onClick={() => void act('reconcile')}>核对观察结果</button>}
        {task && terminal.includes(task.status) && <button className="button secondary compact" disabled={busy} onClick={() => { if (persist({ ...current.current, pending: undefined })) { setTask(null); setMessage(''); } }}>准备下一个问题</button>}
        {task && <TaskQueueStatus task={{ ...task, asset_id: '画面观察' }} />}
      </div>
    </section>
  </section>;
}
