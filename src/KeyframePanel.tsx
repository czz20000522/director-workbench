import { useEffect, useId, useRef, useState } from 'react';
import TaskQueueStatus, { type QueueReceipt } from './TaskQueueStatus';
import ResolutionPicker, { type OutputResolution } from './ResolutionPicker';
import MaterialSelect, { type MaterialOption } from './MaterialSelect';
import { ApiError, request as apiRequest } from './api/client';

type Mode = 'text' | 'reference';
type CreativeProfile = 'general' | 'adult-realism' | 'adult-anime';
type Submission = { mode?: Mode; creative_profile?: CreativeProfile; width?: number; height?: number; output_resolution?: OutputResolution; prompt: string; reference_image?: string; seed: number; idempotency_key: string };
type Task = { id: string; status: string; queue?: QueueReceipt['queue']; error?: string; result?: { output?: string; native_output?: string; published_native_output?: string; width?: number; height?: number; output_size?: { resolution?: OutputResolution; width?: number; height?: number; method?: string; note?: string } }; payload?: { request?: Submission; output_size?: { resolution?: OutputResolution; width?: number; height?: number; method?: string } } };
type Saved = { mode: Mode; creative_profile: CreativeProfile; prompt: string; reference_image: string; seed: number; width?: number; height?: number; output_resolution?: OutputResolution; pending?: Submission };
class RequestError extends Error {
  constructor(message: string, readonly status: number) { super(message); }
}
const terminal = ['succeeded', 'failed', 'stopped'];
const labels: Record<string, string> = { scheduler_waiting: '已接受 · 等待资源', batch_waiting: '已接受 · 等待依赖', submitting: '正在提交', queued: '排队中', running: '生成中', succeeded: '已生成 · 待审阅', failed: '生成失败', stopped: '已停止', stop_requested: '正在停止', stopping: '正在停止', needs_reconcile: '需要核对结果' };
const resolutionLabel = (value?: OutputResolution) => value && value !== 'native' ? value.toUpperCase() : '原生';
const methodLabel = (value?: string) => value === 'lanczos_resize' ? '尺寸放大（Lanczos）' : value === 'ai_upscale' ? 'AI 高清处理' : '原生生成';

