import { useEffect, useId, useRef, useState } from 'react';
import TaskQueueStatus, { type QueueReceipt } from './TaskQueueStatus';
import { ApiError, mediaUrl, request as apiRequest } from './api/client';

// 两条真实链路：VoxCPM2 能凭文字造音色，IndexTTS 2.5 只能克隆你给的参考音频。
// 换引擎不会偷偷换模型，而是把表单换成那一条路真正需要填的东西。
type Engine = 'voxcpm-2' | 'indextts-2.5';
type Draft = { engine: Engine; voice_description: string; voice_reference: string; text: string; seed: number };
type DesignBody = { engine: 'voxcpm-2'; mode: 'design'; voice_description: string; text: string; seed: number };
type ReferenceBody = { engine: 'indextts-2.5'; mode: 'reference'; voice_reference: string; text: string };
type Submission = (DesignBody | ReferenceBody) & { idempotency_key: string };
type Saved = { draft: Draft; pending?: Submission; completedTaskId?: string };
type Task = { id: string; status: string; queue?: QueueReceipt['queue']; error?: string | null; result?: unknown };
type EngineCapability = { available: boolean; missing?: string[]; timbre?: string; seed?: boolean;
  sample_rate?: number; language?: string };
type Capability = EngineCapability & { default_engine?: Engine; engines?: Record<string, EngineCapability>; reason?: string };
type Receipt = { output?: string; duration_seconds?: number; sample_rate?: number; timbre_prefix?: string };
type Asset = { id: string; kind?: string; title?: string; subtitle?: string; end?: number; sources?: { A?: string } };

const engineIds: Engine[] = ['voxcpm-2', 'indextts-2.5'];
const engineNames: Record<Engine, string> = { 'voxcpm-2': 'VoxCPM2', 'indextts-2.5': 'IndexTTS 2.5' };
// Trade-offs stated once, from what each engine actually does on this machine.
const engineTradeoffs: Record<Engine, { good: string[]; bad: string[] }> = {
  'voxcpm-2': {
    good: ['能用一句话凭空造出新音色，不需要任何参考音频', '有随机编号：同一段描述 + 同一个编号＝同一个声音', '输出 48kHz，细节更多', '支持中文、英文等 30 种语言和 9 种中文方言'],
    bad: ['情绪只能用文字描述，没有八维强度或情绪参考音频', '音色像不像全靠描述写得好不好', '本机实测单句连加载约 20–30 秒'],
  },
  'indextts-2.5': {
    good: ['克隆参考音频时最贴近那段原声的口音与质感', '情绪控制最多：八维强度、情绪参考音频、文本描述都支持', '中文台词的停顿和咬字更稳'],
    bad: ['不能凭文字造音色，必须先给一段参考音频', '没有随机编号，同样参数重跑不保证一模一样', '输出 22.05kHz', '本机实测单句连加载约 30–50 秒'],
  },
};
const terminal = ['succeeded', 'failed', 'stopped'];
const pollTerminal = [...terminal, 'needs_reconcile'];
const labels: Record<string, string> = { scheduler_waiting: '已接受 · 等待资源', batch_waiting: '已接受 · 等待依赖',
  submitting: '正在生成', succeeded: '已生成 · 待试听', failed: '生成失败', stopped: '已停止',
  needs_reconcile: '需要核对结果', stop_requested: '正在停止', stopping: '正在停止' };
const initial: Draft = { engine: 'voxcpm-2', voice_description: '', voice_reference: '', text: '这段声音只用来试音色，语速平稳，咬字清楚。', seed: 20261025 };
const randomSeed = () => crypto.getRandomValues(new Uint32Array(1))[0] % 4294967295;

