import { useEffect, useId, useRef, useState } from 'react';
import CreativeSettingsBar from './CreativeSettingsBar';
import TaskQueueStatus, { type QueueReceipt } from './TaskQueueStatus';
import { ApiError, mediaUrl, request as apiRequest } from './api/client';
import { consumeBringIn, onBringIn } from './libraryBus';

type Draft = { tags: string; lyrics: string; duration_seconds: number; bpm: number; keyscale: string;
  timesignature: string; language: string; seed: number; steps: number; cfg: number; shift: number;
  output_format: 'mp3' | 'flac'; quality: 'V0' | '128k' | '320k'; generate_audio_codes: boolean;
  encode_cfg_scale: number; temperature: number; top_p: number; top_k: number; min_p: number };
type Submission = Draft & { idempotency_key: string };
type Saved = { draft: Draft; pending?: Submission; completedTaskId?: string };
type Task = { id: string; status: string; queue?: QueueReceipt['queue']; error?: string | null; result?: unknown };
type Capability = { available: boolean; missing_nodes?: string[]; missing_models?: string[]; engine_offline?: boolean;
  reason?: string; keyscales?: string[]; timesignatures?: string[]; languages?: string[]; mp3_qualities?: string[];
  duration_seconds?: [number, number] };
type Receipt = { output?: string; outputs?: string[]; music?: Partial<Draft> };

const terminal = ['succeeded', 'failed', 'stopped'];
const pollTerminal = [...terminal, 'needs_reconcile'];
const labels: Record<string, string> = { scheduler_waiting: '已接受 · 等待资源', batch_waiting: '已接受 · 等待依赖',
  submitting: '正在提交', queued: '排队中', running: '正在生成', succeeded: '已生成 · 待审核', failed: '生成失败',
  stopped: '已停止', needs_reconcile: '需要核对结果', stop_requested: '正在停止', stopping: '正在停止' };
const fallback: Capability = { available: false, keyscales: [], timesignatures: ['2', '3', '4', '6'],
  languages: ['unknown', 'en', 'zh'], mp3_qualities: ['V0', '128k', '320k'], duration_seconds: [1, 300] };
const randomSeed = () => crypto.getRandomValues(new Uint32Array(1))[0] % 4294967295;
const initial: Draft = { tags: '', lyrics: '', duration_seconds: 30, bpm: 120, keyscale: 'A minor', timesignature: '4',
  language: 'unknown', seed: randomSeed(), steps: 8, cfg: 1, shift: 3, output_format: 'mp3', quality: 'V0',
  generate_audio_codes: true, encode_cfg_scale: 2, temperature: 0.85, top_p: 0.9, top_k: 0, min_p: 0 };

function number(value: unknown, low: number, high: number, fallbackValue: number): number {
  return typeof value === 'number' && Number.isFinite(value) && value >= low && value <= high ? value : fallbackValue;
}
function load(storageKey: string, capability: Capability): Saved {
  const scales = capability.keyscales?.length ? capability.keyscales : ['A minor'];
  try {
    const value = JSON.parse(localStorage.getItem(storageKey) || '{}');
    const raw = value.draft && typeof value.draft === 'object' ? value.draft : {};
    const draft: Draft = {
      tags: typeof raw.tags === 'string' ? raw.tags : '',
      lyrics: typeof raw.lyrics === 'string' ? raw.lyrics : '',
      duration_seconds: number(raw.duration_seconds, 1, 300, 30),
      bpm: Math.trunc(number(raw.bpm, 10, 300, 120)),
      keyscale: scales.includes(raw.keyscale) ? raw.keyscale : scales[0],
      timesignature: (capability.timesignatures || []).includes(raw.timesignature) ? raw.timesignature : '4',
      language: (capability.languages || []).includes(raw.language) ? raw.language : 'unknown',
      seed: Math.trunc(number(raw.seed, 0, 4294967295, randomSeed())),
      steps: Math.trunc(number(raw.steps, 1, 60, 8)), cfg: number(raw.cfg, 0, 10, 1), shift: number(raw.shift, 0, 20, 3),
      output_format: raw.output_format === 'flac' ? 'flac' : 'mp3',
      quality: (capability.mp3_qualities || []).includes(raw.quality) ? raw.quality : 'V0',
      generate_audio_codes: typeof raw.generate_audio_codes === 'boolean' ? raw.generate_audio_codes : true,
      encode_cfg_scale: number(raw.encode_cfg_scale, 0, 100, 2), temperature: number(raw.temperature, 0, 2, 0.85),
      top_p: number(raw.top_p, 0, 2000, 0.9), top_k: Math.trunc(number(raw.top_k, 0, 100, 0)), min_p: number(raw.min_p, 0, 1, 0),
    };
    const pending = value.pending && typeof value.pending.idempotency_key === 'string' ? value.pending : undefined;
    return { draft, pending, completedTaskId: typeof value.completedTaskId === 'string' ? value.completedTaskId : undefined };
  } catch { return { draft: { ...initial } }; }
}
function valid(draft: Draft): boolean {
  return Boolean(draft.tags.trim()) && draft.tags.length <= 2000 && draft.lyrics.length <= 4000
    && Number.isFinite(draft.duration_seconds) && draft.duration_seconds >= 1 && draft.duration_seconds <= 300
    && Number.isInteger(draft.bpm) && draft.bpm >= 10 && draft.bpm <= 300 && Boolean(draft.keyscale)
    && Number.isInteger(draft.seed) && draft.seed >= 0 && draft.seed <= 4294967295
    && Number.isInteger(draft.steps) && draft.steps >= 1 && draft.steps <= 60
    && Number.isFinite(draft.cfg) && draft.cfg >= 0 && draft.cfg <= 10
    && Number.isFinite(draft.shift) && draft.shift >= 0 && draft.shift <= 20;
}
class RequestError extends Error { constructor(message: string, readonly status: number) { super(message); } }