export default function KeyframePanel({ projectId, options, onBrowse, onComplete, simple = false, initialMode = 'reference', candidateTaskId, creativeProfile, defaultOpen = false }: {
  projectId: string; options: MaterialOption[]; onBrowse: () => void; onComplete: (taskId: string) => void;
  simple?: boolean; initialMode?: Mode; candidateTaskId?: string; creativeProfile?: CreativeProfile; defaultOpen?: boolean;
}) {
  const storageKey = `director-workbench:${projectId}:keyframe:v1`;
  const promptId = useId();
  const modeId = useId();
  const [saved, setSaved] = useState<Saved>(() => {
    try {
      const value = JSON.parse(localStorage.getItem(storageKey) || '{}');
      return { mode: value.pending ? (value.pending.mode === 'text' ? 'text' : 'reference') : value.mode === 'text' || value.mode === 'reference' ? value.mode : initialMode, creative_profile: value.creative_profile === 'adult-realism' || value.creative_profile === 'adult-anime' ? value.creative_profile : (creativeProfile ?? 'general'), prompt: typeof value.prompt === 'string' ? value.prompt : '', reference_image: typeof value.reference_image === 'string' ? value.reference_image : '',
        width: value.width ?? 768, height: value.height ?? 1344, output_resolution: value.output_resolution ?? 'native',
        seed: Number.isInteger(value.seed) && value.seed >= 0 && value.seed <= 4294967295 ? value.seed : 340921,
        pending: value.pending && typeof value.pending.idempotency_key === 'string' && typeof value.pending.prompt === 'string' && (value.pending.mode === 'text' || typeof value.pending.reference_image === 'string') && Number.isInteger(value.pending.seed) ? value.pending : undefined };
    } catch { return { mode: initialMode, creative_profile: creativeProfile ?? 'general', prompt: '', reference_image: '', seed: 340921 }; }
  });
  const [task, setTask] = useState<Task | null>(null);
  const pendingKey = useRef(saved.pending?.idempotency_key);
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);
  const [capability, setCapability] = useState<{ mode: Mode; available: boolean; missing_nodes?: string[]; missing_models?: string[]; reason?: string; requires_reference_image?: boolean } | null>(null);
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
        const pending: Submission = saved.pending || { mode: saved.mode, creative_profile: saved.creative_profile, prompt: saved.prompt.trim(), ...(saved.mode === 'text' ? { width: saved.width ?? 768, height: saved.height ?? 1344, output_resolution: saved.output_resolution ?? 'native' } : { reference_image: saved.reference_image }), seed: saved.seed, idempotency_key: `keyframe-${crypto.randomUUID()}` };
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
  return <details className="director-operation-panel quick-image-form" open={simple || defaultOpen || undefined} aria-label="生成关键帧">
    <summary>{simple ? '生成图片' : '生成关键帧'} {task && `· ${labels[task.status] || task.status}`}</summary>
    <div className="director-creation-grid">
      <div><label htmlFor={modeId}>生成方式</label><select id={modeId} value={saved.mode} disabled={Boolean(saved.pending)} onChange={event => persist({ ...saved, mode: event.target.value as Mode })}><option value="text">文字生图</option><option value="reference">参考图改图</option></select></div>
      {saved.mode === 'text' && <ResolutionPicker label="图片" disabled={Boolean(saved.pending)} aspect={saved.width === saved.height ? '1:1' : (saved.width ?? 768) > (saved.height ?? 1344) ? '16:9' : '9:16'} resolution={saved.output_resolution} nativeSize={`${saved.width ?? 768} × ${saved.height ?? 1344}`} onChange={(aspect, output_resolution) => { const [width, height] = aspect === '1:1' ? [1024, 1024] : aspect === '16:9' ? [1344, 768] : [768, 1344]; persist({ ...saved, width, height, output_resolution }); }} />}
      <div className="director-creation-wide"><label htmlFor={promptId}>{simple ? '图片提示词' : '画面描述'}</label><textarea id={promptId} rows={4} maxLength={6000} value={saved.prompt} disabled={Boolean(saved.pending)} placeholder="描述角色、场景、姿态和构图" onChange={event => persist({ ...saved, prompt: event.target.value })} /></div>
      {saved.mode === 'reference' && <fieldset className="director-creation-wide" disabled={Boolean(saved.pending)}><MaterialSelect label="参考图片" kind="image" value={saved.reference_image} options={options} onChange={value => persist({ ...saved, reference_image: value })} onBrowse={onBrowse} /></fieldset>}
      <details className="director-creation-wide"><summary>高级：固定种子</summary><label>种子<input type="number" min={0} max={4294967295} step={1} value={saved.seed} disabled={Boolean(saved.pending)} onChange={event => persist({ ...saved, seed: Number(event.target.value) })} /></label></details>
    </div>
    <p className="director-muted">生成后可直接下载图片，也可以选作视频的开始画面。每次生成都会保留原图。</p>
    {(!capability?.available || capability.mode !== saved.mode) && <p role="status" aria-live="polite">{!capability || capability.mode !== saved.mode ? '正在检查生成环境…' : capability.reason || '关键帧生成环境尚未就绪。'}</p>}
    {capability && !capability.available && <details><summary>查看缺少的资源</summary><p>{[...(capability.missing_nodes || []), ...(capability.missing_models || [])].join("、") || "请检查生成服务连接。"}</p></details>}
    {message && <p role="status" aria-live="polite">{message}</p>}
    {task?.error && <p role="status" aria-live="polite">{task.error}</p>}
    {task?.status === 'succeeded' && <div className="quick-candidate-meta" aria-label="图片候选信息">
      <div className="quick-candidate-meta-heading"><strong>当前图片候选</strong><span>已生成 · 待审阅</span></div>
      <dl><div><dt>输出规格</dt><dd>{resolutionLabel(task.result?.output_size?.resolution ?? task.payload?.request?.output_resolution)}{(task.result?.output_size?.width ?? task.result?.width) && (task.result?.output_size?.height ?? task.result?.height) ? ` · ${task.result?.output_size?.width ?? task.result?.width} × ${task.result?.output_size?.height ?? task.result?.height}` : ''}</dd></div><div><dt>处理方式</dt><dd>{methodLabel(task.result?.output_size?.method ?? task.payload?.output_size?.method)}</dd></div><div><dt>输入快照</dt><dd>完整提示词与参数已保留</dd></div></dl>
      {(task.result?.native_output || task.result?.published_native_output) && <p>原生文件已保留；当前显示的是处理后的图片候选。</p>}
    </div>}
    <div className="director-form-actions">
      {!saved.pending && <button className="button primary compact" disabled={busy || (!capability?.available || capability.mode !== saved.mode) || !saved.prompt.trim() || (saved.mode === 'reference' && !saved.reference_image) || !Number.isInteger(saved.seed) || saved.seed < 0 || saved.seed > 4294967295} onClick={() => void act('submit')}>{simple ? '生成图片' : '生成关键帧候选'}</button>}
      {saved.pending && !task && <><button className="button secondary compact" disabled={busy} onClick={() => void act('check')}>核对关键帧提交</button><button className="button secondary compact" disabled={busy || (!capability?.available || capability.mode !== saved.mode)} onClick={() => void act('submit')}>使用原凭据重试</button></>}
      {task && !terminal.includes(task.status) && task.status !== 'needs_reconcile' && <button className="button secondary compact" disabled={busy} onClick={() => void act('stop')}>停止关键帧任务</button>}
      {task?.status === 'needs_reconcile' && <button className="button secondary compact" disabled={busy} onClick={() => void act('reconcile')}>核对关键帧结果</button>}
      {task && terminal.includes(task.status) && <button className="button secondary compact" disabled={busy} onClick={() => { if (persist({ ...saved, pending: undefined })) setTask(null); }}>{simple ? '修改提示词 / 再生成' : '准备下一张关键帧'}</button>}
      {simple && candidateTaskId && (!saved.pending || task && terminal.includes(task.status)) && <button className="button secondary compact" disabled={busy} onClick={() => void editCandidate()}>载入当前图片的提示词</button>}
      {task && <TaskQueueStatus task={{ ...task, asset_id: '关键帧' }} />}
      {task && <span>{labels[task.status] || task.status}</span>}
    </div>
  </details>;
}
