import { useEffect, useId, useRef, useState } from 'react';
import TaskQueueStatus, { type QueueReceipt } from './TaskQueueStatus';
import MaterialSelect, { type MaterialOption } from './MaterialSelect';
import EmotionControls, { emotionPayload, emotionReady, noEmotion, readEmotion, type Emotion } from './EmotionControls';
import { ApiError, request as apiRequest } from './api/client';

type Engine = 'auk-flash' | 'indextts-2.5' | 'voxcpm-2';
type Submission = { engine?: Engine; gen_seconds?: number; seed?: number; text: string; mode?: 'reference' | 'design'; voice_reference?: string; voice_description?: string; emotion?: Record<string, unknown> | null; idempotency_key: string };
type Task = { id: string; status: string; queue?: QueueReceipt['queue']; error?: string; result?: { output?: string } };
type Saved = { engine: Engine; gen_seconds: number; seed: number; text: string; mode: 'reference' | 'design'; voice_reference: string; voice_description: string; emotion: Emotion; pending?: Submission };
type EngineCapability = { available: boolean; missing?: string[]; note?: string; modes?: string[]; reason?: string; emotion_modes?: Record<string, boolean>; emotion_unsupported?: string[]; timbre?: string; seed?: boolean; sample_rate?: number };
type Capability = EngineCapability & { engines?: Record<string, EngineCapability> };
const engineLabels: Record<Engine, string> = { 'auk-flash': 'AuK-Flash', 'indextts-2.5': 'IndexTTS 2.5', 'voxcpm-2': 'VoxCPM2' };
// VoxCPM2 与 AuK 支持用文字凭空造音色；IndexTTS 只能克隆参考音频。
const designEngines: Engine[] = ['auk-flash', 'voxcpm-2'];
class RequestError extends Error {
  constructor(message: string, readonly status: number) { super(message); }
}
const terminal = ['succeeded', 'failed', 'stopped'];
const labels: Record<string, string> = { scheduler_waiting: '已接受 · 等待资源', batch_waiting: '已接受 · 等待依赖', submitting: '正在生成', succeeded: '已生成 · 待试听', failed: '生成失败', stopped: '已停止', stop_requested: '正在停止', stopping: '正在停止', needs_reconcile: '需要核对结果' };

