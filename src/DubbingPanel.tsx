import { useEffect, useId, useRef, useState } from 'react';
import CreativeSettingsBar from './CreativeSettingsBar';
import EmotionControls, { emotionPayload, emotionReady, noEmotion, readEmotion, type Emotion } from './EmotionControls';
import TaskQueueStatus, { type QueueReceipt } from './TaskQueueStatus';
import MaterialSelect, { type MaterialOption } from './MaterialSelect';
import { ApiError, mediaUrl, request as apiRequest } from './api/client';
import { parseSubtitles } from './subtitles';
import { consumeBringIn, onBringIn } from './libraryBus';

type VideoAsset = { id: string; title: string };
type Cue = { at_seconds: number; text: string; gain?: number };
type Engine = 'indextts-2.5' | 'voxcpm-2';
type Draft = { source_asset_id: string; engine: Engine; timbre: 'reference' | 'design'; voice_reference: string; voice_description: string; seed: number; cues: Cue[]; original_audio: 'keep' | 'mute'; original_gain: number; emotion: Emotion };
type Submission = { source_asset_id: string; engine: Engine; cues: Array<{ at_seconds: number; text: string; gain: number }>; voice_reference: string | null; voice_description: string | null; seed: number; original_audio: 'keep' | 'mute'; original_gain: number; emotion: Record<string, unknown> | null; idempotency_key: string };
type Saved = { draft: Draft; pending?: Submission; completedTaskId?: string };
type Task = { id: string; status: string; queue?: QueueReceipt['queue']; error?: string | null; result?: unknown };
type EngineCapability = { available: boolean; missing?: string[]; note?: string; reason?: string; modes?: string[]; timbre?: string; emotion_modes?: Record<string, boolean>; emotion_unsupported?: string[] };
type Capability = EngineCapability & { default_engine?: Engine; engines?: Record<string, EngineCapability> };
type Receipt = { output?: string; audio_output?: string; duration_seconds?: number; timeline?: Array<{ at_seconds: number; text: string; duration_seconds: number; truncated: boolean }> };

const CUE_LIMIT = 24;
const engineLabels: Record<Engine, string> = { 'indextts-2.5': 'IndexTTS 2.5', 'voxcpm-2': 'VoxCPM2' };
const initial: Draft = { source_asset_id: '', engine: 'voxcpm-2', timbre: 'reference', voice_reference: '', voice_description: '', seed: 20261025, cues: [{ at_seconds: 0, text: '' }], original_audio: 'keep', original_gain: 1, emotion: { ...noEmotion } };
const terminal = ['succeeded', 'failed', 'stopped'];
const pollTerminal = [...terminal, 'needs_reconcile'];
const labels: Record<string, string> = { scheduler_waiting: '已接受 · 等待资源', batch_waiting: '已接受 · 等待依赖', submitting: '正在生成', running: '正在生成', succeeded: '已生成 · 待检查',
  failed: '生成失败', stopped: '已停止', needs_reconcile: '需要核对结果', stop_requested: '正在停止', stopping: '正在停止' };

