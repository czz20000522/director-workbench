// Pure helpers extracted from the workbench shell: no React state, no JSX.
import type { CheckpointRecord, CreativeDraft, CreativeProfile, Drafts, PlanSegment, QuickAspect, SegmentDraft, ShotVersion, TaskRecord } from './types';
import { mediaUrl } from '../api/client';
import type { Asset, Kind, Variant } from '../data';
import type { OutputResolution } from '../ResolutionPicker';

export const taskStateLabels: Record<string, string> = {
  preparing: '准备中', submitting: '提交中', queued: '排队中', running: '执行中', succeeded: '已生成', failed: '生成失败',
  stopped: '已停止', stop_requested: '停止请求中', stopping: '停止中', needs_reconcile: '待核对', batch_waiting: '批次等待中', scheduler_waiting: '等待资源',
};
export const taskStateLabel = (status: string) => status ? (taskStateLabels[status] ?? `未知状态 · ${status}`) : '尚未提交';
// A successful task is represented by the generated asset in the review UI;
// keep the machine status out of the director-facing action labels.
export const normalizeLiveTaskState = (status: string) => status === 'succeeded' ? '' : status;
export const reviewLabel = (status?: string) => status === 'approved' ? '已采用' : status === 'changes_requested' ? '需调整' : status === 'stale' ? '待复核' : '待审核';
export const outputResolutionLabel = (value?: OutputResolution | 'ai') => value === 'ai' ? 'AI 高清候选' : value && value !== 'native' ? value.toUpperCase() : '原生';
export const outputMethodLabel = (value?: string) => value === 'lanczos_resize' ? '尺寸放大（Lanczos）' : value === 'ai_upscale' ? 'AI 高清处理' : '原生生成';
export function candidateOutputFacts(task: TaskRecord | null, segment?: PlanSegment | null, candidate: Variant = 'A'): { resolution?: OutputResolution | 'ai'; width?: number; height?: number; method?: string; nativeOutput?: string } {
  const outputSize = task?.result?.output_size ?? task?.payload?.output_size;
  const request = task?.payload?.request;
  const finishSize = candidate === 'B' ? segment?.video?.finish_review?.processing?.output_size : undefined;
  const finishProcessing = candidate === 'B' ? segment?.video?.finish_review?.processing : undefined;
  const resolution: OutputResolution | 'ai' = outputSize?.resolution ?? request?.output_resolution ?? segment?.generation?.output_resolution ?? (finishProcessing ? 'ai' : 'native');
  const width = finishSize?.width ?? outputSize?.width ?? task?.result?.width ?? request?.width;
  const height = finishSize?.height ?? outputSize?.height ?? task?.result?.height ?? request?.height;
  return {
    resolution,
    width,
    height,
    method: candidate === 'B' && finishProcessing?.method
      ? finishProcessing.method
      : outputSize?.method ?? (resolution === 'native' ? 'native_generation' : 'lanczos_resize'),
    nativeOutput: task?.result?.published_native_output ?? task?.result?.native_output ?? outputSize?.native_output,
  };
}
export const checkpointLabel = (checkpoint: CheckpointRecord | undefined, hasContent: boolean) => checkpoint?.status === 'approved' ? '已确认' : checkpoint?.status === 'changes_requested' ? '需修改' : checkpoint?.status === 'stale' ? '上游变化 · 待复核' : hasContent ? '有内容 · 待确认' : '待填写';

// 成人向视频配方只有一个组合（16:9 + 标准步数），其余组合必须说清实际会用什么，
// 不能把快速预览或别的画幅的原生配方说成成人向配方。
export function adultVideoProfileNote(profile: CreativeProfile, aspect: string, speed: string) {
  const used = speed === 'fast'
    ? '本次改用「快速预览」方案（8 步 + Kitchen attention，0.4MP 横屏）'
    : '本次改用该画幅的 H3 原生配方';
  if (profile === 'adult-anime') return `动漫质感只在关键帧图像加载 Qwen Anime Consistency，视频生成没有对应的成人向配方；${used}，其中不含成人向配方。`;
  if (aspect === '16:9' && speed === 'standard') return '成人向视频将使用：H3 Realism People（标准步数）。';
  return `真人质感配方只在 16:9 + 标准方案下可用；${used}，其中不含成人向配方。`;
}

// 每个快速分区对应一个系列，成人向内容必须留在「成人向创作」，普通创作也不该被写进去。
// 归属判定（openQuickTemplate）与「产物存进哪里」的提示共用这一份名称。
export const QUICK_SERIES = { general: '快速草稿', adult: '成人向创作' } as const;

// 视频画幅必须跟着开始画面走：H3 图像转视频对首帧只做缩放铺满（不裁切），
// 画幅与首帧不一致就会把画面压扁。判定规则与关键帧面板的画幅回显一致。
export function stillAspectRatio(width: number, height: number): QuickAspect {
  return width === height ? '1:1' : width > height ? '16:9' : '9:16';
}

export const ASPECT_LABEL: Record<QuickAspect, string> = { '16:9': '横屏', '9:16': '竖屏', '1:1': '方形' };

