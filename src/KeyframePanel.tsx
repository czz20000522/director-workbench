import { useEffect, useId, useRef, useState } from 'react';
import TaskQueueStatus, { type QueueReceipt } from './TaskQueueStatus';
import ProgressBar, { type TaskProgress } from './TaskProgress';
import ResolutionPicker, { type AspectRatio, type OutputResolution } from './ResolutionPicker';
import { detectQualityIntent } from './qualityIntent';
import { BookOpen, Scaling, Wand2 } from 'lucide-react';
import { mediaUrl } from './api/client';
import { PromptLibraryDialog } from './ReferenceLibrary';
import type { MaterialOption } from './MaterialSelect';
import ReferenceSlotField, { type SlotValue, type ReferenceRole } from './ReferenceSlotField';
import PromptWriter from './PromptWriter';
import TranslateButton from './TranslateButton';
import StyleBlockPicker, { readStyleBlock } from './StyleBlockPicker';
import LiquifyDialog from './LiquifyDialog';
import InpaintDialog from './InpaintDialog';
import { ApiError, request as apiRequest } from './api/client';

type Mode = 'text' | 'reference';
type CreativeProfile = 'general' | 'adult-realism' | 'adult-anime';
type Submission = { mode?: Mode; creative_profile?: CreativeProfile; width?: number; height?: number; output_resolution?: OutputResolution; ai_upscale?: boolean; steps?: number; refine?: boolean; prompt: string; reference_image?: string; references?: { role: string; material_id: string }[]; seed: number; style_block?: string; idempotency_key: string };
type Task = { id: string; status: string; queue?: QueueReceipt['queue']; progress?: TaskProgress; error?: string; result?: { output?: string; native_output?: string; published_native_output?: string; width?: number; height?: number; output_size?: { resolution?: OutputResolution; width?: number; height?: number; method?: string; note?: string } }; payload?: { request?: Submission; output_size?: { resolution?: OutputResolution; width?: number; height?: number; method?: string } } };
type Saved = { mode: Mode; creative_profile: CreativeProfile; prompt: string; reference_image: string; slots?: SlotValue[]; seed: number; autoSeed?: boolean; refine?: boolean; width?: number; height?: number; output_resolution?: OutputResolution; ai_upscale?: boolean; steps?: number; pending?: Submission };
const orderedSlots = (slots?: SlotValue[]) =>
  SLOT_ROLES.flatMap(role => (slots || []).filter(slot => slot.role === role));
const DEFAULT_STEPS = 8;
const SLOT_ROLES: ReferenceRole[] = ['脸型', '造型', '服装', '姿势'];
const SLOT_HINTS: Record<ReferenceRole, string> = {
  '底图': '以这张为准做修改',
  '脸型': '只决定长相：默认只列「脸型·发型」里的正面脸图',
  '造型': '只决定发型：默认只列「造型参考·」那批；也可以直接写「丸子头」',
  '服装': '只决定穿什么：默认只列「服饰·写实」；点「不穿衣服」或自己写一句也行',
  '姿势': '只决定站/坐/动作；走 ControlNet 结构控制，深度图更准，线稿可作备选',
  '自有素材': '一整张图：任意分类都能选，也能上传（首帧台里用）',
};
// 11-07 之前存的草稿里只有一个笼统的「人像」槽，读回来一律落到「脸型」。
const LEGACY_ROLES: Record<string, ReferenceRole> = { '人像': '脸型' };
const readSlots = (value: unknown): SlotValue[] => Array.isArray(value)
  ? value.flatMap(item => {
      if (!item || typeof item !== 'object') return [];
      const record = item as Record<string, unknown>;
      const raw = String(record.role || '');
      const role = LEGACY_ROLES[raw] || raw;
      if (!SLOT_ROLES.includes(role as ReferenceRole)) return [];
      // 槽位可以是素材，也可以是一句自己写的描述（「不穿衣服」「丸子头」）。
      const custom = typeof record.custom === 'string' ? record.custom.trim() : '';
      const materialId = typeof record.material_id === 'string' ? record.material_id : '';
      if (!materialId && !custom) return [];
      return [{ role: role as ReferenceRole, material_id: materialId, ...(custom ? { custom } : {}),
                title: typeof record.title === 'string' ? record.title : '',
                thumb: typeof record.thumb === 'string' ? record.thumb : '' }];
    })
  : [];
