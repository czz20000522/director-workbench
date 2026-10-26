import { useEffect, useId, useRef, useState } from 'react';
import TaskQueueStatus, { type QueueReceipt } from './TaskQueueStatus';
import ProgressBar, { type TaskProgress } from './TaskProgress';
import ResolutionPicker, { type OutputResolution } from './ResolutionPicker';
import { detectQualityIntent } from './qualityIntent';
import { BookOpen } from 'lucide-react';
import { PromptLibraryDialog } from './ReferenceLibrary';
import MaterialSelect, { type MaterialOption } from './MaterialSelect';
import { ApiError, request as apiRequest } from './api/client';

type Mode = 'text' | 'reference';
type CreativeProfile = 'general' | 'adult-realism' | 'adult-anime';
type Submission = { mode?: Mode; creative_profile?: CreativeProfile; width?: number; height?: number; output_resolution?: OutputResolution; prompt: string; reference_image?: string; seed: number; idempotency_key: string };
type Task = { id: string; status: string; queue?: QueueReceipt['queue']; progress?: TaskProgress; error?: string; result?: { output?: string; native_output?: string; published_native_output?: string; width?: number; height?: number; output_size?: { resolution?: OutputResolution; width?: number; height?: number; method?: string; note?: string } }; payload?: { request?: Submission; output_size?: { resolution?: OutputResolution; width?: number; height?: number; method?: string } } };
type Saved = { mode: Mode; creative_profile: CreativeProfile; prompt: string; reference_image: string; seed: number; width?: number; height?: number; output_resolution?: OutputResolution; pending?: Submission };
class RequestError extends Error {
  constructor(message: string, readonly status: number) { super(message); }
}
const terminal = ['succeeded', 'failed', 'stopped'];
const labels: Record<string, string> = { scheduler_waiting: '已接受 · 等待资源', batch_waiting: '已接受 · 等待依赖', submitting: '正在提交', queued: '排队中', running: '生成中', succeeded: '已生成 · 待审阅', failed: '生成失败', stopped: '已停止', stop_requested: '正在停止', stopping: '正在停止', needs_reconcile: '需要核对结果' };
const resolutionLabel = (value?: OutputResolution) => value && value !== 'native' ? value.toUpperCase() : '原生';
const methodLabel = (value?: string) => value === 'lanczos_resize' ? '尺寸放大（Lanczos）' : value === 'ai_upscale' ? 'AI 高清处理' : '原生生成';
// 随机编号写死会让每次新生成都高度雷同：默认每次随机，要复现同一张图时在更多设置里钉住。
const randomSeed = () => crypto.getRandomValues(new Uint32Array(1))[0];
// 1K is the native model output; the larger tiers are Lanczos resizes of it.
const clarityOptions: { value: OutputResolution; label: string; note: string }[] = [
  { value: 'native', label: '1K', note: '原生' },
  { value: '2k', label: '2K', note: '放大' },
  { value: '4k', label: '4K', note: '放大' },
  { value: '8k', label: '8K', note: '放大' },
];