// 素材选项里只有路径、没有尺寸，所以尺寸只能由浏览器读取素材本身；
// 读不到就返回 null，保持现有默认值，不猜。
export function measureStillAspect(path?: string | null): Promise<QuickAspect | null> {
  const url = mediaUrl(path);
  if (!url) return Promise.resolve(null);
  return new Promise(resolve => {
    const image = new Image();
    image.onload = () => resolve(stillAspectRatio(image.naturalWidth, image.naturalHeight));
    image.onerror = () => resolve(null);
    image.src = url;
  });
}

export function localSystemTime(value?: string | null) {
  if (!value) return '生成时间未登记';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '生成时间未登记' : new Intl.DateTimeFormat('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).format(date);
}

export function versionMaterial(version: ShotVersion, field: 'first' | 'last' | 'guide') {
  const snapshot = version.snapshot;
  const frames = snapshot?.keyframes as Record<string, unknown> | undefined;
  const audio = snapshot?.audio as Record<string, unknown> | undefined;
  const execution = snapshot?.execution_snapshot as Record<string, unknown> | undefined;
  const materials = execution?.materials as Record<string, unknown> | undefined;
  return String(field === 'guide' ? snapshot?.audio_guide ?? audio?.guide ?? materials?.guide ?? '' : field === 'first' ? snapshot?.first_frame ?? frames?.first ?? materials?.['first-frame'] ?? '' : snapshot?.last_frame ?? frames?.last ?? materials?.['last-frame'] ?? '');
}

export function normalizeManifestAsset(raw: Record<string, unknown>): Asset {
  const sources = raw.sources && typeof raw.sources === 'object' ? Object.fromEntries(Object.entries(raw.sources as Record<string, unknown>).map(([key, value]) => [key, mediaUrl(String(value))])) as Partial<Record<Variant, string>> : undefined;
  const image = raw.image ? (String(raw.image).startsWith('ComfyUI-') ? mediaUrl(String(raw.image)) : String(raw.image)) : mediaUrl(raw.image_path ? String(raw.image_path) : undefined);
  return {
    id: String(raw.id ?? `asset-${Math.random().toString(36).slice(2, 8)}`), kind: (raw.kind === 'audio' || raw.kind === 'image' ? raw.kind : 'video') as Kind,
    title: String(raw.title ?? raw.id ?? '未命名素材'), subtitle: String(raw.subtitle ?? ''),
    start: typeof raw.start === 'number' ? raw.start : undefined, end: typeof raw.end === 'number' ? raw.end : undefined,
    ready: Boolean(raw.ready ?? image ?? (sources && Object.keys(sources).length)), image, lastImage: mediaUrl(raw.last_image ? String(raw.last_image) : undefined), sources,
    intent: String(raw.intent ?? ''), prompt: String(raw.prompt ?? ''), origin: String(raw.origin ?? '项目资源'), specs: String(raw.specs ?? ''), workflow: String(raw.workflow ?? '待登记'),
  };
}

export function loadDrafts(storageKey: string): Drafts {
  try { const data = JSON.parse(localStorage.getItem(storageKey) ?? '{}'); return data && typeof data === 'object' && !Array.isArray(data) ? data : {}; }
  catch { return {}; }
}

export function loadCreativeDraft(storageKey: string): CreativeDraft {
  const blank: CreativeDraft = { creation_mode: 'original', reference_series: '', adaptation: '', intent: '', world: '', character: '', voice: '', reference_video: '', notes: '' };
  try {
    const data = JSON.parse(localStorage.getItem(storageKey) ?? '{}');
    return data && typeof data === 'object' && !Array.isArray(data) ? { ...blank, ...(data as Partial<CreativeDraft>) } : blank;
  } catch { return blank; }
}

export const creativeFieldNames: Array<keyof CreativeDraft> = ['creation_mode', 'reference_series', 'adaptation', 'intent', 'world', 'character', 'voice', 'reference_video', 'notes'];
export const blankCreativeDraft = (): CreativeDraft => ({ creation_mode: 'original', reference_series: '', adaptation: '', intent: '', world: '', character: '', voice: '', reference_video: '', notes: '' });
export const normalizeCreativeDraft = (value?: Partial<CreativeDraft> | null): CreativeDraft => Object.fromEntries(creativeFieldNames.map(key => [key, String(value?.[key] ?? blankCreativeDraft()[key])])) as CreativeDraft;
export const hasCreativeDraftContent = (value: CreativeDraft) => creativeFieldNames.some(key => key === 'creation_mode' ? value[key] !== 'original' : value[key].trim().length > 0);
export const sameCreativeDraft = (left: CreativeDraft, right: CreativeDraft) => creativeFieldNames.every(key => left[key] === right[key]);

export const emptySegmentDraft = (): SegmentDraft => ({ script_scene_id: '', script_revision: '', audio_guide: '', delivery_master: '', segment_id: '', duration_seconds: '5', location: '', shot_size: '中景', camera: '固定机位', wardrobe: '沿用角色设定', performance: '', prompt: '', workflow: '', first_frame: '', last_frame: '' });
export function readNewSegmentDraft(projectId: string): SegmentDraft {
  const empty = emptySegmentDraft();
  if (!projectId) return empty;
  try {
    const saved = JSON.parse(localStorage.getItem(`director-workbench:${projectId}:new-segment:v1`) ?? '{}');
    return Object.fromEntries(Object.entries(empty).map(([key, fallback]) => [key, typeof saved?.[key] === 'string' ? saved[key] : fallback])) as SegmentDraft;
  } catch { return empty; }
}