function load(storageKey: string): Saved {
  try {
    const value = JSON.parse(localStorage.getItem(storageKey) || '{}');
    const raw = value.draft && typeof value.draft === 'object' ? value.draft : {};
    return {
      draft: { engine: raw.engine === 'indextts-2.5' ? 'indextts-2.5' : 'voxcpm-2',
        voice_description: typeof raw.voice_description === 'string' ? raw.voice_description : '',
        voice_reference: typeof raw.voice_reference === 'string' ? raw.voice_reference : '',
        text: typeof raw.text === 'string' && raw.text.trim() ? raw.text : initial.text,
        seed: Number.isInteger(raw.seed) && raw.seed >= 0 && raw.seed <= 4294967295 ? raw.seed : initial.seed },
      pending: value.pending && typeof value.pending.idempotency_key === 'string' ? value.pending : undefined,
      completedTaskId: typeof value.completedTaskId === 'string' ? value.completedTaskId : undefined,
    };
  } catch { return { draft: { ...initial } }; }
}
// Only the fields that engine actually accepts go out; IndexTTS rejects a random number.
type DistOmit<T, K extends keyof T> = T extends unknown ? Omit<T, K> : never;
function body(draft: Draft): DistOmit<Submission, 'idempotency_key'> {
  const text = draft.text.trim();
  return draft.engine === 'voxcpm-2'
    ? { engine: 'voxcpm-2', mode: 'design', voice_description: draft.voice_description.trim(), text, seed: draft.seed }
    : { engine: 'indextts-2.5', mode: 'reference', voice_reference: draft.voice_reference, text };
}
function valid(draft: Draft): boolean {
  if (!draft.text.trim() || draft.text.length > 2000) return false;
  return draft.engine === 'voxcpm-2'
    ? Boolean(draft.voice_description.trim()) && draft.voice_description.length <= 2000
      && Number.isInteger(draft.seed) && draft.seed >= 0 && draft.seed <= 4294967295
    : Boolean(draft.voice_reference);
}
class RequestError extends Error { constructor(message: string, readonly status: number) { super(message); } }