// 步数由你显式选择；不动它就是 8——Turbo 蒸馏的标称值。
const stepOptions: { value: number; label: string; note: string }[] = [
  { value: 8, label: '8 步', note: '默认' },
  { value: 16, label: '16 步', note: '更慢 · 换构图' },
];
class RequestError extends Error {
  constructor(message: string, readonly status: number) { super(message); }
}
const terminal = ['succeeded', 'failed', 'stopped'];
const labels: Record<string, string> = { scheduler_waiting: '已接受 · 等待资源', batch_waiting: '已接受 · 等待依赖', submitting: '正在提交', queued: '排队中', running: '生成中', succeeded: '已生成 · 待审阅', failed: '生成失败', stopped: '已停止', stop_requested: '正在停止', stopping: '正在停止', needs_reconcile: '需要核对结果' };
const resolutionLabel = (value?: OutputResolution) => value && value !== 'native' ? value.toUpperCase() : '原生';
const methodLabel = (value?: string) => value === 'lanczos_resize' ? '尺寸放大（Lanczos）' : value === 'ai_upscale' ? 'AI 超分（RealESRGAN）+ 尺寸缩放' : '原生生成';
// 随机编号写死会让每次新生成都高度雷同：默认每次随机，要复现同一张图时在更多设置里钉住。
const randomSeed = () => crypto.getRandomValues(new Uint32Array(1))[0];
// 原生直出才是模型真正画的尺寸；更大的档位都是采样后的放大路线。
const clarityOptions: { value: OutputResolution; label: string; note: string }[] = [
  { value: 'native', label: '原生', note: '模型直出' },
  { value: '2k', label: '2K', note: '上采样' },
  { value: '4k', label: '4K', note: '上采样' },
  { value: '8k', label: '8K', note: '上采样' },
];