function cleanCues(value: unknown): Cue[] {
  if (!Array.isArray(value)) return [{ at_seconds: 0, text: '' }];
  const cues = value.filter((cue): cue is Cue => Boolean(cue) && typeof cue === 'object'
    && typeof (cue as Cue).at_seconds === 'number' && typeof (cue as Cue).text === 'string')
    .slice(0, CUE_LIMIT).map(cue => ({ at_seconds: cue.at_seconds, text: cue.text }));
  return cues.length ? cues : [{ at_seconds: 0, text: '' }];
}
function load(storageKey: string): Saved {
  try {
    const value = JSON.parse(localStorage.getItem(storageKey) || '{}');
    const draft = value.draft && typeof value.draft === 'object' ? {
      source_asset_id: typeof value.draft.source_asset_id === 'string' ? value.draft.source_asset_id : '',
      engine: value.draft.engine === 'indextts-2.5' ? 'indextts-2.5' as const : 'voxcpm-2' as const,
      timbre: value.draft.timbre === 'design' ? 'design' as const : 'reference' as const,
      voice_reference: typeof value.draft.voice_reference === 'string' ? value.draft.voice_reference : '',
      voice_description: typeof value.draft.voice_description === 'string' ? value.draft.voice_description : '',
      seed: Number.isInteger(value.draft.seed) && value.draft.seed >= 0 && value.draft.seed <= 4294967295 ? value.draft.seed : 20261025,
      cues: cleanCues(value.draft.cues),
      original_audio: value.draft.original_audio === 'mute' ? 'mute' as const : 'keep' as const,
      original_gain: typeof value.draft.original_gain === 'number' && Number.isFinite(value.draft.original_gain)
        && value.draft.original_gain >= 0 && value.draft.original_gain <= 3 ? value.draft.original_gain : 1,
      emotion: readEmotion(value.draft.emotion),
    } : { ...initial, cues: [{ at_seconds: 0, text: '' }], emotion: { ...noEmotion } };
    const pending = value.pending && typeof value.pending.idempotency_key === 'string' ? value.pending : undefined;
    return { draft, pending, completedTaskId: typeof value.completedTaskId === 'string' ? value.completedTaskId : undefined };
  } catch { return { draft: { ...initial, cues: [{ at_seconds: 0, text: '' }], emotion: { ...noEmotion } } }; }
}
// A stored draft from the IndexTTS-only days never chose a timbre source: the
// reference it filled in stays the choice.
function body(draft: Draft): Omit<Submission, 'idempotency_key'> {
  const useDescription = draft.engine === 'voxcpm-2' && draft.timbre === 'design';
  return { source_asset_id: draft.source_asset_id, engine: draft.engine, cues: draft.cues.map(cue => ({ at_seconds: cue.at_seconds, text: cue.text.trim(), gain: 1 })),
    voice_reference: useDescription ? null : draft.voice_reference, voice_description: useDescription ? draft.voice_description.trim() : null,
    seed: draft.seed, original_audio: draft.original_audio, original_gain: draft.original_gain, emotion: emotionPayload(draft.emotion) };
}
function valid(draft: Draft, videos: VideoAsset[]): boolean {
  if (!videos.some(video => video.id === draft.source_asset_id)) return false;
  if (draft.engine === 'indextts-2.5' ? !draft.voice_reference
    : (draft.timbre === 'design' ? !draft.voice_description.trim() : !draft.voice_reference)) return false;
  if (!Number.isInteger(draft.seed) || draft.seed < 0 || draft.seed > 4294967295) return false;
  if (!Number.isFinite(draft.original_gain) || draft.original_gain < 0 || draft.original_gain > 3) return false;
  if (!emotionReady(draft.emotion)) return false;
  if (draft.cues.length < 1 || draft.cues.length > CUE_LIMIT) return false;
  return draft.cues.every(cue => Number.isFinite(cue.at_seconds) && cue.at_seconds >= 0 && cue.at_seconds < 86400
    && cue.text.trim().length >= 1 && cue.text.length <= 500);
}
class RequestError extends Error { constructor(message: string, readonly status: number) { super(message); } }