export default function SpeechPanel({ projectId, options, onBrowse, onComplete, defaultOpen = false }: {
  projectId: string; options: MaterialOption[]; onBrowse: () => void; onComplete: () => void; defaultOpen?: boolean;
}) {
  // The shell now renders every post tool expanded; keep accepting the legacy
  // prop without letting it collapse the form again.
  void defaultOpen;
  const storageKey = `director-workbench:${projectId}:speech:v1`;
  const textId = useId();
  const durationId = useId();
  const seedId = useId();
  const modeId = useId();
  const engineId = useId();
  const descriptionId = useId();
  const [saved, setSaved] = useState<Saved>(() => {
    try {
      const value = JSON.parse(localStorage.getItem(storageKey) || '{}');
      return { engine: value.engine === 'indextts-2.5' ? 'indextts-2.5' : value.engine === 'auk-flash' ? 'auk-flash' : 'voxcpm-2', gen_seconds: typeof value.gen_seconds === 'number' && value.gen_seconds > 0 && value.gen_seconds <= 30 ? value.gen_seconds : 4.5, seed: Number.isInteger(value.seed) && value.seed >= 0 && value.seed <= 4294967295 ? value.seed : 20260923, text: typeof value.text === 'string' ? value.text : '', voice_reference: typeof value.voice_reference === 'string' ? value.voice_reference : '',
        mode: value.mode === 'design' ? 'design' : 'reference', voice_description: typeof value.voice_description === 'string' ? value.voice_description : '',
        emotion: readEmotion(value.emotion),
        pending: value.pending && typeof value.pending.idempotency_key === 'string' && typeof value.pending.text === 'string' && (value.pending.mode === 'design' ? typeof value.pending.voice_description === 'string' : typeof value.pending.voice_reference === 'string') ? value.pending : undefined };
    } catch { return { engine: 'voxcpm-2', gen_seconds: 4.5, seed: 20260923, text: '', mode: 'reference', voice_reference: '', voice_description: '', emotion: { ...noEmotion } }; }
  });
  const [task, setTask] = useState<Task | null>(null);
  const pendingKey = useRef(saved.pending?.idempotency_key);
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);
  const [capability, setCapability] = useState<Capability | null>(null);
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
    try {
      return await apiRequest<Task>(url, { init });
    } catch (cause) {
      if (cause instanceof ApiError) throw new RequestError(cause.message, cause.status);
      throw cause;
    }
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
        const pending = saved.pending || { engine: saved.engine, ...(saved.engine === 'auk-flash' ? { gen_seconds: saved.gen_seconds, seed: saved.seed } : {}),
          ...(saved.engine === 'voxcpm-2' ? { seed: saved.seed } : {}),
          text: saved.text.trim(), mode: saved.mode,
          ...(saved.mode === 'design' ? { voice_description: saved.voice_description.trim() } : { voice_reference: saved.voice_reference }),
          ...(saved.engine === 'auk-flash' ? {} : { emotion: emotionPayload(saved.emotion) }), idempotency_key: `speech-${crypto.randomUUID()}` };
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
  const engineEntries = capability?.engines ? Object.entries(capability.engines) : capability ? [[saved.engine, capability] as [string, EngineCapability]] : [];
  const engineCapability = capability?.engines?.[saved.engine] ?? capability ?? null;
  const anotherEngineReady = engineEntries.some(([id, item]) => id !== saved.engine && item.available);
  const auk = saved.engine === 'auk-flash';
  const vox = saved.engine === 'voxcpm-2';
  const canDesign = designEngines.includes(saved.engine) && (engineCapability?.modes || ['reference', 'design']).includes('design');
  return <section className="sf-post-tools" aria-label="生成声音">
    <section className="sf-post-section" aria-label="声音生成设置">
      <div className="sf-post-section-head"><h2 className="sf-post-title">声音生成</h2><p className="sf-post-status">{task ? (labels[task.status] || task.status) : '显式提交'}</p></div>
      <div className="sf-post-fields">
        <div className="sf-post-field"><label htmlFor={engineId}>声音引擎</label><select id={engineId} value={saved.engine} disabled={Boolean(saved.pending)} onChange={event => { const engine = event.target.value as Engine; const next = capability?.engines?.[engine]; const modes = next?.modes || []; const emotionModes = next?.emotion_modes; persist({ ...saved, engine, mode: modes.includes(saved.mode) ? saved.mode : 'reference', emotion: emotionModes && emotionModes[saved.emotion.mode] === false ? { ...saved.emotion, mode: 'none' } : saved.emotion }); }}>{engineEntries.length ? engineEntries.map(([id, item]) => <option key={id} value={id}>{engineLabels[id as Engine] || id}{item.available ? '' : '（未就绪）'}</option>) : <option value={saved.engine}>{engineLabels[saved.engine]}</option>}</select></div>
        <div className="sf-post-field sf-post-field--wide"><label htmlFor={textId}>台词</label><textarea id={textId} rows={3} maxLength={2000} value={saved.text} disabled={Boolean(saved.pending)} placeholder="输入这段作品需要说的台词" onChange={event => persist({ ...saved, text: event.target.value })} /></div>
        <div className="sf-post-field"><label htmlFor={modeId}>声音来源</label><select id={modeId} value={saved.mode} disabled={Boolean(saved.pending)} onChange={event => persist({ ...saved, mode: event.target.value === 'design' ? 'design' : 'reference' })}><option value="reference">沿用参考声音</option>{canDesign && <option value="design">用文字设计新音色</option>}</select></div>
        {saved.mode === 'design'
          ? <div className="sf-post-field sf-post-field--wide"><label htmlFor={descriptionId}>音色描述</label><textarea id={descriptionId} rows={2} maxLength={2000} value={saved.voice_description} disabled={Boolean(saved.pending)} placeholder="例如：年长男性，声音低沉沙哑，语速舒缓、克制" onChange={event => persist({ ...saved, voice_description: event.target.value })} /></div>
          : <fieldset className="sf-post-field sf-post-field--wide" disabled={Boolean(saved.pending)}><MaterialSelect label="音色参考" kind="audio" value={saved.voice_reference} options={options} onChange={value => persist({ ...saved, voice_reference: value })} onBrowse={onBrowse} /></fieldset>}
      {vox && <p className="sf-post-hint">VoxCPM2 输出 48kHz，并固定随机编号，所以同样的参数可以复现；音色描述与情绪描述会合并成一段前缀说明。</p>}
        {!auk && <EmotionControls emotion={saved.emotion} modes={engineCapability?.emotion_modes} unsupported={engineCapability?.emotion_unsupported} options={options} locked={Boolean(saved.pending)} onChange={emotion => persist({ ...saved, emotion })} onBrowse={onBrowse}
          subtitle={vox ? '可选 · 只用文字描述，和音色说明一起作为前缀' : undefined}
          textNote={vox ? 'VoxCPM2 把情绪描述写在台词前的括号说明里，不经过本机情绪模型；描述得越具体，语气变化越明显。' : undefined} />}
        {(vox || auk) && <div className="sf-post-field"><label htmlFor={seedId}>随机编号</label><input id={seedId} type="number" min={0} max={4294967295} step={1} disabled={Boolean(saved.pending)} value={saved.pending ? saved.pending.seed ?? '' : saved.seed} placeholder="沿用原提交参数" onChange={event => persist({ ...saved, seed: Number(event.target.value) })} /></div>}
        {auk && <><div className="sf-post-field"><label htmlFor={durationId}>请求时长（秒）</label><input id={durationId} type="number" min={0} max={30} step="any" value={saved.pending ? saved.pending.gen_seconds ?? '' : saved.gen_seconds} placeholder="沿用原提交参数" disabled={Boolean(saved.pending)} onChange={event => persist({ ...saved, gen_seconds: Number(event.target.value) })} /></div>
        </>}
      </div>
      <p className="sf-post-hint">{vox ? 'VoxCPM2 的时长由台词决定，不接收请求时长。' : auk ? '参考音频与请求时长合计须不超过 30 秒；请求时长不保证台词完整。' : 'IndexTTS 2.5 只沿用参考音色，时长由台词决定。'}</p>
      <p className="sf-post-hint">生成后先试听，再在分镜中选择。现有分镜音频会保留。</p>
      {!engineCapability?.available && <p className="sf-post-message" role="status" aria-live="polite">{engineCapability ? `${engineLabels[saved.engine]} 未就绪：${(engineCapability.missing || []).join('、') || engineCapability.reason || '缺少运行环境。'}` : capability ? '正在检查声音环境…' : '正在检查声音环境…'}{anotherEngineReady && ' 其他引擎已就绪，可在上方切换。'}</p>}
      {message && <p className="sf-post-message" role="status" aria-live="polite">{message}</p>}
      {task?.error && <p className="sf-post-message" role="status" aria-live="polite">{task.error}</p>}
      <div className="sf-post-actions">
        {!saved.pending && <button className="button primary compact" disabled={busy || !engineCapability?.available || !saved.text.trim() || (saved.mode === 'design' ? !saved.voice_description.trim() : !saved.voice_reference) || !emotionReady(saved.emotion) || ((vox || auk) && (!Number.isInteger(saved.seed) || saved.seed < 0 || saved.seed > 4294967295)) || (auk && (!Number.isFinite(saved.gen_seconds) || saved.gen_seconds <= 0 || saved.gen_seconds > 30))} onClick={() => void act('submit')}>生成声音候选</button>}
        {saved.pending && !task && <><button className="button secondary compact" disabled={busy} onClick={() => void act('check')}>核对声音提交</button><button className="button secondary compact" disabled={busy || !engineCapability?.available} onClick={() => void act('submit')}>使用原凭据重试</button></>}
        {task && !terminal.includes(task.status) && task.status !== 'needs_reconcile' && <button className="button secondary compact" disabled={busy} onClick={() => void act('stop')}>停止声音任务</button>}
        {task?.status === 'needs_reconcile' && <button className="button secondary compact" disabled={busy} onClick={() => void act('reconcile')}>核对声音结果</button>}
        {task && terminal.includes(task.status) && <button className="button secondary compact" disabled={busy} onClick={() => { if (persist({ ...saved, pending: undefined })) setTask(null); }}>准备下一条台词</button>}
        {task && <TaskQueueStatus task={{ ...task, asset_id: '声音' }} />}
        {task && <span className="sf-post-status">{labels[task.status] || task.status}</span>}
      </div>
    </section>
  </section>;
}