export default function KeyframePanel({ projectId, options, onBrowse, onComplete, simple = false, initialMode = 'reference', candidateTaskId, creativeProfile, defaultOpen = false, fresh = false, initialPrompt, initialSeed, initialAspect, initialResolution, initialSteps, promptTicket }: {
  projectId: string; options: MaterialOption[]; onBrowse: () => void; onComplete: (taskId: string) => void;
  simple?: boolean; initialMode?: Mode; candidateTaskId?: string; creativeProfile?: CreativeProfile; defaultOpen?: boolean; fresh?: boolean; initialPrompt?: string;
  initialSeed?: number; initialAspect?: AspectRatio; initialResolution?: OutputResolution; initialSteps?: number;
  /** 左侧提示词栏点「带入」时递进来的一段文字；按 at 去重，父组件重渲染不会反复覆盖草稿。 */
  promptTicket?: { text: string; at: number } | null;
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
    if (fresh) {
      const [width, height] = initialAspect === '1:1' ? [1152, 1152] : initialAspect === '16:9' ? [1536, 864] : [864, 1536];
      return { mode: initialMode, creative_profile: creativeProfile ?? 'general', prompt: initialPrompt ?? '', reference_image: '', slots: [], width, height,
        output_resolution: initialResolution ?? 'native', ai_upscale: false, steps: initialSteps ?? DEFAULT_STEPS,
        seed: Number.isInteger(initialSeed) && (initialSeed as number) >= 0 ? (initialSeed as number) : randomSeed(),
        // 默认每张都换种子：同一段（或只改几个字的）提示词否则会一直出同一张图。
        autoSeed: true, refine: false };
    }
    try {
      const value = JSON.parse(localStorage.getItem(storageKey) || '{}');
      return { mode: value.pending ? (value.pending.mode === 'text' ? 'text' : 'reference') : value.mode === 'text' || value.mode === 'reference' ? value.mode : initialMode, creative_profile: value.creative_profile === 'adult-realism' || value.creative_profile === 'adult-anime' ? value.creative_profile : (creativeProfile ?? 'general'), prompt: typeof value.prompt === 'string' ? value.prompt : '', reference_image: typeof value.reference_image === 'string' ? value.reference_image : '', slots: readSlots(value.slots),
        width: value.width ?? 768, height: value.height ?? 1344, output_resolution: value.output_resolution ?? 'native',
        ai_upscale: value.ai_upscale === true,
        steps: value.steps === 16 ? 16 : DEFAULT_STEPS,
        seed: Number.isInteger(value.seed) && value.seed >= 0 && value.seed <= 4294967295 ? value.seed : randomSeed(),
        // 旧草稿没有这个字段：默认 true，让「再生成一张」真的换一版。
        autoSeed: value.autoSeed !== false,
        refine: value.refine === true,
        pending: value.pending && typeof value.pending.idempotency_key === 'string' && typeof value.pending.prompt === 'string' && (value.pending.mode === 'text' || typeof value.pending.reference_image === 'string') && Number.isInteger(value.pending.seed) ? value.pending : undefined };
    } catch { return { mode: initialMode, creative_profile: creativeProfile ?? 'general', prompt: '', reference_image: '', slots: [], seed: randomSeed(), autoSeed: true, refine: false }; }
  });
  const [task, setTask] = useState<Task | null>(null);
  const pendingKey = useRef(saved.pending?.idempotency_key);
  const [message, setMessage] = useState('');
  // 风格锁：只拼进 prompt_for_model，saved.prompt 保持用户原文。
  const [styleBlock, setStyleBlock] = useState(readStyleBlock);
  // 人物三要素只是拼草稿的辅助输入，不进提交请求，也不改生成链路。
  const [cast, setCast] = useState({ portrait: '', pose: '', costume: '' });
  const [busy, setBusy] = useState(false);
  const [libraryOpen, setLibraryOpen] = useState(false);
  const [capability, setCapability] = useState<{ mode: Mode; available: boolean; engine_offline?: boolean; missing_nodes?: string[]; missing_models?: string[]; reason?: string; requires_reference_image?: boolean } | null>(null);
  const [batchOpen, setBatchOpen] = useState(false);
  const [batchText, setBatchText] = useState('');
  const [batchReport, setBatchReport] = useState('');
  // 结果图的两个后续动作：液化塑形（定形）与低强度精修（补质感）。
  const [liquifySource, setLiquifySource] = useState('');
  const [refineSource, setRefineSource] = useState('');
  // 液化/精修都会产出新文件：原图不动，这里只记最新一张用于继续加工。
  const [derived, setDerived] = useState('');
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
  // 把肖像/姿势/服装拼成一段画面描述写进提示词框：只生成草稿，落笔前你还能改，
  // 也不会自动提交。已经拼过的那段不重复追加。
  function composeCast() {
    const parts = [
      cast.portrait.trim() && `人物：${cast.portrait.trim()}`,
      cast.pose.trim() && `姿势：${cast.pose.trim()}`,
      cast.costume.trim() && `服装：${cast.costume.trim()}`,
    ].filter(Boolean) as string[];
    if (!parts.length) { setMessage('三行都还空着，先写点内容再写入。'); return; }
    const draft = `${parts.join('；')}。`;
    const current = saved.prompt.trim();
    if (current.includes(draft)) { setMessage('这段已经写进提示词了。'); return; }
    persist({ ...saved, prompt: current ? `${current}\n${draft}` : draft });
    setMessage('已按肖像 → 姿势 → 服装拼成草稿写进提示词，改完再提交。');
  }
  const appliedTicket = useRef(0);
  useEffect(() => {
    if (!promptTicket?.text || promptTicket.at === appliedTicket.current) return;
    appliedTicket.current = promptTicket.at;
    persist({ ...saved, prompt: promptTicket.text });
    setMessage('已把提示词库里那段带进来，改完再提交。');
  }, [promptTicket?.at]);

  function accept(next: Task, expectedKey: string | undefined) {    if (!mounted.current || !expectedKey || pendingKey.current !== expectedKey) return;
    setTask(next); setMessage('');
    // 换了一条任务，上一张的液化/精修结果就不再属于当前画面，收起避免误认。
    if (completed.current !== next.id) setDerived('');
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
        const ordered = orderedSlots(saved.slots);
        const used = ordered.filter(slot => slot.material_id);
        const written = ordered.filter(slot => !slot.material_id && slot.custom);
        const size = { width: saved.width ?? 768, height: saved.height ?? 1344, output_resolution: appliedResolution,
          ai_upscale: appliedResolution !== 'native' && saved.ai_upscale === true, steps: saved.steps ?? DEFAULT_STEPS };
        // 参考图模式：有槽位就按槽位提交（多参考组合），没槽位才退回旧的单图。
        // 纯文字槽（不穿衣服 / 丸子头）没有图，只能拼进提示词。
        const reference = used.length
          ? { references: used.map(slot => ({ role: slot.role, material_id: slot.material_id })) }
          : { reference_image: saved.reference_image };
        const promptText = [written.map(slot => `${slot.role}：${slot.custom}`).join('；'), saved.prompt.trim()]
          .filter(Boolean).join('。');
        // 只选了「不穿衣服」这类文字槽时没有可提交的参考图，退回纯文字生图。
        const submitMode = saved.mode === 'reference' && !used.length && !saved.reference_image ? 'text' : saved.mode;
        // 自动换种子：相似描述再生成时，至少换一版构图。retry 是「上次提交还没落到
        // 终态」的重放，必须沿用原种子，否则同一凭据换了参数会被服务端拒。
        const submitSeed = saved.autoSeed === false ? saved.seed : randomSeed();
        const pending: Submission = retry || { mode: submitMode, creative_profile: saved.creative_profile, prompt: promptText, refine: saved.refine === true, ...(submitMode === 'text' ? size : { ...size, ...reference }), seed: submitSeed, ...(styleBlock ? { style_block: styleBlock } : {}), idempotency_key: `keyframe-${crypto.randomUUID()}` };
        if (!retry && task) setTask(null);
        if (!persist({ ...saved, seed: retry ? saved.seed : submitSeed, pending })) return;
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
              width: saved.width ?? 768, height: saved.height ?? 1344, steps: saved.steps ?? DEFAULT_STEPS,
              output_resolution: saved.output_resolution ?? 'native',
              ai_upscale: (saved.output_resolution ?? 'native') !== 'native' && saved.ai_upscale === true,
              seed: saved.seed,
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
  // 结果既能下载处理后的版本，也能下载未经放大的原生文件。
  const outputPath = task?.result?.output || '';
  const nativePath = task?.result?.native_output || task?.result?.published_native_output || '';
  const fileSuffix = (value: string) => (value.split('.').pop() || 'png').toLowerCase();
  return <section className={simple ? 'sf-post-tools quick-image-form' : 'sf-post-tools'} aria-label="生成关键帧">
    <section className="sf-post-section" aria-label={simple ? '生成图片设置' : '生成关键帧设置'}>
      <div className="sf-post-section-head"><h2 className="sf-post-title">{simple ? '生成图片' : '生成关键帧'}</h2><p className="sf-post-status">{task ? (labels[task.status] || task.status) : '显式提交'}</p></div>
      <div className="sf-post-fields">
        <div className="sf-post-field sf-post-field--wide">
          <div className="sf-post-field-head">
            <label htmlFor={promptId}>{simple ? '图片提示词' : '画面描述'}</label>
            <span className="tb-row">
              <TranslateButton text={saved.prompt} disabled={locked}
                onTranslated={next => persist({ ...saved, prompt: next })}
                onError={setMessage} />
              <StyleBlockPicker value={styleBlock} onChange={setStyleBlock} disabled={locked} hint={false} />
              <button type="button" className="sf-inline-link" disabled={locked} onClick={() => setLibraryOpen(true)}><BookOpen size={12} /> 从提示词库带入</button>
            </span>
          </div>
          <textarea id={promptId} rows={4} maxLength={6000} value={saved.prompt} disabled={locked} placeholder="描述角色、场景、姿态和构图" onChange={event => persist({ ...saved, prompt: event.target.value })} />
          <PromptWriter disabled={locked} prompt={saved.prompt} label="AI 润色"
            context={{
              project_id: projectId,
              profile: saved.creative_profile,
              aspect: saved.width && saved.height
                ? (saved.width === saved.height ? 'square' : saved.width > saved.height ? 'landscape' : 'portrait')
                : undefined,
              seed: saved.seed,
            }}
            onUse={text => persist({ ...saved, prompt: text })} />
        </div>
        <details className="qt-advanced sf-post-field--wide">
          <summary>人物三要素 · 拼成提示词草稿</summary>
          <div className="sf-post-fields">
            <div className="sf-post-field"><label>肖像<input value={cast.portrait} maxLength={200} disabled={locked} placeholder="二十七八岁，短发，鹅蛋脸" onChange={event => setCast({ ...cast, portrait: event.target.value })} /></label></div>
            <div className="sf-post-field"><label>姿势<input value={cast.pose} maxLength={200} disabled={locked} placeholder="侧身站在窗前，一只手扶着窗框" onChange={event => setCast({ ...cast, pose: event.target.value })} /></label></div>
            <div className="sf-post-field"><label>服装<input value={cast.costume} maxLength={200} disabled={locked} placeholder="米白色棉质衬衫，袖口挽起" onChange={event => setCast({ ...cast, costume: event.target.value })} /></label></div>
          </div>
          <div className="sf-post-actions">
            <button type="button" className="button secondary compact" disabled={locked} onClick={composeCast}>写入提示词</button>
            <button type="button" className="button secondary compact" disabled={locked} onClick={() => setCast({ portrait: '', pose: '', costume: '' })}>清空三行</button>
          </div>
          <p className="sf-post-hint">只把这三行拼成一段草稿追加到上面的提示词框，写完还能改；不改生成参数，也不会自动提交。参考图在 H3 里是整体语义参考，没法按这三项分别硬控。</p>
        </details>
        {saved.mode === 'reference' && <fieldset className="sf-post-field sf-post-field--wide sf-slots" disabled={locked}>
          <legend>参考图组合</legend>
          <p className="sf-post-hint">勾选脸型 / 造型 / 服装 / 姿势各一张，工作台会按槽位自动写角色说明。服装的结构细节（蝴蝶结、泡泡袖等）建议在画面描述里补一句，索引标签里通常没有这些词。</p>
          {SLOT_ROLES.map(role => <ReferenceSlotField
            key={role}
            role={role}
            hint={SLOT_HINTS[role]}
            disabled={locked}
            value={(saved.slots || []).find(slot => slot.role === role)}
            onChange={next => persist({ ...saved, slots: [...(saved.slots || []).filter(slot => slot.role !== role), ...(next ? [next] : [])] })}
          />)}
        </fieldset>}
        {simple && saved.mode === 'text' && <div className="sf-post-field sf-post-field--wide">
          <label>清晰度</label>
          <div className="clarity-levels is-compact" role="group" aria-label="图片清晰度">
            {clarityOptions.map(option => <button key={option.value} type="button" aria-pressed={(saved.output_resolution ?? 'native') === option.value} disabled={locked} onClick={() => persist({ ...saved, output_resolution: option.value })}><strong>{option.label}</strong><small>{option.note}</small></button>)}
          </div>
          <p className="sf-post-hint">原生 {saved.width ?? 768} × {saved.height ?? 1344} = {Math.round((saved.width ?? 768) * (saved.height ?? 1344) / 10000)} 万像素，这才是模型真正画出来的尺寸。2K、4K、8K 属于上采样路线：只把这张图放大到目标尺寸，本身不会变清晰；想加细节要勾下面的 AI 超分。</p>
          {(saved.output_resolution ?? 'native') !== 'native' && <>
            <label className="sf-post-check"><input type="checkbox" checked={saved.ai_upscale === true} disabled={locked} onChange={event => persist({ ...saved, ai_upscale: event.target.checked })} />AI 超分后再放大（RealESRGAN 2×）</label>
            <p className="sf-post-hint">11-08 本机对照：对 AI 生成的人像，超分会磨平皮肤颗粒、眉眼略糊，多数情况不如直接放大；只建议用在原生画面本身偏软时。2K 约多 3 秒，4K 约多 10 秒。</p>
          </>}
        </div>}
        {saved.mode === 'text' && <div className="sf-post-field sf-post-field--wide">
          <label>采样步数</label>
          <div className="clarity-levels" role="group" aria-label="采样步数">
            {stepOptions.map(option => <button key={option.value} type="button" aria-pressed={(saved.steps ?? DEFAULT_STEPS) === option.value} disabled={locked} onClick={() => persist({ ...saved, steps: option.value })}><strong>{option.label}</strong><small>{option.note}</small></button>)}
          </div>
          <p className="sf-post-hint">8 步是 Qwen Image Turbo 蒸馏的标称值，也是默认值。11-08 同种子对照：16 步没有更清晰，皮肤与布料差别很小，构图却换了一张，耗时 33 秒 → 56 秒。想换构图改种子更直接。</p>
        </div>}
        {saved.mode === 'text' && <div className="sf-post-field sf-post-field--wide">
          <label>降噪（真人皮肤 / 暗部）</label>
          <div className="clarity-levels" role="group" aria-label="降噪">
            <button type="button" aria-pressed={saved.refine !== true} disabled={locked} onClick={() => persist({ ...saved, refine: false })}>
              <strong>关</strong><small>{saved.steps ?? DEFAULT_STEPS} 步 · 最快</small>
            </button>
            <button type="button" aria-pressed={saved.refine === true} disabled={locked} onClick={() => persist({ ...saved, refine: true })}>
              <strong>开 · 降噪</strong><small>轻度平滑 · 几乎不增耗时</small>
            </button>
          </div>
          <p className="sf-post-hint">真人、成人向的皮肤和暗部容易起颗粒。开这一档会在出图后做一次轻度平滑、再按比例混回原图，只压高频噪点、不动构图，跟着这次生成一起完成，不额外加采样步数。</p>
        </div>}
        {(() => {
          // 单步入口只强制提示词一项；其余都有默认值，收进折叠区，幅面与输出规格各自单选。
          const advanced = <>
            {!simple && <div className="sf-post-field"><label htmlFor={modeId}>生成方式</label><select id={modeId} value={saved.mode} disabled={locked} onChange={event => persist({ ...saved, mode: event.target.value as Mode })}><option value="text">文字生图</option><option value="reference">参考图组合</option></select></div>}
            {saved.mode === 'text' && <div className="sf-post-field sf-post-field--wide"><ResolutionPicker label="图片" disabled={locked} aspect={saved.width === saved.height ? '1:1' : (saved.width ?? 768) > (saved.height ?? 1344) ? '16:9' : '9:16'} resolution={saved.output_resolution} nativeSize={`${saved.width ?? 768} × ${saved.height ?? 1344}`} onChange={(aspect, output_resolution) => { const [width, height] = aspect === '1:1' ? [1152, 1152] : aspect === '16:9' ? [1536, 864] : [864, 1536]; persist({ ...saved, width, height, output_resolution }); }} /></div>}
            <div className="sf-post-subblock sf-post-field--wide"><h3 className="sf-post-subtitle">随机编号</h3><div className="sf-post-field"><label>随机编号<input type="number" min={0} max={4294967295} step={1} value={saved.seed} disabled={locked || saved.autoSeed !== false} onChange={event => persist({ ...saved, seed: Number(event.target.value) })} /></label><button type="button" className="button secondary compact" disabled={locked || saved.autoSeed !== false} onClick={() => persist({ ...saved, seed: randomSeed() })}>换一个</button><label className="sf-post-auto"><input type="checkbox" checked={saved.autoSeed !== false} disabled={locked} onChange={event => persist({ ...saved, autoSeed: event.target.checked })} />每次生成自动换</label></div><p className="sf-post-hint">同一段提示词 + 同一个编号 = 同一张图。默认每张都换编号，所以相似描述也能出不同构图；想复现某张图时，取消勾选、把那张图用过的编号填回来。</p></div>
          </>;
          return simple ? <details className="qt-advanced"><summary>更多设置 · 幅面、输出规格、随机编号</summary>{advanced}</details> : advanced;
        })()}
      </div>
      <p className="sf-post-hint">画质规则：提示词写明 1K / 2K / 4K / 8K（或高清 / 超高清）时，以提示词为准，优先于上面的选择。</p>
      {promptIntent && saved.mode === 'text' && <p className="sf-post-hint">提示词里写了「{promptIntent.phrase}」：本次按 {promptIntent.label} 输出。</p>}
      {promptIntent && saved.mode === 'reference' && <p className="sf-post-hint">提示词里写了「{promptIntent.phrase}」，参考图模式会按所选清晰度输出。</p>}
      <p className="sf-post-hint">生成后可直接下载图片，也可以选作视频的开始画面。每次生成都会保留原图。</p>
      {(!engineReady || capability?.mode !== saved.mode) && <p className="sf-post-message" role="status" aria-live="polite">{!capability || capability.mode !== saved.mode ? '正在检查生成环境…' : capability.reason || '关键帧生成环境尚未就绪。'}</p>}
      {capability && !capability.available && !capability.engine_offline && <p className="sf-post-alert">缺少的资源：{[...(capability.missing_nodes || []), ...(capability.missing_models || [])].join("、") || "请检查生成服务连接。"}</p>}
      {capability?.engine_offline && <p className="sf-post-hint">生成引擎当前未运行；点生成会自动拉起，首次约 10–30 秒。</p>}
      {message && <p className="sf-post-message" role="status" aria-live="polite">{message}</p>}
      {task?.error && <p className="sf-post-message" role="status" aria-live="polite">{task.error}</p>}
      {task?.status === 'succeeded' && outputPath && <div className="sf-post-subblock" aria-label="生成结果">
        <figure className="sf-post-figure"><img src={mediaUrl(outputPath)} alt="生成结果预览" /></figure>
        <div className="sf-post-actions">
          <a className="button primary compact" href={mediaUrl(outputPath)} download={`关键帧-${task.id.slice(0, 8)}.${fileSuffix(outputPath)}`}>下载图片</a>
          {nativePath && nativePath !== outputPath && <a className="button secondary compact" href={mediaUrl(nativePath)} download={`关键帧原生-${task.id.slice(0, 8)}.${fileSuffix(nativePath)}`}>下载原生图</a>}
          <button type="button" className="button secondary compact" onClick={() => setLiquifySource(outputPath)}><Scaling size={13} /> 液化塑形</button>
          <button type="button" className="button secondary compact" onClick={() => setRefineSource(outputPath)}><Wand2 size={13} /> 精修</button>
        </div>
        <p className="sf-post-hint">下载的是本机文件；处理后的候选和未放大的原生文件都会保留。</p>
      </div>}
      {derived && <div className="sf-post-subblock" aria-label="液化/精修结果">
        <figure className="sf-post-figure"><img src={mediaUrl(derived)} alt="液化或精修结果" /></figure>
        <div className="sf-post-actions">
          <a className="button secondary compact" href={mediaUrl(derived)} download={`关键帧-后续-${fileSuffix(derived)}`}>下载这张</a>
          <button type="button" className="button secondary compact" onClick={() => setLiquifySource(derived)}><Scaling size={13} /> 继续液化</button>
          <button type="button" className="button secondary compact" onClick={() => setRefineSource(derived)}><Wand2 size={13} /> 继续精修</button>
          <button type="button" className="button secondary compact" onClick={() => setDerived('')}>收起</button>
        </div>
        <p className="sf-post-hint">这是新文件：原图没有改动，两个版本都留着。</p>
      </div>}
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
        {!locked && <button className="button primary compact" disabled={busy || (!engineReady || capability?.mode !== saved.mode) || (!saved.prompt.trim() && !orderedSlots(saved.slots).some(slot => slot.custom)) || (saved.mode === 'reference' && !orderedSlots(saved.slots).some(slot => slot.material_id) && !saved.reference_image && !orderedSlots(saved.slots).some(slot => slot.custom)) || !Number.isInteger(saved.seed) || saved.seed < 0 || saved.seed > 4294967295} onClick={() => void act('submit')}>{simple ? (task ? '再生成一张' : '生成图片') : '生成关键帧候选'}</button>}
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
    {liquifySource && <LiquifyDialog projectId={projectId} imagePath={liquifySource}
      onClose={() => setLiquifySource('')}
      onDone={(path, count, refine) => {
        setDerived(path);
        setLiquifySource('');
        setMessage(`液化完成（${count} 个调整中心），正在显示新图；原图没有改动。`);
        if (refine) setRefineSource(path);
      }} />}
    {refineSource && <InpaintDialog projectId={projectId} imagePath={refineSource}
      profile={creativeProfile ?? 'general'} initialStrength={0.35}
      onClose={() => setRefineSource('')}
      onDone={(path) => {
        setDerived(path);
        setRefineSource('');
        setMessage('精修完成：只重绘了涂过的区域，其余部分保持原样。');
      }} />}
  </section>;
}