export default function DubbingPanel({ projectId, options, videos, onBrowse, onComplete }: {
  projectId: string; options: MaterialOption[]; videos: VideoAsset[]; onBrowse: () => void; onComplete: () => void;
}) {
  const storageKey = `director-workbench:${projectId}:dubbing:v1`;
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
  // While a submission is pending the form is locked, so the draft still holds
  // exactly what was sent; rendering from it keeps one shape for the fields.
  const draft = saved.draft;
  const locked = Boolean(saved.pending);
  const engineCapability = capability?.engines?.[draft.engine] ?? capability;
  const engineReady = Boolean(engineCapability?.available);
  const engineEntries: Array<[Engine, EngineCapability | null]> = capability?.engines
    ? Object.entries(capability.engines) as Array<[Engine, EngineCapability]>
    : (Object.keys(engineLabels) as Engine[]).map(key => [key, key === draft.engine ? capability : null] as [Engine, EngineCapability | null]);
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
  // 音色库 → 锚点配音：只有点「带入」才会走这里，自动应用一次后即清空。
  useEffect(() => {
    const apply = (params: Record<string, unknown>) => {
      if (current.current.pending) return;
      const description = typeof params.voice_description === 'string' ? params.voice_description.trim() : '';
      const reference = typeof params.voice_reference === 'string' ? params.voice_reference : '';
      const engine = description ? 'voxcpm-2' as const : (params.engine === 'indextts-2.5' ? 'indextts-2.5' as const : 'voxcpm-2' as const);
      persist({ ...current.current, draft: { ...current.current.draft, engine,
        timbre: description ? 'design' as const : 'reference' as const,
        voice_description: description || current.current.draft.voice_description,
        voice_reference: reference || current.current.draft.voice_reference } });
      setMessage('已从音色库带入音色；生成前请确认台词、时间点和参考音频。');
    };
    const queued = consumeBringIn('timbre');
    if (queued) apply(queued);
    return onBringIn(detail => { if (detail.kind === 'timbre') apply(detail.params); });
  }, []);
  useEffect(() => {
    alive.current = true;
    fetch('/api/dubbing-capability').then(response => {
      if (!response.ok) throw new Error('能力检查失败');
      return response.json();
    }).then(data => { if (alive.current) setCapability(data); })
      .catch(() => { if (alive.current) setCapability({ available: false, reason: '无法检查锚点配音环境，请检查连接后刷新。' }); });
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
    let key = saved.pending?.idempotency_key;
    try {
      let next: Task;
      if (action === 'submit') {
        const submission = current.current.pending || {
          ...body(current.current.draft),
          idempotency_key: `dubbing-${crypto.randomUUID()}`,
        };
        if (!valid(current.current.draft, videos)) throw new Error('请确认视频、音色参考、情绪设置和每句台词的时间与内容。');
        if (!persist({ ...current.current, pending: submission })) return;
        key = submission.idempotency_key;
        next = await request(`${base}/dubbing-tasks`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(submission) });
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
  // A voice setting carries the engine, the timbre source and the original-track policy:
  // which video is dubbed and what the lines say stay per-task decisions.
  const voiceValues = { engine: draft.engine,
    ...(draft.engine === 'voxcpm-2' && draft.timbre === 'design' ? { voice_description: draft.voice_description } : { voice_reference: draft.voice_reference }),
    seed: draft.seed, original_audio: draft.original_audio, original_gain: draft.original_gain, emotion: emotionPayload(draft.emotion) };
  function applyVoiceSetting(params: Record<string, unknown>) {
    const description = typeof params.voice_description === 'string' ? params.voice_description : '';
    persist({ ...current.current, draft: { ...current.current.draft,
      engine: params.engine === 'indextts-2.5' || params.engine === 'voxcpm-2' ? params.engine : current.current.draft.engine,
      timbre: description ? 'design' : 'reference',
      voice_reference: typeof params.voice_reference === 'string' ? params.voice_reference : current.current.draft.voice_reference,
      voice_description: description,
      seed: Number.isInteger(params.seed) && (params.seed as number) >= 0 && (params.seed as number) <= 4294967295
        ? params.seed as number : current.current.draft.seed,
      original_audio: params.original_audio === 'mute' ? 'mute' : 'keep',
      original_gain: typeof params.original_gain === 'number' ? params.original_gain : current.current.draft.original_gain,
      emotion: readEmotion(params.emotion) } });
  }
  // 字幕导入只搬时间轴和文字；位置有问题由后端在提交时点名，不在前端猜。
  async function importSubtitles(file: File) {
    if (current.current.pending) return;
    try {
      const parsed = parseSubtitles(await file.text(), CUE_LIMIT);
      if (parsed.error) { setMessage(parsed.error); return; }
      persist({ ...current.current, draft: { ...current.current.draft,
        cues: parsed.cues.map(cue => ({ at_seconds: cue.at_seconds, text: cue.text })) } });
      setMessage(`已从「${file.name}」导入 ${parsed.cues.length} 句${parsed.dropped ? `；超出 ${CUE_LIMIT} 句上限的 ${parsed.dropped} 句没有导入` : ''}。开始时间请对照视频确认。`);
    } catch (reason) {
      setMessage(reason instanceof Error ? `字幕读取失败：${reason.message}` : '字幕读取失败');
    }
  }
  function changeCue(index: number, patch: Partial<Cue>) {
    const cues = current.current.draft.cues.map((cue, position) => position === index ? { ...cue, ...patch } : cue);
    change('cues', cues);
  }
  function addCue() {
    const cues = current.current.draft.cues;
    if (cues.length >= CUE_LIMIT) return;
    const last = cues[cues.length - 1];
    change('cues', [...cues, { at_seconds: Math.min(86400, (last?.at_seconds || 0) + 2), text: '' }]);
  }
  function removeCue(index: number) {
    const cues = current.current.draft.cues.filter((_, position) => position !== index);
    if (cues.length) change('cues', cues);
  }
  return <section className="sf-post-tools" aria-label="锚点配音">
    <section className="sf-post-section" aria-label="锚点配音设置">
      <div className="sf-post-section-head"><h2 className="sf-post-title">锚点配音</h2><p className="sf-post-status">{task ? (labels[task.status] || task.status) : '视频生成后配音'}</p></div>
      <p className="sf-post-hint">选一个已生成的视频，为每句台词填写开始时间。视频画面原样保留；原片音轨可保留（可调音量）或静音。输出配音视频和完整音轨两个候选，不会自动拆分或采用。</p>
      <CreativeSettingsBar kind="dubbing" values={voiceValues} onApply={applyVoiceSetting} />
      <div className="sf-post-fields">
        <div className="sf-post-field"><label htmlFor={`${prefix}-video`}>视频</label><select id={`${prefix}-video`} value={draft.source_asset_id} disabled={locked} onChange={event => change('source_asset_id', event.target.value)}>
          <option value="">选择视频</option>{videos.map(video => <option key={video.id} value={video.id}>{video.title}</option>)}</select></div>
        <div className="sf-post-field"><label htmlFor={`${prefix}-engine`}>配音引擎</label><select id={`${prefix}-engine`} value={draft.engine} disabled={locked} onChange={event => { const engine = event.target.value === 'indextts-2.5' ? 'indextts-2.5' as const : 'voxcpm-2' as const; const modes = capability?.engines?.[engine]?.emotion_modes; persist({ ...current.current, draft: { ...current.current.draft, engine, timbre: engine === 'indextts-2.5' ? 'reference' : draft.timbre, emotion: modes && modes[draft.emotion.mode] === false ? { ...draft.emotion, mode: 'none' } : draft.emotion } }); }}>
          {engineEntries.map(([engine, item]) => <option key={engine} value={engine}>{engineLabels[engine]}{item && item.available === false ? '（未就绪）' : ''}</option>)}</select></div>
        <div className="sf-post-field"><label htmlFor={`${prefix}-timbre`}>音色来源</label><select id={`${prefix}-timbre`} value={draft.engine === 'indextts-2.5' ? 'reference' : draft.timbre} disabled={locked || draft.engine === 'indextts-2.5'} onChange={event => change('timbre', event.target.value === 'design' ? 'design' : 'reference')}>
          <option value="reference">沿用参考音色</option>{draft.engine === 'voxcpm-2' && <option value="design">用文字设计新音色</option>}</select></div>
        <div className="sf-post-field"><label htmlFor={`${prefix}-audio`}>原片音轨</label><select id={`${prefix}-audio`} value={draft.original_audio} disabled={locked} onChange={event => change('original_audio', event.target.value === 'mute' ? 'mute' : 'keep')}>
          <option value="keep">保留原片音轨</option><option value="mute">静音，仅保留配音</option></select></div>
        {draft.original_audio === 'keep' && <div className="sf-post-field"><label htmlFor={`${prefix}-gain`}>原片音量（倍）</label><input id={`${prefix}-gain`} type="number" min={0} max={3} step="any" disabled={locked} value={draft.original_gain} onChange={event => change('original_gain', Number(event.target.value))} /></div>}
        {draft.engine === 'voxcpm-2' && <div className="sf-post-field"><label htmlFor={`${prefix}-seed`}>随机编号</label><input id={`${prefix}-seed`} type="number" min={0} max={4294967295} step={1} disabled={locked} value={locked ? saved.pending?.seed ?? '' : draft.seed} onChange={event => change('seed', Number(event.target.value))} /></div>}
        {draft.timbre === 'design' && draft.engine === 'voxcpm-2'
          ? <div className="sf-post-field sf-post-field--wide"><label htmlFor={`${prefix}-description`}>音色描述</label><textarea id={`${prefix}-description`} rows={2} maxLength={2000} disabled={locked} value={draft.voice_description} placeholder="例如：年轻女性，音色清亮偏软，语速平稳" onChange={event => change('voice_description', event.target.value)} /></div>
          : <fieldset className="sf-post-field sf-post-field--wide" disabled={locked}><MaterialSelect label="音色参考" kind="audio" value={draft.voice_reference} options={options} onChange={value => change('voice_reference', value)} onBrowse={onBrowse} /></fieldset>}
      </div>
      {draft.engine === 'voxcpm-2' && <p className="sf-post-hint">VoxCPM2 输出 48kHz；同一个随机编号 + 同一段音色描述，才会回到同一个音色。每条台词沿用同一个音色来源。{draft.timbre === 'design' ? ' 音色描述与情绪描述会合并写在台词前。' : ''}</p>}
      <EmotionControls emotion={draft.emotion} modes={engineCapability?.emotion_modes} unsupported={engineCapability?.emotion_unsupported} options={options} locked={locked} onChange={emotion => change('emotion', emotion)} onBrowse={onBrowse}
        subtitle={draft.engine === 'voxcpm-2' ? '可选 · VoxCPM2 只用文字描述情绪' : undefined}
        textNote={draft.engine === 'voxcpm-2' ? 'VoxCPM2 把情绪描述写在台词前的括号说明里，不经过本机情绪模型。' : undefined} />
      <div className="sf-post-subblock"><div className="sf-post-section-head"><h3 className="sf-post-subtitle">台词与时间点</h3><p className="sf-post-meta">{draft.cues.length} / {CUE_LIMIT} 句</p></div>
        {!locked && <div className="sf-post-actions">
          <label className="button secondary compact sf-file-button">从字幕导入（SRT / VTT）
            <input type="file" accept=".srt,.vtt,text/plain" hidden onChange={event => { const file = event.target.files?.[0]; if (file) void importSubtitles(file); event.target.value = ''; }} />
          </label>
        </div>}
        {draft.cues.map((cue, index) => <div className="sf-post-fields" key={index}>
          <div className="sf-post-field"><label htmlFor={`${prefix}-at-${index}`}>第 {index + 1} 句开始（秒）</label><input id={`${prefix}-at-${index}`} type="number" min={0} max={86400} step="any" disabled={locked} value={cue.at_seconds} onChange={event => changeCue(index, { at_seconds: Number(event.target.value) })} /></div>
          <div className="sf-post-field sf-post-field--wide"><label htmlFor={`${prefix}-text-${index}`}>台词</label><textarea id={`${prefix}-text-${index}`} rows={2} maxLength={500} disabled={locked} value={cue.text} placeholder="输入这句要说的话" onChange={event => changeCue(index, { text: event.target.value })} /></div>
          {!locked && draft.cues.length > 1 && <button className="button secondary compact" type="button" aria-label={`删除第 ${index + 1} 句`} onClick={() => removeCue(index)}>删除这句</button>}
        </div>)}
        {!locked && draft.cues.length < CUE_LIMIT && <div className="sf-post-actions"><button className="button secondary compact" type="button" onClick={addCue}>添加一句</button></div>}
      </div>
      <p className="sf-post-hint">开始时间必须早于视频结束；每句最长 500 字。超出片尾的台词会被截断并标注。</p>
      {engineCapability && !engineReady && <p className="sf-post-message" role="status" aria-live="polite">{engineLabels[draft.engine]} 配音未就绪：{(engineCapability.missing || []).join('、') || engineCapability.reason || '缺少运行环境。'}</p>}
      {message && <p className="sf-post-message" role="status" aria-live="polite">{message}</p>}{task?.error && <p className="sf-post-message" role="status" aria-live="polite">{task.error}</p>}
      {result?.output && <div className="sf-post-subblock">
        <video controls preload="metadata" aria-label="锚点配音候选视频" src={mediaUrl(result.output)} />
        {result.audio_output && <audio controls preload="metadata" aria-label="锚点配音完整音轨" src={mediaUrl(result.audio_output)} />}
        <p className="sf-post-hint">配音候选 {result.duration_seconds ?? '未知'} 秒 · {result.timeline?.length ?? 0} 句{result.timeline?.some(cue => cue.truncated) ? ' · 有台词超出片尾已截断' : ''}。试听检查后再在分镜中选择。</p>
      </div>}
      <div className="sf-post-actions">
        <button className="button secondary compact" type="button" onClick={onBrowse}>添加素材</button>
        {!locked && <button className="button primary compact" disabled={busy || !engineReady || !valid(draft, videos)} onClick={() => void act('submit')}>生成配音候选</button>}
        {locked && !task && <><button className="button secondary compact" disabled={busy} onClick={() => void act('check')}>核对配音提交</button><button className="button secondary compact" disabled={busy || !engineReady} onClick={() => void act('submit')}>使用原凭据重试</button></>}
        {task && !pollTerminal.includes(task.status) && <button className="button secondary compact" disabled={busy} onClick={() => void act('stop')}>停止配音任务</button>}
        {task?.status === 'needs_reconcile' && <button className="button secondary compact" disabled={busy} onClick={() => void act('reconcile')}>核对配音结果</button>}
        {task && terminal.includes(task.status) && <button className="button secondary compact" disabled={busy} onClick={() => { if (persist({ ...current.current, pending: undefined })) { setTask(null); setMessage(''); } }}>继续配音</button>}
        {task && <TaskQueueStatus task={{ ...task, asset_id: '锚点配音' }} />}
      </div>
    </section>
  </section>;
}