export default function TimbrePanel({ projectId, onComplete, initialDescription }: {
  projectId: string; onComplete: () => void; initialDescription?: string;
}) {
  const storageKey = `director-workbench:${projectId}:timbre:v1`;
  const prefix = useId();
  // 音色库带入的描述只是一次起点，不覆盖你已经写下的内容。
  function seeded(state: Saved): Saved {
    return initialDescription && !state.draft.voice_description
      ? { ...state, draft: { ...state.draft, voice_description: initialDescription } } : state;
  }
  const [saved, setSaved] = useState<Saved>(() => seeded(load(storageKey)));
  const current = useRef(saved);
  const alive = useRef(true);
  const working = useRef(false);
  const completion = useRef(onComplete);
  completion.current = onComplete;
  const [task, setTask] = useState<Task | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const [capability, setCapability] = useState<Capability | null>(null);
  const [samples, setSamples] = useState<Asset[]>([]);
  const [audios, setAudios] = useState<Asset[]>([]);
  const [cardName, setCardName] = useState('');
  const base = `/api/projects/${encodeURIComponent(projectId)}`;
  const pendingKey = saved.pending?.idempotency_key;
  const draft = saved.draft;
  const locked = Boolean(saved.pending);
  const vox = draft.engine === 'voxcpm-2';
  const result = task?.status === 'succeeded' && task.result && typeof task.result === 'object' ? task.result as Receipt : null;
  function persist(next: Saved) {
    if (!alive.current) return false;
    try { localStorage.setItem(storageKey, JSON.stringify(next)); current.current = next; setSaved(next); return true; }
    catch { setMessage('无法保存提交凭据，请恢复浏览器存储后重试。'); return false; }
  }
  function accept(next: Task, key: string) {
    if (!alive.current || current.current.pending?.idempotency_key !== key) return;
    setTask(next); setMessage('');
    if (next.status === 'succeeded') void readAssets();
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
  // Timbre samples are ordinary registered audio, so 配音 and 锚点配音 can pick them as
  // 音色参考 without a second store; this panel only reads the project manifest.
  async function readAssets() {
    try {
      const detail = await apiRequest<{ assets?: Asset[] }>(base);
      if (!alive.current) return;
      const assets = detail.assets || [];
      setSamples(assets.filter(asset => (asset.subtitle || '') === '音色样本 · 待试听').slice(-8).reverse());
      setAudios(assets.filter(asset => asset.kind === 'audio' && asset.sources?.A).slice(-24).reverse());
    } catch { if (alive.current) { setSamples([]); setAudios([]); } }
  }
  useEffect(() => {
    alive.current = true;
    void readAssets();
    fetch('/api/speech-capability').then(response => {
      if (!response.ok) throw new Error('能力检查失败');
      return response.json();
    }).then(data => { if (alive.current) setCapability(data); })
      .catch(() => { if (alive.current) setCapability({ available: false, reason: '无法检查声音环境，请检查连接后刷新。' }); });
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
        const submission = current.current.pending || { ...body(current.current.draft), idempotency_key: `timbre-${crypto.randomUUID()}` };
        if (!valid(current.current.draft)) throw new Error(vox ? '请写清音色描述与试听台词，并确认随机编号是 0 到 4294967295 的整数。' : 'IndexTTS 2.5 需要一段参考音频和一句试听台词。');
        if (!persist({ ...current.current, pending: submission })) return;
        key = submission.idempotency_key;
        next = await request(`${base}/speech-tasks`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(submission) });
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
  // The library keeps the starting point, never the random number: that stays per attempt.
  async function saveCard() {
    const label = cardName.trim();
    if (!label || !valid(draft)) return;
    const params = vox ? { engine: draft.engine, voice_description: draft.voice_description.trim(), text: draft.text.trim() }
      : { engine: draft.engine, voice_reference: draft.voice_reference, text: draft.text.trim() };
    setBusy(true);
    try {
      await apiRequest(`/api/preset-library/timbre/${encodeURIComponent(label)}`, { init: { method: 'PUT', body: JSON.stringify({ params }), headers: { 'Content-Type': 'application/json' } } });
      if (alive.current) { setCardName(''); setMessage(`已存入音色库：${label}`); }
    } catch (reason) {
      if (alive.current) setMessage(reason instanceof ApiError ? `存入音色库失败：${reason.message}` : '存入音色库未完成');
    } finally { if (alive.current) setBusy(false); }
  }
  const facts: EngineCapability | null = capability?.engines?.[draft.engine] ?? null;
  const ready = Boolean(facts?.available ?? capability?.available);
  const tradeoff = engineTradeoffs[draft.engine];
  const other = engineIds.find(engine => engine !== draft.engine)!;
  return <section className="sf-post-tools" aria-label="音色设计">
    <section className="sf-post-section" aria-label="音色设计设置">
      <div className="sf-post-section-head"><h2 className="sf-post-title">音色设计</h2>
        <p className="sf-post-status">{task ? (labels[task.status] || task.status) : '显式提交'}</p></div>
      <p className="sf-post-hint">先选引擎，下面只会问这条路线真正需要的东西：VoxCPM2 写一句话造音色，IndexTTS 2.5 拿一段参考音频照着念。生成的都是待试听候选，不会自动接入音轨，也不会替你采用。</p>
      <div className="sf-post-fields">
        <div className="sf-post-field"><label htmlFor={`${prefix}-engine`}>声音引擎</label>
          <select id={`${prefix}-engine`} disabled={locked} value={draft.engine} onChange={event => change('engine', event.target.value === 'indextts-2.5' ? 'indextts-2.5' : 'voxcpm-2')}>
            {engineIds.map(engine => <option key={engine} value={engine}>{engineNames[engine]}{capability?.engines?.[engine]?.available === false ? '（未就绪）' : ''}</option>)}</select></div>
        <div className="sf-post-field sf-post-field--wide"><label htmlFor={`${prefix}-text`}>试听台词</label>
          <textarea id={`${prefix}-text`} rows={2} maxLength={2000} disabled={locked} value={draft.text}
            placeholder="随便一句用来听音色的话" onChange={event => change('text', event.target.value)} /></div>
        {vox
          ? <div className="sf-post-field sf-post-field--wide"><label htmlFor={`${prefix}-description`}>音色描述</label>
            <textarea id={`${prefix}-description`} rows={3} maxLength={2000} disabled={locked} value={draft.voice_description}
              placeholder="例如：五十岁上下的男性，声音低沉沙哑，语速偏慢，克制里带着疲惫" onChange={event => change('voice_description', event.target.value)} /></div>
          : <div className="sf-post-field sf-post-field--wide"><label htmlFor={`${prefix}-reference`}>参考音频 · 音色就从这里来</label>
            <select id={`${prefix}-reference`} disabled={locked} value={draft.voice_reference} onChange={event => change('voice_reference', event.target.value)}>
              <option value="">选择本作品里的一段音频</option>
              {audios.map(asset => <option key={asset.id} value={String(asset.sources?.A)}>{asset.title || asset.id}</option>)}</select>
            {!audios.length && <p className="sf-post-hint">这个作品里还没有可参考的音频。先在素材区登记一段，或改用 VoxCPM2 直接描述音色。</p>}</div>}
        {vox && <div className="sf-post-field"><label htmlFor={`${prefix}-seed`}>随机编号</label>
          <input id={`${prefix}-seed`} type="number" min={0} max={4294967295} step={1} disabled={locked}
            value={locked ? (saved.pending?.mode === 'design' ? saved.pending.seed : '') : draft.seed}
            onChange={event => change('seed', Number(event.target.value))} /></div>}
        {vox && !locked && <div className="sf-post-field"><span className="sf-post-hint">随机编号由你掌控</span>
          <button className="button secondary compact" type="button" onClick={() => change('seed', randomSeed())}>换一个随机编号</button></div>}
      </div>
      <div className="sf-post-subblock" aria-label="所选引擎的优势与代价">
        <div className="sf-post-section-head"><h3 className="sf-post-subtitle">{engineNames[draft.engine]} 在这台机器上</h3>
          <p className="sf-post-meta">{facts ? `${facts.sample_rate ? `${facts.sample_rate / 1000}kHz · ` : ''}${facts.seed === false ? '无随机编号' : '有随机编号'} · ${facts.language || '中文'}` : '正在检查…'}</p></div>
        {tradeoff.good.map(line => <p className="sf-post-hint" key={line}>优势 · {line}</p>)}
        {tradeoff.bad.map(line => <p className="sf-post-hint" key={line}>代价 · {line}</p>)}
        <p className="sf-post-hint">换用 {engineNames[other]}：{engineTradeoffs[other].good[0]}；但{engineTradeoffs[other].bad[0]}。</p>
      </div>
      <p className="sf-post-hint">时长由台词决定，不能指定。{vox ? 'VoxCPM2 输出 48kHz，同一段描述 + 同一个随机编号会回到同一个声音，样本进下面的音色列表。' : 'IndexTTS 2.5 输出 22.05kHz，没有随机编号，重跑不会一模一样；这条样本记在「配音」名下，不进下面的音色列表。'}</p>
      {!ready && <p className="sf-post-message" role="status" aria-live="polite">{engineNames[draft.engine]} 未就绪：{(facts?.missing || []).join('、') || capability?.reason || '缺少运行环境。'}</p>}
      {message && <p className="sf-post-message" role="status" aria-live="polite">{message}</p>}
      {task?.error && <p className="sf-post-message" role="status" aria-live="polite">{task.error}</p>}
      {result?.output && <div className="sf-post-subblock">
        <audio controls preload="metadata" aria-label="音色样本试听" src={mediaUrl(result.output)} />
        <p className="sf-post-hint">{vox ? `音色说明「${result.timbre_prefix?.slice(0, 60) || draft.voice_description.slice(0, 60)}」· ` : '参考音色克隆 · '}{result.duration_seconds ? `${result.duration_seconds.toFixed(2)} 秒` : '时长未知'} · {result.sample_rate ?? facts?.sample_rate ?? 48000}Hz。满意就把它当音色参考：本作品可直接选，换作品时在素材区「复用素材」带走。</p>
      </div>}
      {samples.length > 0 && <div className="sf-post-subblock">
        <div className="sf-post-section-head"><h3 className="sf-post-subtitle">本作已设计的音色</h3><p className="sf-post-meta">{samples.length} 段 · 可在配音与锚点配音里选作音色参考</p></div>
        {samples.map(sample => <div className="sf-post-fields" key={sample.id}>
          <div className="sf-post-field sf-post-field--wide"><span>{sample.title}</span>
            <audio controls preload="metadata" aria-label={`试听 ${sample.title || sample.id}`} src={mediaUrl(String(sample.sources?.A || ''))} /></div>
          <div className="sf-post-field"><span>素材编号</span><p className="sf-post-hint">{sample.id} · {sample.end ? `${sample.end.toFixed(2)} 秒` : '时长未知'}</p></div>
        </div>)}
      </div>}
      <div className="sf-post-actions">
        {!locked && <button className="button primary compact" disabled={busy || !ready || !valid(draft)} onClick={() => void act('submit')}>生成音色样本</button>}
        {locked && !task && <><button className="button secondary compact" disabled={busy} onClick={() => void act('check')}>核对提交</button><button className="button secondary compact" disabled={busy || !ready} onClick={() => void act('submit')}>使用原凭据重试</button></>}
        {task && !pollTerminal.includes(task.status) && <button className="button secondary compact" disabled={busy} onClick={() => void act('stop')}>停止任务</button>}
        {task?.status === 'needs_reconcile' && <button className="button secondary compact" disabled={busy} onClick={() => void act('reconcile')}>核对结果</button>}
        {task && terminal.includes(task.status) && <button className="button secondary compact" disabled={busy} onClick={() => { if (persist({ ...current.current, pending: undefined })) { setTask(null); setMessage(''); } }}>设计下一个音色</button>}
        {task && <TaskQueueStatus task={{ ...task, asset_id: '音色设计' }} />}
      </div>
      {!locked && <div className="sf-post-subblock" aria-label="存进音色库">
        <div className="sf-post-fields">
          <div className="sf-post-field sf-post-field--wide"><label htmlFor={`${prefix}-card`}>把这段音色存进音色库</label>
            <input id={`${prefix}-card`} maxLength={40} value={cardName} placeholder="卡片名称，例如：沙哑老翁" onChange={event => setCardName(event.target.value)} /></div>
          <div className="sf-post-field"><span className="sf-post-hint">只存引擎、描述与台词</span>
            <button className="button secondary compact" type="button" disabled={busy || !cardName.trim() || !valid(draft)} onClick={() => void saveCard()}>存入音色库</button></div>
        </div>
      </div>}
    </section>
  </section>;
}