// 带入预设卡片只覆盖卡片里带的项；随机编号永远留在你自己手里。
function fromCard(state: Saved, card?: Record<string, unknown> | null): Saved {
  if (!card) return state;
  const patch = { ...card } as Partial<Draft>;
  delete patch.seed;
  return { ...state, draft: { ...state.draft, ...patch } };
}

export default function MusicPanel({ projectId, onComplete, initialCard }: { projectId: string; onComplete: () => void; initialCard?: Record<string, unknown> | null }) {
  const storageKey = `director-workbench:${projectId}:music:v1`;
  const prefix = useId();
  const [capability, setCapability] = useState<Capability | null>(null);
  const [saved, setSaved] = useState<Saved>(() => ({ draft: { ...initial } }));
  const current = useRef(saved);
  const alive = useRef(true);
  const working = useRef(false);
  const completion = useRef(onComplete);
  completion.current = onComplete;
  const [task, setTask] = useState<Task | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const [cardName, setCardName] = useState('');
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
  // 配乐库 → 配乐：带入风格/速度/调性/拍号/时长，随机编号仍由你自己定。
  useEffect(() => {
    const apply = (params: Record<string, unknown>) => {
      if (current.current.pending) return;
      const patch = { ...params } as Partial<Draft>;
      delete patch.seed;
      persist({ ...current.current, draft: { ...current.current.draft, ...patch } });
      setMessage('已从配乐库带入风格参数；生成前请确认时长与随机编号。');
    };
    const queued = consumeBringIn('music');
    if (queued) apply(queued);
    return onBringIn(detail => { if (detail.kind === 'music') apply(detail.params); });
  }, []);
  useEffect(() => {
    alive.current = true;
    fetch('/api/music-capability').then(response => {
      if (!response.ok) throw new Error('能力检查失败');
      return response.json();
    }).then((data: Capability) => {
      if (!alive.current) return;
      const ready = { ...fallback, ...data };
      setCapability(ready);
      setSaved(state => { const next = fromCard(load(storageKey, ready), initialCard); current.current = next; return next; });
    }).catch(() => {
      if (!alive.current) return;
      setCapability({ ...fallback, reason: '无法检查配乐环境，请检查连接后刷新。' });
      setSaved(state => { const next = fromCard(load(storageKey, fallback), initialCard); current.current = next; return next; });
    });
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
        const submission = current.current.pending || { ...current.current.draft, tags: current.current.draft.tags.trim(), idempotency_key: `music-${crypto.randomUUID()}` };
        if (!valid(submission)) throw new Error('请检查风格标签、时长、调性与随机编号；已有提交请先核对。');
        if (!persist({ ...current.current, pending: submission })) return;
        key = submission.idempotency_key;
        next = await request(`${base}/music-tasks`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(submission) });
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
  // 配乐库存的是起点参数，不存随机编号：那是一次生成的决定。
  async function saveCard() {
    const label = cardName.trim();
    if (!label || !valid(draft)) return;
    setBusy(true);
    try {
      const params = { tags: draft.tags.trim(), bpm: draft.bpm, keyscale: draft.keyscale,
        timesignature: draft.timesignature, duration_seconds: draft.duration_seconds, language: draft.language };
      await apiRequest(`/api/preset-library/music/${encodeURIComponent(label)}`, { init: { method: 'PUT', body: JSON.stringify({ params }), headers: { 'Content-Type': 'application/json' } } });
      if (alive.current) { setCardName(''); setMessage(`已存入配乐库：${label}`); }
    } catch (reason) {
      if (alive.current) setMessage(reason instanceof ApiError ? `存入配乐库失败：${reason.message}` : '存入配乐库未完成');
    } finally { if (alive.current) setBusy(false); }
  }
  // A saved setting never carries a seed: the seed is decided per generation attempt.
  const { seed: _seed, ...settingValues } = draft;
  function applySetting(params: Record<string, unknown>) {
    const patch = { ...params } as Partial<Draft>;
    delete patch.seed;
    persist({ ...current.current, draft: { ...current.current.draft, ...patch } });
  }
  const field = <K extends keyof Draft>(name: K, label: string, options: { min?: number; max?: number; step?: number | string; integer?: boolean }) =>
    <div className="sf-post-field"><label htmlFor={`${prefix}-${name}`}>{label}</label>
      <input id={`${prefix}-${name}`} type="number" min={options.min} max={options.max} step={options.step ?? 'any'} disabled={locked}
        value={draft[name] as number} onChange={event => change(name, (options.integer ? Math.trunc(Number(event.target.value)) : Number(event.target.value)) as Draft[K])} /></div>;
  return <section className="sf-post-tools" aria-label="配乐生成">
    <section className="sf-post-section" aria-label="配乐参数">
      <div className="sf-post-section-head"><h2 className="sf-post-title">配乐</h2>
        <p className="sf-post-status">{task ? (labels[task.status] || task.status) : '显式提交'}</p></div>
      <p className="sf-post-hint">ACE-Step 1.5 按你写的风格标签、时长、BPM、调性与拍号生成一段新音乐。歌词留空即器乐。输出是配乐候选，不会自动接进音轨，也不会替你采用。</p>
      <CreativeSettingsBar kind="music" values={settingValues} onApply={applySetting} />
      <div className="sf-post-fields">
        <div className="sf-post-field sf-post-field--wide"><label htmlFor={`${prefix}-tags`}>风格标签</label>
          <textarea id={`${prefix}-tags`} rows={3} maxLength={2000} disabled={locked} value={draft.tags} placeholder="例如：悬疑氛围，弦乐铺底与低频脉冲，克制、无人声" onChange={event => change('tags', event.target.value)} /></div>
        <div className="sf-post-field sf-post-field--wide"><label htmlFor={`${prefix}-lyrics`}>歌词（留空＝器乐）</label>
          <textarea id={`${prefix}-lyrics`} rows={3} maxLength={4000} disabled={locked} value={draft.lyrics} placeholder="[verse]&#10;……&#10;[chorus]&#10;……" onChange={event => change('lyrics', event.target.value)} /></div>
        {field('duration_seconds', '时长（秒）', { min: 1, max: 300 })}
        {field('bpm', 'BPM', { min: 10, max: 300, step: 1, integer: true })}
        <div className="sf-post-field"><label htmlFor={`${prefix}-keyscale`}>调性</label>
          <select id={`${prefix}-keyscale`} disabled={locked} value={draft.keyscale} onChange={event => change('keyscale', event.target.value)}>
            {(capability?.keyscales || []).length ? (capability?.keyscales || []).map(value => <option key={value} value={value}>{value}</option>)
              : <option value={draft.keyscale}>{draft.keyscale}</option>}</select></div>
        <div className="sf-post-field"><label htmlFor={`${prefix}-timesignature`}>拍号</label>
          <select id={`${prefix}-timesignature`} disabled={locked} value={draft.timesignature} onChange={event => change('timesignature', event.target.value)}>
            {(capability?.timesignatures || ['2', '3', '4', '6']).map(value => <option key={value} value={value}>{value}</option>)}</select></div>
        <div className="sf-post-field"><label htmlFor={`${prefix}-language`}>歌词语言</label>
          <select id={`${prefix}-language`} disabled={locked} value={draft.language} onChange={event => change('language', event.target.value)}>
            {(capability?.languages || []).length ? (capability?.languages || []).map(value => <option key={value} value={value}>{value}</option>)
              : <option value={draft.language}>{draft.language}</option>}</select></div>
        {field('seed', '随机编号', { min: 0, max: 4294967295, step: 1, integer: true })}
        {field('steps', '采样步数', { min: 1, max: 60, step: 1, integer: true })}
        {field('cfg', 'CFG', { min: 0, max: 10 })}
        {field('shift', 'Flow shift', { min: 0, max: 20 })}
        <div className="sf-post-field"><label htmlFor={`${prefix}-format`}>输出格式</label>
          <select id={`${prefix}-format`} disabled={locked} value={draft.output_format} onChange={event => change('output_format', event.target.value === 'flac' ? 'flac' : 'mp3')}>
            <option value="mp3">MP3</option><option value="flac">FLAC（无损）</option></select></div>
        {draft.output_format === 'mp3' && <div className="sf-post-field"><label htmlFor={`${prefix}-quality`}>MP3 质量</label>
          <select id={`${prefix}-quality`} disabled={locked} value={draft.quality} onChange={event => change('quality', event.target.value as Draft['quality'])}>
            {(capability?.mp3_qualities || ['V0', '128k', '320k']).map(value => <option key={value} value={value}>{value}</option>)}</select></div>}
        {!locked && <div className="sf-post-field"><span className="sf-post-hint">随机编号由你掌控</span>
          <button className="button secondary compact" type="button" onClick={() => change('seed', randomSeed())}>换一个随机编号</button></div>}
      </div>
      <details className="sf-post-subblock"><summary className="sf-post-subtitle">更多设置</summary>
        <label className="sf-post-check"><input type="checkbox" disabled={locked} checked={draft.generate_audio_codes} onChange={event => change('generate_audio_codes', event.target.checked)} />启用歌词→音频码的 LLM（更慢，官方说质量更好；给了音频参考时应关掉）</label>
        <div className="sf-post-fields">
          {field('encode_cfg_scale', '歌词生成 CFG', { min: 0, max: 100 })}
          {field('temperature', '歌词温度', { min: 0, max: 2 })}
          {field('top_p', '歌词 top_p', { min: 0, max: 2000 })}
          {field('top_k', '歌词 top_k', { min: 0, max: 100, step: 1, integer: true })}
          {field('min_p', '歌词 min_p', { min: 0, max: 1 })}
        </div></details>
      {capability && !capability.available && <p className="sf-post-message" role="status" aria-live="polite">配乐未就绪：{(capability.missing_models || []).concat(capability.missing_nodes || []).join('、') || capability.reason || '缺少运行环境。'}</p>}
      {message && <p className="sf-post-message" role="status" aria-live="polite">{message}</p>}
      {task?.error && <p className="sf-post-message" role="status" aria-live="polite">{task.error}</p>}
      {result?.output && <div className="sf-post-subblock">
        <audio controls preload="metadata" aria-label="配乐候选试听" src={mediaUrl(result.output)} />
        <p className="sf-post-hint">实测时长以文件为准；标签「{result.music?.tags?.slice(0, 40)}」· BPM {result.music?.bpm ?? '—'} · {result.music?.keyscale ?? '—'} · 随机编号 {result.music?.seed ?? '—'}。听完再决定是否放进音轨。</p>
      </div>}
      <div className="sf-post-actions">
        {!locked && <button className="button primary compact" disabled={busy || !capability?.available || !valid(draft)} onClick={() => void act('submit')}>生成配乐候选</button>}
        {locked && !task && <><button className="button secondary compact" disabled={busy} onClick={() => void act('check')}>核对配乐提交</button><button className="button secondary compact" disabled={busy || !capability?.available} onClick={() => void act('submit')}>使用原凭据重试</button></>}
        {task && !pollTerminal.includes(task.status) && <button className="button secondary compact" disabled={busy} onClick={() => void act('stop')}>停止配乐任务</button>}
        {task?.status === 'needs_reconcile' && <button className="button secondary compact" disabled={busy} onClick={() => void act('reconcile')}>核对配乐结果</button>}
        {task && terminal.includes(task.status) && <button className="button secondary compact" disabled={busy} onClick={() => { if (persist({ ...current.current, pending: undefined })) { setTask(null); setMessage(''); } }}>准备下一段配乐</button>}
        {task && <TaskQueueStatus task={{ ...task, asset_id: '配乐' }} />}
      </div>
      {!locked && <div className="sf-post-subblock" aria-label="存进配乐库">
        <div className="sf-post-fields">
          <div className="sf-post-field sf-post-field--wide"><label htmlFor={`${prefix}-card`}>把这套参数存进配乐库</label>
            <input id={`${prefix}-card`} maxLength={40} value={cardName} placeholder="卡片名称，例如：雨夜追逐 140拍" onChange={event => setCardName(event.target.value)} /></div>
          <div className="sf-post-field"><span className="sf-post-hint">只存标签、BPM、调性、拍号、时长与语言</span>
            <button className="button secondary compact" type="button" disabled={busy || !cardName.trim() || !valid(draft)} onClick={() => void saveCard()}>存入配乐库</button></div>
        </div>
      </div>}
    </section>
  </section>;
}