export default function KeyframePanel({ projectId, options, onBrowse, onComplete, simple = false, initialMode = 'reference', candidateTaskId, creativeProfile, defaultOpen = false, fresh = false, initialPrompt }: {
  projectId: string; options: MaterialOption[]; onBrowse: () => void; onComplete: (taskId: string) => void;
  simple?: boolean; initialMode?: Mode; candidateTaskId?: string; creativeProfile?: CreativeProfile; defaultOpen?: boolean; fresh?: boolean; initialPrompt?: string;
}) {
  // The shell now renders every post tool expanded; keep accepting the legacy
  // prop without letting it collapse the form again.
  void defaultOpen;
  const storageKey = `director-workbench:${projectId}:keyframe:v1`;
  const promptId = useId();
  const modeId = useId();
  const [saved, setSaved] = useState<Saved>(() => {
    // 单步工具每次进入都是一次新的实验：不继承上一次的草稿，也不继承可能已随
    // 任务一起被删除的提交凭据，否则旧凭据会把新表单永久锁住。
    if (fresh) return { mode: initialMode, creative_profile: creativeProfile ?? 'general', prompt: initialPrompt ?? '', reference_image: '', width: 768, height: 1344, output_resolution: 'native', seed: randomSeed() };
    try {
      const value = JSON.parse(localStorage.getItem(storageKey) || '{}');
      return { mode: value.pending ? (value.pending.mode === 'text' ? 'text' : 'reference') : value.mode === 'text' || value.mode === 'reference' ? value.mode : initialMode, creative_profile: value.creative_profile === 'adult-realism' || value.creative_profile === 'adult-anime' ? value.creative_profile : (creativeProfile ?? 'general'), prompt: typeof value.prompt === 'string' ? value.prompt : '', reference_image: typeof value.reference_image === 'string' ? value.reference_image : '',
        width: value.width ?? 768, height: value.height ?? 1344, output_resolution: value.output_resolution ?? 'native',
        seed: Number.isInteger(value.seed) && value.seed >= 0 && value.seed <= 4294967295 ? value.seed : randomSeed(),
        pending: value.pending && typeof value.pending.idempotency_key === 'string' && typeof value.pending.prompt === 'string' && (value.pending.mode === 'text' || typeof value.pending.reference_image === 'string') && Number.isInteger(value.pending.seed) ? value.pending : undefined };
    } catch { return { mode: initialMode, creative_profile: creativeProfile ?? 'general', prompt: '', reference_image: '', seed: randomSeed() }; }
  });
  const [task, setTask] = useState<Task | null>(null);
  const pendingKey = useRef(saved.pending?.idempotency_key);
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);
  const [libraryOpen, setLibraryOpen] = useState(false);
  const [capability, setCapability] = useState<{ mode: Mode; available: boolean; engine_offline?: boolean; missing_nodes?: string[]; missing_models?: string[]; reason?: string; requires_reference_image?: boolean } | null>(null);
  const [batchOpen, setBatchOpen] = useState(false);
  const [batchText, setBatchText] = useState('');
  const [batchReport, setBatchReport] = useState('');
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
      completed.current = next.id; completion.current(next.id);
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
    return () => { mounted.current = false; };
  }, []);
  useEffect(() => {
    let active = true;
    setCapability(null);
    fetch(`/api/keyframe-capability?mode=${saved.mode}&creative_profile=${encodeURIComponent(saved.creative_profile)}`).then(response => {
      if (!response.ok) throw new Error('能力检查失败');
      return response.json();
    }).then(data => {
      if (active) setCapability({ ...data, mode: saved.mode });
    }).catch(() => {
      if (active) setCapability({ mode: saved.mode, available: false, reason: '无法检查生成环境，请检查连接后刷新。' });
    });
    return () => { active = false; };
  }, [saved.mode, saved.creative_profile]);
  useEffect(() => {
    if (!creativeProfile || saved.pending || saved.creative_profile === creativeProfile) return;
    persist({ ...saved, creative_profile: creativeProfile });
  }, [creativeProfile, saved.pending?.idempotency_key, saved.creative_profile]);
  useEffect(() => {
    // A confirmed terminal result no longer needs the receipt to guard the form.
    // Keeping it would leave the prompt read-only after every generation.
    if (!saved.pending || !task || !terminal.includes(task.status)) return;
    persist({ ...saved, pending: undefined });
  }, [task?.id, task?.status, saved.pending?.idempotency_key]);
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
        // Reuse the receipt only while that submission is still unresolved; after a
        // terminal result the textarea is editable again, so a new run needs a new key.
        const retry = saved.pending && (!task || !terminal.includes(task.status)) ? saved.pending : undefined;
        const pending: Submission = retry || { mode: saved.mode, creative_profile: saved.creative_profile, prompt: saved.prompt.trim(), ...(saved.mode === 'text' ? { width: saved.width ?? 768, height: saved.height ?? 1344, output_resolution: appliedResolution } : { reference_image: saved.reference_image }), seed: saved.seed, idempotency_key: `keyframe-${crypto.randomUUID()}` };
        if (!retry && task) setTask(null);
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
          persist({ ...saved, pending: undefined });
        }
        setMessage(error instanceof Error ? error.message : '请求未获确认，请核对原任务。');
      }
    }
    finally { working.current = false; if (mounted.current) setBusy(false); }
  }
  // 每行一条提示词，各自排队；显卡只有一张，执行器会按顺序跑完。
  async function submitBatch() {
    if (working.current) return;
    const prompts = batchText.split('\n').map(line => line.trim()).filter(Boolean).slice(0, 12);
    if (!prompts.length) { setMessage('先写下至少一条提示词，每行一条。'); return; }
    if (!(engineReady && capability?.mode === saved.mode)) { setMessage('图片引擎还没就绪，批量排队已停下。'); return; }
    working.current = true; setBusy(true); setBatchReport('');
    let queued = 0;
    const failed: string[] = [];
    try {
      for (const prompt of prompts) {
        try {
          await request(`${base}/keyframe-tasks`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ mode: 'text', creative_profile: saved.creative_profile, prompt,
              width: saved.width ?? 768, height: saved.height ?? 1344,
              output_resolution: saved.output_resolution ?? 'native', seed: saved.seed,
              idempotency_key: `keyframe-${crypto.randomUUID()}` }),
          });
          queued += 1;
        } catch (error) {
          failed.push(`${prompt.slice(0, 14)}：${error instanceof Error ? error.message : '未获确认'}`);
        }
      }
    } finally {
      working.current = false;
      if (mounted.current) {
        setBusy(false);
        setBatchReport(`已排队 ${queued} 条${failed.length ? `；${failed.length} 条没排上（${failed.slice(0, 3).join('；')}）` : ''}。进度在任务列表里。`);
        if (queued) { setBatchText(''); completion.current(''); }
      }
    }
  }
  async function editCandidate() {
    if (!candidateTaskId || working.current) return;
    working.current = true; setBusy(true);
    try {
      const candidate = await request(`${base}/tasks/${encodeURIComponent(candidateTaskId)}`);
      if (!mounted.current) return;
      const input = candidate.payload?.request;
      if (!input?.prompt) throw new Error('这个候选缺少生成提示词，原草稿已保留。');
      if (persist({ mode: input.mode === 'text' ? 'text' : 'reference', creative_profile: (input.creative_profile === 'adult-realism' || input.creative_profile === 'adult-anime') ? input.creative_profile : (creativeProfile ?? 'general'), prompt: input.prompt, seed: input.seed, reference_image: input.reference_image ?? '', width: input.width, height: input.height, output_resolution: input.output_resolution })) {
        setTask(null); setMessage('已载入这张图的完整提示词。修改后生成新候选，原图保留。');
      }
    } catch (error) { if (mounted.current) setMessage(error instanceof Error ? error.message : '读取候选失败'); }
    finally { working.current = false; if (mounted.current) setBusy(false); }
  }
  // Only an unresolved submission locks the form; once the task is terminal the
  // prompt is a normal draft again and the next run gets a fresh idempotency key.
  const locked = Boolean(saved.pending) && (!task || !terminal.includes(task.status));
  // An idle engine is allowed to submit: the backend starts it before the task runs.
  const engineReady = Boolean(capability?.available || capability?.engine_offline);
  // 提示词里写明的画质优先于选择器；后端用同一份规则再校验一次。
  const promptIntent = detectQualityIntent(saved.prompt);
  const appliedResolution = saved.mode === 'text' && promptIntent ? promptIntent.resolution : saved.output_resolution ?? 'native';
  return <section className={simple ? 'sf-post-tools quick-image-form' : 'sf-post-tools'} aria-label="生成关键帧">
    <section className="sf-post-section" aria-label={simple ? '生成图片设置' : '生成关键帧设置'}>
      <div className="sf-post-section-head"><h2 className="sf-post-title">{simple ? '生成图片' : '生成关键帧'}</h2><p className="sf-post-status">{task ? (labels[task.status] || task.status) : '显式提交'}</p></div>
      <div className="sf-post-fields">
        <div className="sf-post-field sf-post-field--wide">
          <div className="sf-post-field-head"><label htmlFor={promptId}>{simple ? '图片提示词' : '画面描述'}</label><button type="button" className="sf-inline-link" disabled={locked} onClick={() => setLibraryOpen(true)}><BookOpen size={12} /> 从提示词库带入</button></div>
          <textarea id={promptId} rows={4} maxLength={6000} value={saved.prompt} disabled={locked} placeholder="描述角色、场景、姿态和构图" onChange={event => persist({ ...saved, prompt: event.target.value })} />
        </div>
        {saved.mode === 'reference' && <fieldset className="sf-post-field sf-post-field--wide" disabled={locked}><MaterialSelect label="参考图片" kind="image" value={saved.reference_image} options={options} onChange={value => persist({ ...saved, reference_image: value })} onBrowse={onBrowse} /></fieldset>}
        {simple && saved.mode === 'text' && <div className="sf-post-field sf-post-field--wide">
          <label>清晰度</label>
          <div className="clarity-levels" role="group" aria-label="图片清晰度">
            {clarityOptions.map(option => <button key={option.value} type="button" aria-pressed={(saved.output_resolution ?? 'native') === option.value} disabled={locked} onClick={() => persist({ ...saved, output_resolution: option.value })}><strong>{option.label}</strong><small>{option.note}</small></button>)}
          </div>
          <p className="sf-post-hint">当前 {saved.width ?? 768} × {saved.height ?? 1344}；2K、4K、8K 在生成后用 Lanczos 放大，不增加原生细节。</p>
        </div>}
        {(() => {
          // 单步入口只强制提示词一项；其余都有默认值，收进折叠区，幅面与输出规格各自单选。
          const advanced = <>
            {!simple && <div className="sf-post-field"><label htmlFor={modeId}>生成方式</label><select id={modeId} value={saved.mode} disabled={locked} onChange={event => persist({ ...saved, mode: event.target.value as Mode })}><option value="text">文字生图</option><option value="reference">参考图改图</option></select></div>}
            {saved.mode === 'text' && <div className="sf-post-field sf-post-field--wide"><ResolutionPicker label="图片" disabled={locked} aspect={saved.width === saved.height ? '1:1' : (saved.width ?? 768) > (saved.height ?? 1344) ? '16:9' : '9:16'} resolution={saved.output_resolution} nativeSize={`${saved.width ?? 768} × ${saved.height ?? 1344}`} onChange={(aspect, output_resolution) => { const [width, height] = aspect === '1:1' ? [1024, 1024] : aspect === '16:9' ? [1344, 768] : [768, 1344]; persist({ ...saved, width, height, output_resolution }); }} /></div>}
            <div className="sf-post-subblock sf-post-field--wide"><h3 className="sf-post-subtitle">随机编号</h3><div className="sf-post-field"><label>随机编号<input type="number" min={0} max={4294967295} step={1} value={saved.seed} disabled={locked} onChange={event => persist({ ...saved, seed: Number(event.target.value) })} /></label><button type="button" className="button secondary compact" disabled={locked} onClick={() => persist({ ...saved, seed: randomSeed() })}>换一个</button></div><p className="sf-post-hint">同一段提示词 + 同一个编号 = 同一张图；换编号就换一版。默认每次随机，平时不用管它；想复现某张图时，把那张图用过的编号填回来。</p></div>
          </>;
          return simple ? <details className="qt-advanced"><summary>更多设置 · 幅面、输出规格、随机编号</summary>{advanced}</details> : advanced;
        })()}
      </div>
      <p className="sf-post-hint">画质规则：提示词写明 1K / 2K / 4K / 8K（或高清 / 超高清）时，以提示词为准，优先于上面的选择。</p>
      {promptIntent && saved.mode === 'text' && <p className="sf-post-hint">提示词里写了「{promptIntent.phrase}」：本次按 {promptIntent.label} 输出。</p>}
      {promptIntent && saved.mode === 'reference' && <p className="sf-post-hint">提示词里写了「{promptIntent.phrase}」，但参考图模式固定按原图尺寸输出；需要放大请用「清晰化」。</p>}
      <p className="sf-post-hint">生成后可直接下载图片，也可以选作视频的开始画面。每次生成都会保留原图。</p>
      {(!engineReady || capability?.mode !== saved.mode) && <p className="sf-post-message" role="status" aria-live="polite">{!capability || capability.mode !== saved.mode ? '正在检查生成环境…' : capability.reason || '关键帧生成环境尚未就绪。'}</p>}
      {capability && !capability.available && !capability.engine_offline && <p className="sf-post-alert">缺少的资源：{[...(capability.missing_nodes || []), ...(capability.missing_models || [])].join("、") || "请检查生成服务连接。"}</p>}
      {capability?.engine_offline && <p className="sf-post-hint">生成引擎当前未运行；点生成会自动拉起，首次约 10–30 秒。</p>}
      {message && <p className="sf-post-message" role="status" aria-live="polite">{message}</p>}
      {task?.error && <p className="sf-post-message" role="status" aria-live="polite">{task.error}</p>}
      {task?.status === 'succeeded' && <div className="quick-candidate-meta" aria-label="图片候选信息">
        <div className="quick-candidate-meta-heading"><strong>当前图片候选</strong><span>已生成 · 待审阅</span></div>
        <dl><div><dt>输出规格</dt><dd>{resolutionLabel(task.result?.output_size?.resolution ?? task.payload?.request?.output_resolution)}{(task.result?.output_size?.width ?? task.result?.width) && (task.result?.output_size?.height ?? task.result?.height) ? ` · ${task.result?.output_size?.width ?? task.result?.width} × ${task.result?.output_size?.height ?? task.result?.height}` : ''}</dd></div><div><dt>处理方式</dt><dd>{methodLabel(task.result?.output_size?.method ?? task.payload?.output_size?.method)}</dd></div><div><dt>画面编号</dt><dd>{task.payload?.request?.seed ?? '—'}</dd></div><div><dt>输入快照</dt><dd>完整提示词与参数已保留</dd></div></dl>
        {(task.result?.native_output || task.result?.published_native_output) && <p className="sf-post-hint">原生文件已保留；当前显示的是处理后的图片候选。</p>}
      </div>}
      {batchOpen && saved.mode === 'text' && <div className="sf-post-subblock" aria-label="批量排队">
        <div className="sf-post-section-head"><h3 className="sf-post-subtitle">批量排队生成</h3><p className="sf-post-meta">每行一条 · 最多 12 条</p></div>
        <div className="sf-post-field sf-post-field--wide"><label htmlFor={`${'${prefix}'}-batch`}>提示词列表</label>
          <textarea id={`${'${prefix}'}-batch`} rows={5} maxLength={12000} value={batchText} placeholder={'雨夜霓虹的街角\n清晨空荡的地铁站\n黄昏海边的旧灯塔'} onChange={event => setBatchText(event.target.value)} /></div>
        <div className="sf-post-actions">
          <button className="button primary compact" type="button" disabled={busy || !batchText.trim() || !(engineReady && capability?.mode === saved.mode)} onClick={() => void submitBatch()}>排队生成</button>
        </div>
        {batchReport && <p className="sf-post-message" role="status" aria-live="polite">{batchReport}</p>}
      </div>}
      <div className="sf-post-actions">
        {!locked && <button className="button primary compact" disabled={busy || (!engineReady || capability?.mode !== saved.mode) || !saved.prompt.trim() || (saved.mode === 'reference' && !saved.reference_image) || !Number.isInteger(saved.seed) || saved.seed < 0 || saved.seed > 4294967295} onClick={() => void act('submit')}>{simple ? (task ? '再生成一张' : '生成图片') : '生成关键帧候选'}</button>}
        {!locked && saved.mode === 'text' && <button className="button secondary compact" type="button" disabled={busy} onClick={() => { setBatchOpen(value => !value); setBatchReport(''); }}>{batchOpen ? '收起批量' : '批量排队'}</button>}
        {saved.pending && !task && <><button className="button secondary compact" disabled={busy} onClick={() => void act('check')}>核对关键帧提交</button><button className="button secondary compact" disabled={busy || (!engineReady || capability?.mode !== saved.mode)} onClick={() => void act('submit')}>使用原凭据重试</button><button className="button secondary compact" disabled={busy} onClick={() => { if (persist({ ...saved, pending: undefined })) { setTask(null); setMessage('已放弃这次提交凭据，可以重新填写。若原任务其实已落库，它会留在任务列表里。'); } }}>放弃核对，重新填写</button></>}
        {saved.pending && !task && <p className="sf-post-hint">长时间查不到，通常是因为原任务已被删除；放弃核对即可解锁表单，不会删掉任何已生成的图片。</p>}
        {task && !terminal.includes(task.status) && task.status !== 'needs_reconcile' && <button className="button secondary compact" disabled={busy} onClick={() => void act('stop')}>停止关键帧任务</button>}
        {task?.status === 'needs_reconcile' && <button className="button secondary compact" disabled={busy} onClick={() => void act('reconcile')}>核对关键帧结果</button>}
        {task && terminal.includes(task.status) && !simple && <button className="button secondary compact" disabled={busy} onClick={() => { if (persist({ ...saved, pending: undefined })) setTask(null); }}>准备下一张关键帧</button>}
        {simple && candidateTaskId && (!saved.pending || task && terminal.includes(task.status)) && <button className="button secondary compact" disabled={busy} onClick={() => void editCandidate()}>载入当前图片的提示词</button>}
        {task && <ProgressBar progress={task.progress} />}
      {task && <TaskQueueStatus task={{ ...task, asset_id: '关键帧' }} />}
        {task && <span className="sf-post-status">{labels[task.status] || task.status}</span>}
      </div>
    </section>
    {libraryOpen && <PromptLibraryDialog target="image" onUse={text => persist({ ...saved, prompt: text })} onClose={() => setLibraryOpen(false)} />}
  </section>;
}
