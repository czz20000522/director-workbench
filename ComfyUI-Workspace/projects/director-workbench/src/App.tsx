import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import type { CSSProperties, InputHTMLAttributes, PointerEvent as ReactPointerEvent } from 'react';
import { ArrowDownToLine, ArrowUpRight, BookOpen, Check, ChevronDown, ChevronLeft, ChevronRight, Clapperboard, ClipboardCheck, Film, FolderOpen, Image as ImageIcon, Layers3, LayoutDashboard, LogOut, Maximize2, Music2, Pause, Play, Plus, Radio, Search, Settings2, Square, Workflow, X } from 'lucide-react';
import { assets as fallbackAssets, clock, labels, pipelineStages as defaultPipelineStages, range, type Asset, type Kind, type PipelineInput, type PipelineStage, type Variant } from './data';
import MaterialBridge from './MaterialBridge';
import MaterialSelect from './MaterialSelect';
import SpeechPanel from './SpeechPanel';
import AudioEditPanel from './AudioEditPanel';
import MediaToolsPanel from './MediaToolsPanel';
import AssemblyEditPanel from './AssemblyEditPanel';
import KeyframePanel from './KeyframePanel';
import ReferenceOrigin, { type ReferenceOriginData } from './ReferenceOrigin';
import CreativeInspectionPanel, { ReferenceDifferenceSummary, type Inspection } from './CreativeInspectionPanel';
import { diagnosticMaterialPaths, isCreativeMaterial, materialPathKey, projectMaterialOptions } from './materialOptions';
import ProductionPresetPicker from './ProductionPresetPicker';
import { selectVideoGenerationStage } from './generationStage';
import ReverseWorkbench from './ReverseWorkbench';
import CreationRoutes, { creationRoutes } from './CreationRoutes';
import ScriptWorkbench from './ScriptWorkbench';
import WorkspaceExplorer from './WorkspaceExplorer';
import ArchiveBrowser from './ArchiveBrowser';
import DeleteDirectoryDialog from './DeleteDirectoryDialog';
import ShotPromptEditor from './ShotPromptEditor';
import FirstUseGuide from './FirstUseGuide';
import TaskQueueStatus from './TaskQueueStatus';
import ConnectAgent from './components/ConnectAgent';
import AgentPageBridge, { type AgentPageAction, type AgentPageActionReceipt } from './components/AgentPageBridge';
import CreationAssistant, { type AssistantPresentationEvent } from './components/CreationAssistant';
import { blockerMessage, readinessStateLabel, type ProjectReadiness, type ReadinessItem } from './readiness';
import { assetLabel, assetTitle, type TreeKind } from './shotLabels';

type Draft = { prompt?: string; note?: string };
type Drafts = Record<string, Draft>;
type ShotGenerationSettings = { mode: 'auto' | 'advanced'; aspect_ratio: '16:9' | '1:1' | '9:16'; sound_mode: 'performance_reference' | 'locked_dialogue'; seed: number };
type PlanSegment = ReferenceOriginData & { generation?: ShotGenerationSettings; id: string; script_scene_id?: string; script_revision?: number; start_seconds: number; end_seconds: number; duration_seconds: number; status: string; location: string; shot_size: string; camera: string; wardrobe: string; performance: string; dependencies?: string[]; audio?: { delivery_master?: string; guide?: string; range_seconds?: [number, number] | string }; keyframes?: { first?: string | null; last?: string | null }; workflow?: string | null; prompt?: string | null; comfyui_task_id?: string | null; current_version_task_id?: string | null; visual_review?: { status?: string; label?: string; reason?: string; reviewed_at?: string; reviewer?: string }; video?: { path?: string; variant?: string; rework_candidate?: { path?: string; variant?: string; workflow?: string; ui_workflow?: string; prompt_id?: string; status?: string; specs?: string }; finish_review?: { path?: string; variant?: string; workflow?: string; prompt_id?: string; status?: string; specs?: string } } };
type PlanAssembly = { status?: string; output?: string; workflow?: string; prompt_id?: string; duration_seconds?: number; target_duration_seconds?: number; notes?: string };
type WorkflowCatalogEntry = { id: string; role?: string; asset_id?: string; path: string; comfyui_import?: 'api' | 'ui'; equivalence?: string };
type PipelineValidation = { revision?: number; stage_id: string; valid: boolean; values: Record<string, string | number | boolean | string[]>; args: string[]; command_preview: string; binding_count?: number; parameters?: Record<string, unknown>; materials?: Record<string, unknown>; creative_inspection?: Inspection; h3_duration?: { requested_seconds: number; frame_count: number; playback_seconds: number; fps: number; width: number; height: number; aspect_ratio: string; generation_mode?: 'text_to_video' | 'image_to_video'; first_frame: string | null; prompt: string; first_frame_node_id: string | null; prompt_node_id: string } };
type TaskRecord = { id: string; project_id: string; asset_id: string; status: string; queue?: { state: string; reason?: { code: string; message?: string } | null; position?: number | null }; sequence?: number; batch_id?: string; payload?: { kind?: string; execution_snapshot?: { api_graph?: string; source_workflow?: string; prompt?: string | null; prompt_node_ids?: string[]; parameters?: Record<string, unknown>; materials?: Record<string, unknown>; pipeline_stage_id?: string; applied_bindings?: Array<Record<string, unknown>>; frozen_at?: number } }; error?: string | null; created_at?: number };
type BatchTask = TaskRecord;
type ReviewRecord = { stage: 'sample' | 'finish' | 'final'; status: 'pending_review' | 'approved' | 'changes_requested' | 'stale'; note?: string; adopted_variant?: Variant; candidate_ref?: string; revision: number; source: string; created_at: string };
type CheckpointRecord = { stage_id: string; status: 'draft' | 'pending_review' | 'approved' | 'changes_requested' | 'stale'; note?: string; revision: number; source: string; created_at: string };
type ProjectState = { readiness?: ProjectReadiness; project_id: string; creative: { values?: Partial<CreativeDraft>; status?: string; revision: number; source: string; created_at: string } | null; reviews: Record<string, Partial<Record<'sample' | 'finish' | 'final', ReviewRecord>>>; checkpoints: Record<string, CheckpointRecord>; artifacts: Array<{ kind: string; asset_id: string; title?: string; status?: string; revision: number; created_at: string }>; history: Array<{ id: number; asset_id: string; record_type: string; revision: number; source: string; created_at_iso: string; data: Record<string, unknown> }> };
type ProductionStepState = 'done' | 'active' | 'waiting' | 'blocked';
type ProductionStep = { id: string; label: string; detail: string; state: ProductionStepState; mode: 'manual' | 'auto' | 'hybrid' };
type CreativeDraft = { creation_mode: string; reference_series: string; adaptation: string; intent: string; world: string; character: string; voice: string; reference_video: string; notes: string };
type CreativeConflict = { server: CreativeDraft; revision: number; status?: string; created_at: string };
type PageKey = 'overview' | 'pipeline' | 'review' | 'segment';
type ProjectManifest = { id: string; default_creation_mode?: 'auto' | 'advanced'; production_preset?: { id: string; title: string; workflow: string }; series?: string; title?: string; tag?: string; eyebrow?: string; subtitle?: string; character_label?: string; workspace_root?: string; assets?: Array<Record<string, unknown>>; media?: Record<string, string>; pipeline?: PipelineStage[]; workflow_catalog?: WorkflowCatalogEntry[]; assembly_asset_id?: string; default_task_asset_id?: string; task_templates?: Record<string, { workflow?: string; prompt?: string; first_frame?: string; last_frame?: string; audio_guide?: string; seed?: number }> };
type SegmentDraft = { script_scene_id: string; script_revision: string; audio_guide: string; delivery_master: string; segment_id: string; duration_seconds: string; location: string; shot_size: string; camera: string; wardrobe: string; performance: string; prompt: string; workflow: string; first_frame: string; last_frame: string };
type SegmentEdit = { duration_seconds: string; location: string; shot_size: string; camera: string; wardrobe: string; performance: string; prompt: string; workflow: string; first_frame: string; last_frame: string; audio_guide: string; delivery_master: string; dependencies: string };
type ShotVersion = { task_id: string; generated_at: string; video_path: string; snapshot?: Record<string, unknown>; snapshot_complete?: boolean; is_current: boolean; restored_at?: string | null };
const kindIcons = { video: Film, audio: Music2, image: ImageIcon };
const taskStateLabels: Record<string, string> = {
  preparing: '准备中', submitting: '提交中', queued: '排队中', running: '执行中', succeeded: '已生成', failed: '生成失败',
  stopped: '已停止', stop_requested: '停止请求中', stopping: '停止中', needs_reconcile: '待核对', batch_waiting: '批次等待中', scheduler_waiting: '等待资源',
};
const taskStateLabel = (status: string) => status ? (taskStateLabels[status] ?? `未知状态 · ${status}`) : '尚未提交';
// A successful task is represented by the generated asset in the review UI;
// keep the machine status out of the director-facing action labels.
const normalizeLiveTaskState = (status: string) => status === 'succeeded' ? '' : status;
const reviewLabel = (status?: string) => status === 'approved' ? '已采用' : status === 'changes_requested' ? '需调整' : status === 'stale' ? '待复核' : '待审核';
const checkpointLabel = (checkpoint: CheckpointRecord | undefined, hasContent: boolean) => checkpoint?.status === 'approved' ? '已确认' : checkpoint?.status === 'changes_requested' ? '需修改' : checkpoint?.status === 'stale' ? '上游变化 · 待复核' : hasContent ? '有内容 · 待确认' : '待填写';

function sharedMediaUrl(path?: string | null) {
  return path ? `/media-file?path=${encodeURIComponent(path)}` : undefined;
}

function workflowDownloadUrl(path: string) {
  return `/workflow-file?path=${encodeURIComponent(path)}`;
}

function localSystemTime(value?: string | null) {
  if (!value) return '生成时间未登记';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '生成时间未登记' : new Intl.DateTimeFormat('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).format(date);
}

function versionMaterial(version: ShotVersion, field: 'first' | 'last' | 'guide') {
  const snapshot = version.snapshot;
  const frames = snapshot?.keyframes as Record<string, unknown> | undefined;
  const audio = snapshot?.audio as Record<string, unknown> | undefined;
  const execution = snapshot?.execution_snapshot as Record<string, unknown> | undefined;
  const materials = execution?.materials as Record<string, unknown> | undefined;
  return String(field === 'guide' ? snapshot?.audio_guide ?? audio?.guide ?? materials?.guide ?? '' : field === 'first' ? snapshot?.first_frame ?? frames?.first ?? materials?.['first-frame'] ?? '' : snapshot?.last_frame ?? frames?.last ?? materials?.['last-frame'] ?? '');
}

function normalizeManifestAsset(raw: Record<string, unknown>): Asset {
  const sources = raw.sources && typeof raw.sources === 'object' ? Object.fromEntries(Object.entries(raw.sources as Record<string, unknown>).map(([key, value]) => [key, sharedMediaUrl(String(value))])) as Partial<Record<Variant, string>> : undefined;
  const image = raw.image ? (String(raw.image).startsWith('ComfyUI-') ? sharedMediaUrl(String(raw.image)) : String(raw.image)) : sharedMediaUrl(raw.image_path ? String(raw.image_path) : undefined);
  return {
    id: String(raw.id ?? `asset-${Math.random().toString(36).slice(2, 8)}`), kind: (raw.kind === 'audio' || raw.kind === 'image' ? raw.kind : 'video') as Kind,
    title: String(raw.title ?? raw.id ?? '未命名素材'), subtitle: String(raw.subtitle ?? ''),
    start: typeof raw.start === 'number' ? raw.start : undefined, end: typeof raw.end === 'number' ? raw.end : undefined,
    ready: Boolean(raw.ready ?? image ?? (sources && Object.keys(sources).length)), image, lastImage: sharedMediaUrl(raw.last_image ? String(raw.last_image) : undefined), sources,
    intent: String(raw.intent ?? ''), prompt: String(raw.prompt ?? ''), origin: String(raw.origin ?? '项目资源'), specs: String(raw.specs ?? ''), workflow: String(raw.workflow ?? '待登记'),
  };
}

function loadDrafts(storageKey: string): Drafts {
  try { const data = JSON.parse(localStorage.getItem(storageKey) ?? '{}'); return data && typeof data === 'object' && !Array.isArray(data) ? data : {}; }
  catch { return {}; }
}

function loadCreativeDraft(storageKey: string): CreativeDraft {
  const blank: CreativeDraft = { creation_mode: 'original', reference_series: '', adaptation: '', intent: '', world: '', character: '', voice: '', reference_video: '', notes: '' };
  try {
    const data = JSON.parse(localStorage.getItem(storageKey) ?? '{}');
    return data && typeof data === 'object' && !Array.isArray(data) ? { ...blank, ...(data as Partial<CreativeDraft>) } : blank;
  } catch { return blank; }
}

const creativeFieldNames: Array<keyof CreativeDraft> = ['creation_mode', 'reference_series', 'adaptation', 'intent', 'world', 'character', 'voice', 'reference_video', 'notes'];
const blankCreativeDraft = (): CreativeDraft => ({ creation_mode: 'original', reference_series: '', adaptation: '', intent: '', world: '', character: '', voice: '', reference_video: '', notes: '' });
const normalizeCreativeDraft = (value?: Partial<CreativeDraft> | null): CreativeDraft => Object.fromEntries(creativeFieldNames.map(key => [key, String(value?.[key] ?? blankCreativeDraft()[key])])) as CreativeDraft;
const hasCreativeDraftContent = (value: CreativeDraft) => creativeFieldNames.some(key => key === 'creation_mode' ? value[key] !== 'original' : value[key].trim().length > 0);
const sameCreativeDraft = (left: CreativeDraft, right: CreativeDraft) => creativeFieldNames.every(key => left[key] === right[key]);

const emptySegmentDraft = (): SegmentDraft => ({ script_scene_id: '', script_revision: '', audio_guide: '', delivery_master: '', segment_id: '', duration_seconds: '5', location: '', shot_size: '中景', camera: '固定机位', wardrobe: '沿用角色设定', performance: '', prompt: '', workflow: '', first_frame: '', last_frame: '' });
function readNewSegmentDraft(projectId: string): SegmentDraft {
  const empty = emptySegmentDraft();
  if (!projectId) return empty;
  try {
    const saved = JSON.parse(localStorage.getItem(`director-workbench:${projectId}:new-segment:v1`) ?? '{}');
    return Object.fromEntries(Object.entries(empty).map(([key, fallback]) => [key, typeof saved?.[key] === 'string' ? saved[key] : fallback])) as SegmentDraft;
  } catch { return empty; }
}

export default function App({ privateMode = false, onLogout, logoutBusy = false, sessionError = '' }: { privateMode?: boolean; onLogout?: () => void; logoutBusy?: boolean; sessionError?: string } = {}) {
  const [project, setProject] = useState<ProjectManifest | null>(null);
  const activeProjectId = useRef<string | undefined>(undefined);
  activeProjectId.current = project?.id;
  const [projectList, setProjectList] = useState<Array<{ id: string; title: string; series?: string; selected?: boolean }>>([]);
  const [archive, setArchive] = useState<{ series: string; work: string } | null>(null);
  const [seriesRefreshKey, setSeriesRefreshKey] = useState(0);
  const [deleteDirectory, setDeleteDirectory] = useState<{ series: string; work?: string; projectId?: string; title?: string; resource?: boolean } | null>(null);
  const [deleteShotOpen, setDeleteShotOpen] = useState(false);
  const [deletingShot, setDeletingShot] = useState(false);
  const [projectMenuOpen, setProjectMenuOpen] = useState(false);
  const [sidebarWidth, setSidebarWidth] = useState(() => {
    const saved = Number(localStorage.getItem('director-workbench:sidebar-width'));
    return Number.isFinite(saved) && saved >= 240 ? saved : 286;
  });
  const [pendingTreeItem, setPendingTreeItem] = useState<{ projectId: string; kind: TreeKind; id: string } | null>(null);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [page, setPage] = useState<PageKey>('overview');
  const [referenceTarget, setReferenceTarget] = useState<{ projectId: string; analysisId: string } | null>(null);
  const [workspaceMode, setWorkspaceMode] = useState<'production' | 'reverse'>('production');
  const [workspacePage, setWorkspacePage] = useState<'creative' | 'script' | 'shots' | 'delivery'>('shots');
  const [importPath, setImportPath] = useState('');
  const [importReport, setImportReport] = useState<Record<string, unknown> | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [newSeries, setNewSeries] = useState('');
  const [newTitle, setNewTitle] = useState('');
  const [newProjectId, setNewProjectId] = useState('');
  const [newCreationMode, setNewCreationMode] = useState('original');
  const [newReferenceProject, setNewReferenceProject] = useState('');
  const [creatingProject, setCreatingProject] = useState(false);
  const [importFiles, setImportFiles] = useState<File[]>([]);
  const folderInputRef = useRef<HTMLInputElement>(null);
  const [kind, setKind] = useState<Kind>('video');
  const [selected, setSelected] = useState('');
  const [materialTitle, setMaterialTitle] = useState('');
  const [savingMaterialTitle, setSavingMaterialTitle] = useState(false);
  const [variant, setVariant] = useState<Variant>('A');
  const [expanded, setExpanded] = useState<string[]>([]);
  const [drafts, setDrafts] = useState<Drafts>({});
  const [continuous, setContinuous] = useState(false);
  const [playing, setPlaying] = useState(false);
  const [mediaLoad, setMediaLoad] = useState<{ key: string; status: 'loading' | 'ready' | 'error' }>({ key: '', status: 'loading' });
  const [position, setPosition] = useState(0);
  const [notice, setNotice] = useState('');
  const pageInteraction = useRef(0);
  const agentDraft = useRef<{ projectId: string; draft: SegmentDraft; generation: ShotGenerationSettings; interaction: number } | null>(null);
  const [agentStep, setAgentStep] = useState('');
  useEffect(() => {
    const interacted = (event: Event) => {
      const target = event.target as HTMLElement | null;
      if (!event.isTrusted || target?.closest('[aria-label="创作助手"], [aria-label="Agent 标签页引导"], [aria-label="连接你的Agent"], [data-agent-connect]')) return;
      pageInteraction.current += 1;
    };
    document.addEventListener('input', interacted);
    document.addEventListener('pointerdown', interacted);
    document.addEventListener('keydown', interacted);
    return () => { document.removeEventListener('input', interacted); document.removeEventListener('pointerdown', interacted); document.removeEventListener('keydown', interacted); };
  }, []);
  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(''), 5000);
    return () => window.clearTimeout(timer);
  }, [notice]);
  const [storageError, setStorageError] = useState(false);
  const [lightbox, setLightbox] = useState(false);
  const [resourceOpen, setResourceOpen] = useState(false);
  const [materialBridgeOpen, setMaterialBridgeOpen] = useState(false);
  const [materialBridgeDevice, setMaterialBridgeDevice] = useState(false);
  const [comfyOnline, setComfyOnline] = useState(false);
  const [comfyInfo, setComfyInfo] = useState<{ version?: string; gpu_free_mib?: number; queue_running?: number; queue_pending?: number }>({});
  const [taskState, setTaskState] = useState('');
  const submittingShot = useRef(false);
  const [taskId, setTaskId] = useState('');
  const [currentTask, setCurrentTask] = useState<TaskRecord | null>(null);
  const [projectTasks, setProjectTasks] = useState<TaskRecord[]>([]);
  const [assemblyTaskState, setAssemblyTaskState] = useState('');
  const [assemblyTaskId, setAssemblyTaskId] = useState('');
  const [pendingAssemblyProject, setPendingAssemblyProject] = useState('');
  const [assemblySubmitting, setAssemblySubmitting] = useState(false);
  const submittingAssembly = useRef(false);
  const hasPendingAssembly = Boolean(project?.id && pendingAssemblyProject === project.id);
  useEffect(() => {
    if (!project?.id) { setPendingAssemblyProject(''); return; }
    try { setPendingAssemblyProject(localStorage.getItem(`director-workbench:${project.id}:assembly-submission:v1`) ? project.id : ''); }
    catch { setNotice('无法读取装配提交凭据，请恢复浏览器存储后重试。'); }
  }, [project?.id]);
  const [batchId, setBatchId] = useState('');
  const [batchTasks, setBatchTasks] = useState<BatchTask[]>([]);
  const [pipeline, setPipeline] = useState<PipelineStage[]>(defaultPipelineStages);
  const [pipelineStageId, setPipelineStageId] = useState('');
  const [pipelineOpen, setPipelineOpen] = useState(false);
  const [inspectorTab, setInspectorTab] = useState<'intent' | 'materials' | 'params' | 'history'>('intent');
  const [shotEditTab, setShotEditTab] = useState<'materials' | 'prompt' | 'history'>('materials');
  const [shotVersions, setShotVersions] = useState<ShotVersion[]>([]);
  const [shotVersionsTotal, setShotVersionsTotal] = useState(0);
  const [shotVersionsOffset, setShotVersionsOffset] = useState(0);
  const [shotVersionsError, setShotVersionsError] = useState('');
  const [restoringVersion, setRestoringVersion] = useState('');
  const [generationEdit, setGenerationEdit] = useState<ShotGenerationSettings>({ mode: 'auto', aspect_ratio: '16:9', sound_mode: 'performance_reference', seed: 42 });
  const [generationDraft, setGenerationDraft] = useState<ShotGenerationSettings>({ mode: 'auto', aspect_ratio: '16:9', sound_mode: 'performance_reference', seed: 42 });
  const [pipelineValues, setPipelineValues] = useState<Record<string, string | number | boolean | string[]>>({});
  const [pipelineValidation, setPipelineValidation] = useState<PipelineValidation | null>(null);
  const [draftReadiness, setDraftReadiness] = useState<ReadinessItem | null>(null);
  const [pipelineError, setPipelineError] = useState('');
  const [pipelineChecking, setPipelineChecking] = useState(false);
  const [stageFormOpen, setStageFormOpen] = useState(false);
  const [segmentFormOpen, setSegmentFormOpen] = useState(false);
  const [scriptLinkSaving, setScriptLinkSaving] = useState(false);
  const [stageTitle, setStageTitle] = useState('');
  const [stagePurpose, setStagePurpose] = useState('');
  const [stageBackend, setStageBackend] = useState('混合流程');
  const [stageInputs, setStageInputs] = useState('');
  const [stageOutputs, setStageOutputs] = useState('');
  const [stageWorkflow, setStageWorkflow] = useState('');
  const [newSegmentDrafts, setNewSegmentDrafts] = useState<Record<string, SegmentDraft>>({});
  const segmentDraftKey = project?.id ?? '';
  const [segmentStorageError, setSegmentStorageError] = useState(false);
  const persistedSegmentDrafts = useRef<Record<string, SegmentDraft>>({});
  const segmentDraft = newSegmentDrafts[segmentDraftKey] ?? readNewSegmentDraft(segmentDraftKey);
  const setSegmentDraft = (update: SegmentDraft | ((previous: SegmentDraft) => SegmentDraft)) => {
    setNewSegmentDrafts(previous => ({ ...previous, [segmentDraftKey]: typeof update === 'function' ? update(previous[segmentDraftKey] ?? readNewSegmentDraft(segmentDraftKey)) : update }));
  };
  useEffect(() => {
    try {
      for (const [id, draft] of Object.entries(newSegmentDrafts)) {
        if (id && persistedSegmentDrafts.current[id] !== draft) {
          localStorage.setItem(`director-workbench:${id}:new-segment:v1`, JSON.stringify(draft));
          persistedSegmentDrafts.current[id] = draft;
        }
      }
      setSegmentStorageError(false);
    } catch { setSegmentStorageError(true); }
  }, [newSegmentDrafts]);
  const [creativeOpen, setCreativeOpen] = useState(false);
  const [creativeDraft, setCreativeDraft] = useState<CreativeDraft>(blankCreativeDraft());
  const [creativeConflict, setCreativeConflict] = useState<CreativeConflict | null>(null);
  const [reviewConflict, setReviewConflict] = useState<{ projectId: string; assetId: string; stage: ReviewRecord['stage']; state: ProjectState } | null>(null);
  const [projectState, setProjectState] = useState<ProjectState | null>(null);
  const [stateSaving, setStateSaving] = useState(false);
  const [segmentEdit, setSegmentEdit] = useState<SegmentEdit>({ duration_seconds: '', location: '', shot_size: '', camera: '', wardrobe: '', performance: '', prompt: '', workflow: '', first_frame: '', last_frame: '', audio_guide: '', delivery_master: '', dependencies: '' });
  useEffect(() => { setDraftReadiness(null); }, [project?.id, selected, segmentEdit]);
  const [planSegments, setPlanSegments] = useState<PlanSegment[]>([]);
  const [planRevision, setPlanRevision] = useState(0);
  const [mergeEnd, setMergeEnd] = useState('');
  useEffect(() => { setMergeEnd(''); }, [project?.id, selected]);
  const creatingSegment = useRef(false);
  const [planAssembly, setPlanAssembly] = useState<PlanAssembly | null>(null);
  const storageKey = `director-workbench:${project?.id ?? 'loading'}:drafts:v1`;
  const creativeStorageKey = `director-workbench:${project?.id ?? 'loading'}:creative:v1`;
  const baseAssets = project ? (Array.isArray(project.assets) ? project.assets.filter(isCreativeMaterial).map(normalizeManifestAsset) : []) : privateMode ? [] : fallbackAssets;
  const diagnosticPaths = diagnosticMaterialPaths(project);
  const isDiagnosticPath = (path?: string | null) => !!path && diagnosticPaths.some(item => materialPathKey(item) === materialPathKey(path));
  const emptyAsset: Asset = { id: 'empty-project', kind, title: '尚未登记素材', subtitle: '先从上方登记资源或创建分段', ready: false, intent: '这个项目还没有可预览的素材。先选择文件、登记制作步骤或添加分段计划。', prompt: '', origin: '空白项目', specs: '', workflow: '待登记' };
  const mediaRef = useRef<HTMLMediaElement | null>(null);
  const [mediaElement, setMediaElement] = useState<HTMLMediaElement | null>(null);
  const attachMedia = useCallback((node: HTMLMediaElement | null) => {
    mediaRef.current = node;
    setMediaElement(node);
  }, []);
  const mediaEpoch = useRef(0);
  const autoplayRef = useRef(false);
  const advancing = useRef(false);
  const refreshedPlanTaskRef = useRef('');
  const dialogRef = useRef<HTMLDialogElement>(null);
  const planVideoAssets: Asset[] = planSegments.map(segment => {
    const base = baseAssets.find(asset => asset.id === segment.id);
    const firstFrame = sharedMediaUrl(segment.keyframes?.first);
    const videoSource = sharedMediaUrl(segment.video?.path);
    const reworkSource = sharedMediaUrl(segment.video?.rework_candidate?.path);
    const finishSource = sharedMediaUrl(segment.video?.finish_review?.path);
    return {
      id: segment.id, kind: 'video', title: `${segment.id} · ${segment.location}`, subtitle: `${segment.shot_size} · ${segment.camera} · ${segment.wardrobe}`,
      start: segment.start_seconds, end: segment.end_seconds, ready: Boolean(videoSource) || Boolean(base?.ready), image: firstFrame ?? base?.image, lastImage: sharedMediaUrl(segment.keyframes?.last),
      sources: videoSource ? { A: videoSource, ...((reworkSource ?? finishSource) ? { B: reworkSource ?? finishSource } : {}) } : base?.sources, intent: segment.performance, prompt: segment.prompt ?? base?.prompt ?? '', origin: segment.video?.path ? `ComfyUI history 成功回执 · ${segment.comfyui_task_id ?? '任务 ID 未登记'}${segment.video.rework_candidate?.prompt_id ? ` · 重做样片 ${segment.video.rework_candidate.prompt_id}` : segment.video.finish_review?.prompt_id ? ` · 精细化候选 ${segment.video.finish_review.prompt_id}` : ''}` : (segment.keyframes?.first ? '生成前分段计划 · 已登记首帧' : '分镜草案 · 待准备本作关键帧'), specs: `${segment.duration_seconds} 秒 · ${segment.status}${segment.video?.rework_candidate?.specs ? ` · B ${segment.video.rework_candidate.specs}` : segment.video?.finish_review?.specs ? ` · B ${segment.video.finish_review.specs}` : ''}`, workflow: [segment.workflow ?? base?.workflow ?? '待登记', segment.video?.rework_candidate?.workflow ? `重做 ${segment.video.rework_candidate.workflow}` : '', segment.video?.rework_candidate?.ui_workflow ? `画布 ${segment.video.rework_candidate.ui_workflow}` : '', segment.video?.finish_review?.workflow ? `精细化 ${segment.video.finish_review.workflow}` : '', segment.keyframes?.first ? `首帧 ${segment.keyframes.first}` : '', segment.keyframes?.last ? `尾帧 ${segment.keyframes.last}` : '', segment.audio?.delivery_master ? `交付音频 ${segment.audio.delivery_master}` : '', segment.audio?.guide ? `引导音频 ${segment.audio.guide}` : '', segment.comfyui_task_id ? `ComfyUI 任务 ${segment.comfyui_task_id}` : ''].filter(Boolean).join(' · '),
    };
  });
  const planAudioAssets: Asset[] = planSegments.flatMap(segment => {
    const audioPath = segment.audio?.delivery_master ?? segment.audio?.guide;
    const audioSource = sharedMediaUrl(audioPath);
    if (!audioSource) return [];
    return [{
      id: segment.id, kind: 'audio', title: `${segment.id} · ${segment.audio?.delivery_master ? '交付音频' : '表演引导'}`,
      subtitle: `${segment.duration_seconds} 秒 · ${segment.audio?.delivery_master ? '已登记交付母版' : '参考演唱切片'}`,
      start: 0, end: segment.duration_seconds, ready: true, sources: { A: audioSource }, intent: segment.performance,
      prompt: segment.prompt ?? '', origin: audioPath ?? '', specs: '44.1 kHz · 分段音频', workflow: segment.workflow ?? '待登记',
    }];
  });
  const planImageAssets: Asset[] = planSegments.flatMap(segment => {
    const image = sharedMediaUrl(segment.keyframes?.first);
    if (!image || isDiagnosticPath(segment.keyframes?.first)) return [];
    return [{
      id: segment.id, kind: 'image', title: `${segment.id} · 首帧`, subtitle: `${segment.location} · ${segment.shot_size}`,
      ready: true, image, lastImage: sharedMediaUrl(segment.keyframes?.last), intent: segment.performance,
      prompt: segment.prompt ?? '', origin: segment.keyframes?.first ?? '', specs: '首帧候选', workflow: segment.workflow ?? '待登记',
    }];
  });
  const assembly = planAssembly;
  const assemblyIsStale = assembly?.status === 'stale';
  const assemblySource = sharedMediaUrl(assembly?.output);
  const assemblyAsset: Asset | null = assembly && assemblySource ? {
    id: project?.assembly_asset_id || 'assembly', kind: 'video', title: `${project?.title ?? '项目'} · ${assemblyIsStale ? '历史成片 · 需重新装配' : '当前成片'}`,
    subtitle: `${assembly.duration_seconds?.toFixed(2) ?? '—'} 秒 · ${assemblyIsStale ? '对应旧计划' : '已装配'}`,
    start: 0, end: assembly.duration_seconds ?? assembly.target_duration_seconds ?? 0,
    ready: true, sources: { A: assemblySource }, image: sharedMediaUrl(planSegments[0]?.keyframes?.first),
    intent: assemblyIsStale ? '这是旧计划的历史成片，仍可预览；当前计划已变化，请重新装配。' : '当前作品的成片。请检查音画后决定是否继续修改分镜。',
    prompt: '', origin: assembly.prompt_id ? `装配任务回执 · ${assembly.prompt_id}` : '装配结果 · 未登记任务回执',
    specs: `${assembly.duration_seconds?.toFixed(2) ?? '—'} 秒 · 分辨率与编码待核验`,
    workflow: assembly.workflow ?? '待登记',
  } : null;
  const derivedMaterialAssets = [...planAudioAssets, ...planImageAssets];
  const remainingBaseAssets = baseAssets.filter(asset => !planVideoAssets.some(shot => asset.kind === 'video' && shot.id === asset.id) && !derivedMaterialAssets.some(derived => derived.kind === asset.kind && derived.id === asset.id));
  const library = [...planVideoAssets, ...(assemblyAsset ? [assemblyAsset] : []), ...derivedMaterialAssets, ...remainingBaseAssets];
  const list = kind === 'video' && assemblyAsset?.id === selected
    ? [assemblyAsset]
    : library.filter(asset => asset.kind === kind && (kind !== 'video' || asset.id !== assemblyAsset?.id));
  const current = list.find(asset => asset.id === selected) ?? list[0] ?? emptyAsset;
  useEffect(() => {
    if (!pendingTreeItem || pendingTreeItem.projectId !== project?.id) return;
    const available = pendingTreeItem.kind === 'video'
      ? planSegments.some(segment => segment.id === pendingTreeItem.id) || project?.assets?.some(asset => asset.id === pendingTreeItem.id && asset.kind === 'video')
      : project?.assets?.some(asset => asset.id === pendingTreeItem.id && asset.kind === pendingTreeItem.kind) || planSegments.some(segment => segment.id === pendingTreeItem.id && (pendingTreeItem.kind === 'audio' ? Boolean(segment.audio?.guide || segment.audio?.delivery_master) : Boolean(segment.keyframes?.first)));
    if (!available) return;
    setKind(pendingTreeItem.kind);
    setSelected(pendingTreeItem.id);
    setWorkspaceMode('production');
    setWorkspacePage('shots');
    setPendingTreeItem(null);
  }, [pendingTreeItem, project?.id, project?.assets, planSegments]);
  const source = kind === 'video' && planSegments.some(segment => segment.id === current.id) ? current.sources?.A : current.sources?.[variant];
  const mediaKey = JSON.stringify([project?.id, current.id, source]);
  const mediaStatus = mediaLoad.key === mediaKey ? mediaLoad.status : 'loading';
  const playbackDisabled = !current.ready || (kind !== 'image' && mediaStatus !== 'ready');
  const index = list.findIndex(asset => asset.id === current.id);
  const start = kind === 'audio' ? current.start ?? 0 : 0;
  const duration = (current.end ?? 0) - (current.start ?? 0);
  const activeTaskStates = ['preparing', 'submitting', 'queued', 'scheduler_waiting', 'running', 'stop_requested', 'stopping', 'batch_waiting', 'needs_reconcile'];
  const batchActive = batchTasks.some(task => activeTaskStates.includes(task.status));
  const resourceReleaseBlocked = batchActive || activeTaskStates.includes(taskState) || activeTaskStates.includes(assemblyTaskState);
  const currentPlanSegment = planSegments.find(segment => segment.id === current.id);
  const ownedAgentDraft = agentDraft.current?.projectId === segmentDraftKey &&
    agentDraft.current.interaction === pageInteraction.current &&
    JSON.stringify(agentDraft.current.draft) === JSON.stringify(segmentDraft) &&
    JSON.stringify(agentDraft.current.generation) === JSON.stringify(generationDraft);
  const agentView = useRef({ projectId: project?.id, assetId: current.id, source });
  agentView.current = { projectId: project?.id, assetId: current.id, source };
  const savedReadiness = projectState?.readiness;
  const currentReadiness = savedReadiness?.segments.find(segment => segment.asset_id === current.id);
  const isAssemblyAsset = Boolean(project?.assembly_asset_id && current.id === project.assembly_asset_id);
  const isProjectVideoMaterial = kind === 'video' && !currentPlanSegment && !isAssemblyAsset && current.id !== 'empty-project';
  const focusList = kind === 'video' ? (isProjectVideoMaterial || isAssemblyAsset ? [current] : planVideoAssets) : list;
  const focusIndex = focusList.findIndex(asset => asset.id === current.id);
  const editableMaterial = !currentPlanSegment && !isAssemblyAsset ? project?.assets?.find(asset => asset.id === current.id && asset.kind === kind) : undefined;
  useEffect(() => { setMaterialTitle(editableMaterial ? String(editableMaterial.title ?? '') : ''); }, [project?.id, current.id, editableMaterial?.title]);
  const currentTaskTemplate = project?.task_templates?.[current.id];
  const canReviewCurrent = kind === 'video' && !isProjectVideoMaterial && (current.ready || taskState === 'succeeded');
  const materialCandidateRef = current.sources?.[variant] ?? (kind === 'image' && variant === 'A' ? current.image : undefined);
  const canReviewAsset = canReviewCurrent || ((kind === 'image' || kind === 'audio') && current.ready && Boolean(materialCandidateRef));
  const currentExecutable = kind === 'video' && !isAssemblyAsset && Boolean(currentReadiness?.stage_id);
  const timelineAssets = kind === 'video' ? planVideoAssets : list;
  const currentAudioPath = currentPlanSegment?.audio?.delivery_master ?? currentPlanSegment?.audio?.guide;
  const currentAudioSource = currentAudioPath
    ? sharedMediaUrl(currentAudioPath)
    : kind === 'audio' ? source : undefined;
  const currentSampleRecord = projectState?.reviews[current.id]?.sample;
  const currentFinishRecord = projectState?.reviews[current.id]?.finish;
  const currentFinalRecord = projectState?.reviews[current.id]?.final;
  const currentAttempts = projectTasks.filter(task => task.asset_id === current.id);
  const currentReview = isProjectVideoMaterial ? '参考素材' : current.ready ? reviewLabel((isAssemblyAsset ? currentFinalRecord : currentSampleRecord)?.status) : '待准备';
  const currentFinishReview = reviewLabel(currentFinishRecord?.status);
  const currentPrompt = drafts[current.id]?.prompt ?? currentPlanSegment?.prompt ?? currentTaskTemplate?.prompt ?? current.prompt;
  const currentNote = drafts[current.id]?.note ?? (isAssemblyAsset ? currentFinalRecord?.note : currentSampleRecord?.note) ?? currentPlanSegment?.visual_review?.reason ?? '';
  const currentPipeline = pipeline.filter(stage => isAssemblyAsset
    ? stage.id === 'assembly'
    : kind === 'audio'
      ? stage.id.includes('audio') || stage.id.includes('voice')
      : kind === 'image'
        ? stage.id.includes('frame') || stage.outputs.some(output => output.includes('图') || output.includes('帧'))
        : !stage.id.includes('assembly') && !stage.id.includes('audio-master'));
  const currentGenerationStage = kind === 'video' && !isAssemblyAsset && !isProjectVideoMaterial
    ? currentPipeline.find(stage => stage.id === currentReadiness?.stage_id) ?? selectVideoGenerationStage(currentPipeline, currentPlanSegment?.workflow || currentTaskTemplate?.workflow || undefined, project?.production_preset?.workflow) : undefined;
  const activePipelineStage = currentPipeline.find(stage => stage.id === pipelineStageId) ?? currentGenerationStage ?? currentPipeline[0];
  const durationInput = currentGenerationStage?.inputSpecs?.find(input => input.id === 'duration');
  const isH3Stage = Boolean(currentGenerationStage?.recipe.startsWith('H3 '));
  const autoCreation = currentPlanSegment ? currentPlanSegment.generation?.mode === 'auto' : project?.default_creation_mode === 'auto';
  useEffect(() => { setGenerationEdit(currentPlanSegment?.generation ?? { mode: 'advanced', aspect_ratio: '16:9', sound_mode: 'performance_reference', seed: 42 }); }, [project?.id, currentPlanSegment?.id, currentPlanSegment?.generation?.mode, currentPlanSegment?.generation?.aspect_ratio, currentPlanSegment?.generation?.sound_mode, currentPlanSegment?.generation?.seed]);
  const isTextGeneration = currentGenerationStage?.generation_mode === 'text_to_video';
  const supportsH3Duration = isH3Stage && durationInput?.min === 4 && durationInput?.max === 15;
  const readinessGaps = currentReadiness?.blockers.map(blockerMessage) ?? ['正在读取服务器准备度…'];
  const candidateA = current.sources?.A ?? source;
  const candidateB = current.sources?.B;
  const currentCandidateBIsRework = Boolean(currentPlanSegment?.video?.rework_candidate?.path);
  const currentCandidateBIsFinish = Boolean(!currentCandidateBIsRework && currentPlanSegment?.video?.finish_review?.path);
  const taskIsActive = activeTaskStates.includes(taskState);
  const hasPlanCheckpoint = Boolean(currentPlanSegment) || planSegments.length > 0;
  const hasMaterialCheckpoint = Boolean(current.image || current.lastImage || currentAudioSource || (current.workflow && current.workflow !== '待登记'));
  const hasGeneratedCheckpoint = Boolean(canReviewCurrent);
  const hasApprovalCheckpoint = currentSampleRecord?.status === 'approved';
  const hasFinishCheckpoint = currentFinishRecord?.status === 'approved';
  const finishRejected = currentPlanSegment?.video?.finish_review?.status?.startsWith('rejected') ?? false;
  const assemblyReady = savedReadiness?.assembly.ready === true;
  const hasAssemblyArtifact = Boolean(planAssembly?.output);
  const hasAssemblyCheckpoint = assemblyReady && hasAssemblyArtifact && !assemblyIsStale;
  const checkpointFor = (stageId: string) => projectState?.checkpoints[`${current.id}:${stageId}`] ?? projectState?.checkpoints[stageId];
  const hasIntentContent = Boolean(creativeDraft.intent.trim() || creativeDraft.world.trim() || creativeDraft.reference_series.trim() || creativeDraft.adaptation.trim());
  const hasCharacterContent = Boolean(creativeDraft.character.trim() || creativeDraft.voice.trim());
  const hasSceneContent = Boolean(currentPlanSegment?.location);
  const hasPerformanceContent = current.id !== 'empty-project' && Boolean(currentPlanSegment?.performance || current.intent);
  const hasFinalReviewCheckpoint = Boolean(hasAssemblyCheckpoint && assemblyAsset && currentFinalRecord?.status === 'approved');
  const stepState = (checkpoint: CheckpointRecord | undefined, hasContent: boolean): ProductionStepState => checkpoint?.status === 'approved' ? 'done' : checkpoint?.status === 'changes_requested' ? 'blocked' : hasContent ? 'active' : 'waiting';
  const productionRail: ProductionStep[] = [
    { id: 'intent', label: '创作意图', detail: checkpointLabel(checkpointFor('intent'), hasIntentContent), state: stepState(checkpointFor('intent'), hasIntentContent), mode: 'manual' },
    { id: 'character', label: '角色 / 声音', detail: checkpointLabel(checkpointFor('character'), hasCharacterContent), state: stepState(checkpointFor('character'), hasCharacterContent), mode: 'manual' },
    { id: 'scene', label: '场景设定', detail: checkpointLabel(checkpointFor('scene'), hasSceneContent), state: stepState(checkpointFor('scene'), hasSceneContent), mode: 'manual' },
    { id: 'performance', label: '动作表演', detail: checkpointLabel(checkpointFor('performance'), hasPerformanceContent), state: stepState(checkpointFor('performance'), hasPerformanceContent), mode: 'manual' },
    { id: 'storyboard', label: '分镜 / 节拍', detail: checkpointLabel(checkpointFor('storyboard'), hasPlanCheckpoint), state: stepState(checkpointFor('storyboard'), hasPlanCheckpoint), mode: 'manual' },
    { id: 'materials', label: '素材准备', detail: checkpointLabel(checkpointFor('materials'), hasMaterialCheckpoint), state: stepState(checkpointFor('materials'), hasMaterialCheckpoint), mode: 'hybrid' },
    { id: 'generate', label: '生成任务', detail: taskIsActive ? (taskStateLabels[taskState] ?? taskState) : hasGeneratedCheckpoint ? '候选已生成' : '等待提交', state: taskIsActive ? 'active' : hasGeneratedCheckpoint ? 'done' : 'waiting', mode: 'auto' },
    { id: 'review', label: '片段审核', detail: hasApprovalCheckpoint ? '已采用' : hasGeneratedCheckpoint ? '等待决定' : '生成后开放', state: hasApprovalCheckpoint ? 'done' : hasGeneratedCheckpoint ? 'active' : 'waiting', mode: 'manual' },
    { id: 'picture-finish', label: '精细化 / 超分', detail: hasFinishCheckpoint ? '精细化版本已采用' : finishRejected ? '源画面驳回，先重做样片' : hasApprovalCheckpoint ? '已开放，可生成精细版' : '片段采用后开放', state: hasFinishCheckpoint ? 'done' : hasApprovalCheckpoint ? 'active' : 'waiting', mode: 'hybrid' },
    { id: 'assembly', label: '最终装配', detail: assemblyIsStale ? '需重新装配' : hasAssemblyCheckpoint ? '成片候选已回执' : savedReadiness?.assembly.blockers.map(blockerMessage).join(' · ') || (assemblyReady ? '可提交装配' : readinessStateLabel(savedReadiness?.assembly.state)), state: hasAssemblyCheckpoint ? 'done' : assemblyReady ? 'active' : 'waiting', mode: 'auto' },
    { id: 'final-review', label: '全片审核', detail: hasFinalReviewCheckpoint ? '已采用' : hasAssemblyCheckpoint ? '等待全片决定' : '最终装配后开放', state: hasFinalReviewCheckpoint ? 'done' : hasAssemblyCheckpoint ? 'active' : 'waiting', mode: 'manual' },
  ];
  const checkpointSummary = productionRail.map(step => `${step.label}:${step.detail}`).join(' · ');
  const timelineCountLabel = planSegments.length ? `${planSegments.length} 个分镜${assemblyAsset ? ' · 含完整版候选' : ''}` : `${timelineAssets.length} 个${labels[kind]}`;

  useEffect(() => {
    let cancelled = false;
    fetch('/api/project').then(response => response.ok ? response.json() : Promise.reject(new Error('project'))).then(data => {
      if (cancelled) return;
      setProject(data as ProjectManifest);
      const manifestAssets: Asset[] = Array.isArray(data.assets) ? data.assets.map((asset: Record<string, unknown>) => normalizeManifestAsset(asset)) : [];
      setSelected('');
      setExpanded(manifestAssets.slice(0, 1).map(asset => asset.id));
      // Open the production page in a quiet overview state. Parameter controls
      // appear only after the reviewer chooses a concrete step.
      setPipelineStageId('');
      setDrafts(loadDrafts(`director-workbench:${String(data.id)}:drafts:v1`));
      setCreativeDraft(loadCreativeDraft(`director-workbench:${String(data.id)}:creative:v1`));
      setCreativeConflict(null);
      void refreshProjects();
    }).catch(() => { if (!cancelled) {
      setNotice(privateMode ? '正在读取你的作品…' : '项目清单暂时不可用，当前仅显示离线占位。');
      if (privateMode) void refreshProjects().then(items => {
        if (cancelled) return;
        const last = localStorage.getItem('director-workbench:last-project');
        const initial = items.find(item => item.id === last) ?? items[0];
        if (initial) void selectProject(initial.id);
        else setNotice('还没有作品。可以使用上方“新建作品”开始创作。');
      }).catch(() => { if (!cancelled) setNotice('作品列表读取失败，请刷新重试。'); });
    } });
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    if (!project) return;
    setPipeline((project.pipeline ?? []).map(raw => {
      const stage = raw as PipelineStage & { input_specs?: PipelineStage['inputSpecs']; output_specs?: PipelineStage['outputSpecs'] };
      return { ...stage, inputSpecs: stage.inputSpecs ?? stage.input_specs, outputSpecs: stage.outputSpecs ?? stage.output_specs };
    }));
  }, [project]);

  useEffect(() => {
    if (!privateMode) return;
    const timer = window.setInterval(() => { void refreshProjects().catch(() => {}); }, 30000);
    return () => window.clearInterval(timer);
  }, [privateMode]);

  useEffect(() => {
    if (!project) return;
    try { localStorage.setItem(storageKey, JSON.stringify(drafts)); setStorageError(false); }
    catch { setStorageError(true); }
  }, [drafts, project, storageKey]);

  useEffect(() => {
    if (!project) return;
    try { localStorage.setItem(creativeStorageKey, JSON.stringify(creativeDraft)); setStorageError(false); }
    catch { setStorageError(true); }
  }, [creativeDraft, creativeStorageKey, project]);

  useEffect(() => {
    let cancelled = false;
    const refresh = async () => {
      try {
        const response = await fetch('/api/status');
        if (!response.ok) throw new Error('status');
        const data = await response.json();
        if (!cancelled) { setComfyOnline(Boolean(data.comfy?.online)); setComfyInfo(data.comfy ?? {}); }
      } catch { if (!cancelled) setComfyOnline(false); }
    };
    void refresh();
    const timer = window.setInterval(refresh, 4000);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, []);

  useEffect(() => {
    void refreshPlan().catch(() => { /* the static pilot remains available while the plan is offline */ });
    void refreshProjectState().catch(() => { /* local drafts remain available while the archive is offline */ });
    setTaskId(''); setTaskState(''); setCurrentTask(null); setProjectTasks([]); setAssemblyTaskId(''); setAssemblyTaskState(''); setBatchId(''); setBatchTasks([]);
  }, [project?.id]);

  useEffect(() => {
    let cancelled = false;
    setTaskId(''); setTaskState(''); setCurrentTask(null);
    if (!project?.id || !current.id) return () => { cancelled = true; };
    fetch(`/api/projects/${encodeURIComponent(project.id)}/tasks?limit=100`).then(response => response.ok ? response.json() : Promise.reject(new Error('tasks'))).then(({ tasks: records }: { tasks: TaskRecord[] }) => {
      if (cancelled) return;
      const scoped = records.filter(task => task.project_id === project.id);
      setProjectTasks(scoped);
      const task = scoped.find(item => item.asset_id === current.id) ?? null;
      setCurrentTask(task); setTaskId(task?.id ?? ''); setTaskState(normalizeLiveTaskState(task?.status ?? ''));
      const assemblyTask = project.assembly_asset_id ? scoped.find(item => item.asset_id === project.assembly_asset_id) : undefined;
      setAssemblyTaskId(assemblyTask?.id ?? ''); setAssemblyTaskState(assemblyTask?.status ?? '');
    }).catch(() => { /* SSE will retry live task state */ });
    return () => { cancelled = true; };
  }, [project?.id, project?.assembly_asset_id, current.id]);

  useEffect(() => {
    // Restore presentation from the same persisted tasks used by HTTP and SSE.
    const grouped = new Map<string, TaskRecord[]>();
    for (const task of projectTasks) {
      if (task.project_id !== project?.id || !task.batch_id) continue;
      grouped.set(task.batch_id, [...(grouped.get(task.batch_id) ?? []), task]);
    }
    const groups = [...grouped.entries()].sort((a, b) =>
      Math.max(...b[1].map(task => task.created_at ?? 0)) - Math.max(...a[1].map(task => task.created_at ?? 0)));
    const active = groups.filter(([, tasks]) => tasks.some(task => activeTaskStates.includes(task.status)));
    const selected = active.find(([id]) => id === batchId) ?? active[0] ?? groups[0];
    if (selected) {
      setBatchId(selected[0]);
      setBatchTasks([...selected[1]].sort((a, b) => (a.sequence ?? 0) - (b.sequence ?? 0)));
    } else {
      setBatchId('');
      setBatchTasks([]);
    }
  }, [projectTasks, project?.id]);

  useEffect(() => { setShotEditTab('materials'); }, [project?.id, current.id]);
  useEffect(() => { setShotVersionsOffset(0); setShotVersions([]); setShotVersionsTotal(0); setShotVersionsError(''); }, [project?.id, current.id, planRevision]);
  useEffect(() => {
    const projectId = project?.id;
    const segmentId = currentPlanSegment?.id;
    if (!projectId || !segmentId || kind !== 'video') return;
    let cancelled = false;
    fetch(`/api/projects/${encodeURIComponent(projectId)}/segments/${encodeURIComponent(segmentId)}/versions?limit=20&offset=${shotVersionsOffset}`)
      .then(async response => { const data = await response.json(); if (!response.ok) throw new Error(data.detail?.message || data.detail || '历史记录读取失败'); return data as { items: ShotVersion[]; total: number }; })
      .then(data => { if (cancelled || activeProjectId.current !== projectId) return; setShotVersions(previous => shotVersionsOffset ? [...previous, ...data.items] : data.items); setShotVersionsTotal(data.total); setShotVersionsError(''); })
      .catch(error => { if (!cancelled) setShotVersionsError(error instanceof Error ? error.message : '历史记录读取失败'); });
    return () => { cancelled = true; };
  }, [project?.id, currentPlanSegment?.id, kind, planRevision, shotVersionsOffset]);

  useEffect(() => {
    if (!currentPlanSegment) return;
    setSegmentEdit({
      duration_seconds: String(currentPlanSegment.duration_seconds ?? ''), location: currentPlanSegment.location ?? '', shot_size: currentPlanSegment.shot_size ?? '', camera: currentPlanSegment.camera ?? '', wardrobe: currentPlanSegment.wardrobe ?? '',
      performance: currentPlanSegment.performance ?? '', prompt: drafts[currentPlanSegment.id]?.prompt ?? currentPlanSegment.prompt ?? '', workflow: currentPlanSegment.workflow ?? '',
      first_frame: currentPlanSegment.keyframes?.first ?? '', last_frame: currentPlanSegment.keyframes?.last ?? '', audio_guide: currentPlanSegment.audio?.guide ?? '', delivery_master: currentPlanSegment.audio?.delivery_master ?? '', dependencies: (currentPlanSegment.dependencies ?? []).join(', '),
    });
  }, [currentPlanSegment?.id, currentPlanSegment?.duration_seconds, currentPlanSegment?.location, currentPlanSegment?.performance, currentPlanSegment?.prompt, currentPlanSegment?.workflow, currentPlanSegment?.keyframes?.first, currentPlanSegment?.keyframes?.last, currentPlanSegment?.audio?.guide, currentPlanSegment?.audio?.delivery_master]);

  useEffect(() => {
    setPipelineError('');
    if (!activePipelineStage) { setPipelineValues({}); setPipelineValidation(null); return; }
    const defaults: Record<string, string | number | boolean | string[]> = {};
    const template = project?.task_templates?.[current.id];
    for (const input of activePipelineStage.inputSpecs ?? []) {
      if (input.id === 'first-frame') defaults[input.id] = currentPlanSegment?.keyframes?.first ?? template?.first_frame ?? '';
      else if (input.id === 'last-frame') defaults[input.id] = currentPlanSegment?.keyframes?.last ?? template?.last_frame ?? '';
      else if (input.id === 'guide') defaults[input.id] = currentPlanSegment?.audio?.guide ?? template?.audio_guide ?? '';
      else if (input.id === 'prompt') defaults[input.id] = currentPrompt ?? '';
      else if (input.id === 'seed' && currentPlanSegment?.generation) defaults[input.id] = currentPlanSegment.generation.seed;
      else if (input.id === 'seed' && template?.seed !== undefined) defaults[input.id] = template.seed;
      else if (input.id === 'duration' && currentPlanSegment?.duration_seconds !== undefined) defaults[input.id] = currentPlanSegment.duration_seconds;
      else if (input.id === 'turbo-sampling') defaults[input.id] = 'enable';
      else if (input.example !== undefined) defaults[input.id] = input.example;
      else if (input.control === 'multi-select') defaults[input.id] = [];
    }
    setPipelineValues(defaults);
    setPipelineValidation(null);
  }, [project?.id, current.id, activePipelineStage?.id]);

  useEffect(() => {
    if (!currentPlanSegment || activePipelineStage?.id !== currentGenerationStage?.id) return;
    setPipelineValues(previous => ({ ...previous,
      'first-frame': currentPlanSegment.keyframes?.first ?? '',
      'last-frame': currentPlanSegment.keyframes?.last ?? '',
      guide: currentPlanSegment.audio?.guide ?? '',
      duration: currentPlanSegment.duration_seconds,
      ...(currentPlanSegment.generation ? { seed: currentPlanSegment.generation.seed } : {}),
    }));
    setPipelineValidation(null);
  }, [currentPlanSegment?.keyframes?.first, currentPlanSegment?.keyframes?.last, currentPlanSegment?.audio?.guide, currentPlanSegment?.duration_seconds, currentPlanSegment?.generation?.seed]);

  useEffect(() => {
    let cancelled = false;
    if (privateMode && !project?.id) return;
    fetch(project?.id ? `/api/projects/${encodeURIComponent(project.id)}/pipeline` : '/api/pipeline').then(response => response.ok ? response.json() : Promise.reject(new Error('pipeline'))).then(data => {
      if (!cancelled && Array.isArray(data.stages)) {
        const defaults = new Map(defaultPipelineStages.map(stage => [stage.id, stage]));
        const normalized = data.stages.map((raw: PipelineStage & { input_specs?: PipelineStage['inputSpecs']; output_specs?: PipelineStage['outputSpecs'] }) => {
          const base = defaults.get(raw.id);
          return { ...base, ...raw, inputSpecs: raw.inputSpecs ?? raw.input_specs ?? base?.inputSpecs, outputSpecs: raw.outputSpecs ?? raw.output_specs ?? base?.outputSpecs } as PipelineStage;
        });
        setPipeline(normalized);
      }
    }).catch(() => { /* static stage contracts remain available while the API is offline */ });
    return () => { cancelled = true; };
  }, [project?.id]);

  useEffect(() => {
    if (!project?.id) return;
    const sourceEvents = new EventSource(`/api/projects/${encodeURIComponent(project.id)}/events`);
    sourceEvents.onmessage = event => {
      try {
        const data = JSON.parse(event.data);
        const projectTasks = (data.tasks ?? []).filter((task: TaskRecord) => task.project_id === project?.id);
        setProjectTasks(projectTasks);
        const currentTask = projectTasks.find((task: TaskRecord) => task.asset_id === current.id);
        if (currentTask) { setCurrentTask(currentTask); setTaskState(normalizeLiveTaskState(currentTask.status ?? '')); setTaskId(currentTask.id ?? ''); }
        else { setCurrentTask(null); setTaskState(''); setTaskId(''); }
        const assemblyTask = project?.assembly_asset_id ? projectTasks.find((task: TaskRecord) => task.asset_id === project.assembly_asset_id) : undefined;
        if (assemblyTask) {
          setAssemblyTaskState(assemblyTask.status);
          setAssemblyTaskId(assemblyTask.id ?? '');
        }
        if (batchId) {
          const currentBatchTasks = projectTasks.filter((task: BatchTask) => task.batch_id === batchId);
          if (currentBatchTasks.length) setBatchTasks(currentBatchTasks);
        }
        const completedPlanTask = projectTasks.find((task: TaskRecord) => task.asset_id && task.status === 'succeeded');
        if (completedPlanTask?.id && completedPlanTask.id !== refreshedPlanTaskRef.current) {
          refreshedPlanTaskRef.current = completedPlanTask.id;
          void refreshPlan().catch(() => { refreshedPlanTaskRef.current = ''; });
        }
      } catch { /* a transient event does not affect the media review UI */ }
    };
    return () => sourceEvents.close();
  }, [batchId, project?.id, project?.assembly_asset_id, current.id]);

  useLayoutEffect(() => {
    mediaEpoch.current += 1;
    return () => { mediaEpoch.current += 1; };
  }, [mediaKey, start, mediaElement]);

  useEffect(() => {
    advancing.current = false;
    setPosition(0);
    setPlaying(false);
    const media = mediaElement;
    if (!media) return;
    let cancelled = false;
    let initialized = false;
    const epoch = mediaEpoch.current;
    setMediaLoad({ key: mediaKey, status: 'loading' });
    const initialize = () => {
      if (cancelled || mediaRef.current !== media || mediaEpoch.current !== epoch) return;
      if (media.error) { setMediaLoad({ key: mediaKey, status: 'error' }); return; }
      if (media.readyState < 3) return;
      setMediaLoad({ key: mediaKey, status: 'ready' });
      if (initialized) return;
      initialized = true;
      media.currentTime = start;
      if (autoplayRef.current) {
        autoplayRef.current = false;
        void media.play().catch(error => { if (!cancelled && mediaEpoch.current === epoch && mediaRef.current === media && error?.name !== 'AbortError') setNotice('浏览器暂停了自动播放，请点击播放继续。'); });
      } else { media.pause(); }
    };
    const events = ['loadedmetadata', 'loadeddata', 'canplay', 'canplaythrough', 'progress', 'playing'];
    for (const event of events) media.addEventListener(event, initialize);
    initialize();
    return () => { cancelled = true; for (const event of events) media.removeEventListener(event, initialize); };
  }, [mediaKey, start, mediaElement]);

  useEffect(() => {
    if (lightbox) dialogRef.current?.showModal();
    else dialogRef.current?.close();
  }, [lightbox]);

  function choose(asset: Asset, auto = false) {
    mediaRef.current?.pause();
    autoplayRef.current = auto;
    setPlaying(false);
    setKind(asset.kind);
    setSelected(asset.id);
    setNotice('');
  }

  function openTreeItem(itemKind: TreeKind, id: string) {
    mediaRef.current?.pause();
    setKind(itemKind);
    setSelected(id);
    setWorkspaceMode('production');
    setWorkspacePage('shots');
    setNotice('');
  }

  function openTreeProject(projectId: string, item?: { kind: TreeKind; id: string }) {
    setArchive(null);
    if (item) setPendingTreeItem({ projectId, ...item });
    if (projectId !== project?.id) void selectProject(projectId);
    else if (item) openTreeItem(item.kind, item.id);
  }

  function resizeSidebar(next: number) {
    const width = Math.max(240, Math.min(Math.floor(window.innerWidth / 2), next));
    setSidebarWidth(width);
    localStorage.setItem('director-workbench:sidebar-width', String(width));
  }

  function beginSidebarResize(event: ReactPointerEvent<HTMLDivElement>) {
    event.preventDefault();
    const move = (pointer: PointerEvent) => resizeSidebar(pointer.clientX);
    const finish = () => { window.removeEventListener('pointermove', move); window.removeEventListener('pointerup', finish); };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', finish);
  }

  function switchKind(next: Kind) {
    mediaRef.current?.pause();
    autoplayRef.current = false;
    setPlaying(false);
    setKind(next);
    setSelected(next === 'video' ? (planSegments[0]?.id ?? baseAssets.find(asset => asset.kind === 'video')?.id ?? '') : next === 'audio' ? (baseAssets.find(asset => asset.kind === 'audio')?.id ?? '') : (baseAssets.find(asset => asset.kind === 'image')?.id ?? ''));
    setNotice('');
  }

  function finish() {
    if (advancing.current) return;
    advancing.current = true;
    mediaRef.current?.pause();
    setPlaying(false);
    if (continuous && focusList[focusIndex + 1]?.ready) choose(focusList[focusIndex + 1], true);
    else {
      autoplayRef.current = false;
      setNotice(continuous && focusList[focusIndex + 1] ? '下一段尚未生成，连续预览已暂停。' : '本次播放结束。');
    }
  }

  async function togglePlay() {
    const media = mediaRef.current;
    if (!media || playbackDisabled) return;
    const epoch = mediaEpoch.current;
    if (!media.paused) { media.pause(); return; }
    advancing.current = false;
    if (media.ended || (duration > 0 && media.currentTime >= start + duration - 0.08)) media.currentTime = start;
    try { await media.play(); if (mediaEpoch.current === epoch && mediaRef.current === media && media.isConnected) setNotice(''); } catch (error) { if (mediaEpoch.current === epoch && mediaRef.current === media && media.isConnected && !(error instanceof DOMException && error.name === 'AbortError')) setNotice('暂时无法播放，请检查素材是否可读取。'); }
  }

  function updateDraft(id: string, patch: Draft) {
    if ('prompt' in patch) setDraftReadiness(null);
    setDrafts(previous => ({ ...previous, [id]: { ...previous[id], ...patch } }));
  }

  async function persistReview(stage: 'sample' | 'finish' | 'final', status: ReviewRecord['status'], adoptedVariant?: Variant) {
    if (!current.id || !project?.id) return;
    if (!canReviewAsset) { setNotice('请先选择有实际文件的候选，再提交审核。'); return; }
    if (stage === 'final' && status === 'approved' && assemblyIsStale) { setNotice('当前计划已变化，请重新装配后再通过全片审核。'); return; }
    if (kind !== 'video' && stage !== 'sample') { setNotice('图片和音频素材仅支持素材候选审核。'); return; }
    if (reviewConflict?.projectId === project.id && reviewConflict.assetId === current.id) { setNotice('请先处理审核版本冲突，再保存或采用候选。'); return; }
    setStateSaving(true);
    try {
      const response = await fetch(`/api/projects/${encodeURIComponent(project.id)}/reviews`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ expected_revision: projectState?.reviews[current.id]?.[stage]?.revision ?? 0, ...(kind === 'video' && status === 'approved' ? { expected_plan_revision: planRevision } : {}), asset_id: current.id, stage, status, note: currentNote, adopted_variant: adoptedVariant, candidate_ref: kind === 'video' ? current.sources?.[adoptedVariant ?? variant] ?? null : materialCandidateRef, source: 'director-ui' }),
      });
      const data = await response.json();
      if (response.status === 409 && data.detail?.code === 'record_revision_conflict') {
        const latestResponse = await fetch(`/api/projects/${encodeURIComponent(project.id)}/state`);
        if (!latestResponse.ok) throw new Error('审核已更新，暂时无法读取最新记录，请稍后重试。');
        const latest = await latestResponse.json() as ProjectState;
        if (latest.project_id !== project.id) throw new Error('审核记录的作品归属不匹配。');
        setReviewConflict({ projectId: project.id, assetId: current.id, stage, state: latest });
      }
      if (!response.ok) throw new Error(data.detail?.message || data.detail || '审核保存失败');
      await refreshProjectState();
      setNotice(data.status === 'stale' && status === 'approved' ? data.stale_reason || '审核未绑定当前候选，请重新读取并确认。' : status === 'approved' ? `${assetLabel(current.id, kind)} ${stage === 'finish' ? '精细版' : stage === 'final' ? '全片' : kind === 'video' ? '样片' : '素材候选'}已采用，版本 ${data.revision}。` : `${assetLabel(current.id, kind)} 审核意见已保存，版本 ${data.revision}。`);
    } catch (error) { setNotice(error instanceof Error ? error.message : '审核保存失败。'); }
    finally { setStateSaving(false); }
  }

  async function adoptCurrent() {
    if (!current.id) return;
    if (!canReviewAsset) { setNotice('请先选择有实际文件的候选，再审核或采用。'); return; }
    if (kind !== 'video') { await persistReview('sample', 'approved', variant); return; }
    if (isAssemblyAsset) { await persistReview('final', 'approved', variant); return; }
    const adoptingFinishCandidate = kind === 'video' && variant === 'B' && currentCandidateBIsFinish;
    if (adoptingFinishCandidate) {
      if (!hasApprovalCheckpoint) { setNotice('请先采用低成本样片，再采用精细化版本。'); return; }
      await persistReview('finish', 'approved', variant);
      return;
    }
    await persistReview('sample', 'approved', variant);
  }

  async function refreshPlan() {
    if (privateMode && !project?.id) return;
    const requestedProjectId = project?.id;
    const response = await fetch(project?.id ? `/api/projects/${encodeURIComponent(project.id)}/plan` : '/api/plan');
    if (!response.ok) throw new Error('plan');
    const data = await response.json();
    if (activeProjectId.current !== requestedProjectId) return;
    if (Array.isArray(data.segments)) setPlanSegments(data.segments as PlanSegment[]);
    setPlanRevision(Number(data.revision) || 0);
    setPlanAssembly(data.assembly && typeof data.assembly === 'object' ? data.assembly as PlanAssembly : null);
    void refreshProjectState().catch(() => setNotice('服务器准备度读取失败，请刷新后重试。'));
  }

  async function restoreShotVersion(version: ShotVersion) {
    if (!project?.id || !currentPlanSegment || restoringVersion) return;
    const projectId = project.id;
    const segmentId = currentPlanSegment.id;
    setRestoringVersion(version.task_id);
    try {
      const response = await fetch(`/api/projects/${encodeURIComponent(projectId)}/segments/${encodeURIComponent(segmentId)}/versions/${encodeURIComponent(version.task_id)}/restore`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ expected_plan_revision: planRevision }) });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail?.message || data.detail || '恢复历史版本失败');
      if (activeProjectId.current !== projectId) return;
      setDrafts(previous => { const next = { ...previous }; delete next[segmentId]; return next; });
      setShotVersionsOffset(0);
      await refreshPlan();
      setNotice(`已恢复 ${localSystemTime(version.generated_at)} 生成的版本。原生成时间保留，本次恢复时间另行记录。`);
    } catch (error) { if (activeProjectId.current === projectId) setNotice(error instanceof Error ? error.message : '恢复历史版本失败'); }
    finally { setRestoringVersion(''); }
  }

  async function refreshProjectState() {
    if (privateMode && !project?.id) return;
    const requestedProjectId = project?.id;
    const response = await fetch(project?.id ? `/api/projects/${encodeURIComponent(project.id)}/state` : '/api/project-state');
    if (!response.ok) throw new Error('project-state');
    const data = await response.json() as ProjectState;
    if (activeProjectId.current !== requestedProjectId) return;
    if (project?.id && data.project_id !== project.id) return;
    setProjectState(data);
    const creativeRecord = data.creative;
    const persisted = creativeRecord?.values;
    if (persisted) {
      const serverDraft = normalizeCreativeDraft(persisted);
      const localDraft = project?.id ? loadCreativeDraft(`director-workbench:${project.id}:creative:v1`) : blankCreativeDraft();
      if (hasCreativeDraftContent(localDraft) && !sameCreativeDraft(localDraft, serverDraft)) {
        setCreativeDraft(localDraft);
        setCreativeConflict({ server: serverDraft, revision: creativeRecord.revision, status: creativeRecord.status, created_at: creativeRecord.created_at });
      } else {
        setCreativeConflict(null);
        setCreativeDraft(localDraft && hasCreativeDraftContent(localDraft) ? localDraft : serverDraft);
      }
    } else {
      setCreativeConflict(null);
    }
  }

  async function saveCreativeSettings(status: 'pending_review' | 'approved') {
    if (!project?.id) return;
    setStateSaving(true);
    try {
      const endpoint = `/api/projects/${encodeURIComponent(project.id)}/creative`;
      const response = await fetch(endpoint, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ values: creativeDraft, status, source: 'director-ui', expected_revision: projectState?.creative?.revision ?? 0 }) });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail?.message || data.detail || '创作设定保存失败');
      localStorage.setItem(creativeStorageKey, JSON.stringify(creativeDraft));
      if (activeProjectId.current !== project.id) return;
      setCreativeConflict(null);
      await refreshProjectState();
      if (activeProjectId.current !== project.id) return;
      setNotice(status === 'approved' ? `创作设定 revision ${data.revision} 已确认。` : `创作设定 revision ${data.revision} 已保存，等待确认。`);
    } catch (error) { if (activeProjectId.current === project.id) setNotice(error instanceof Error ? error.message : '创作设定保存失败。'); }
    finally { setStateSaving(false); }
  }

  async function saveCheckpoint(stageId: string, status: CheckpointRecord['status']) {
    if (!project?.id) return;
    const assetId = ['intent', 'character'].includes(stageId) ? '' : (currentPlanSegment?.id ?? '');
    try {
      const response = await fetch(`/api/projects/${encodeURIComponent(project.id)}/checkpoints`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ expected_revision: projectState?.checkpoints[assetId ? `${assetId}:${stageId}` : stageId]?.revision ?? 0, stage_id: stageId, status, asset_id: assetId, note: status === 'approved' ? '导演在当前工作区确认' : currentNote, source: 'director-ui' }) });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail?.message || data.detail || '阶段状态保存失败');
      await refreshProjectState(); setNotice(`${stageId} 已保存为 ${checkpointLabel(data as CheckpointRecord, true)}。`);
    } catch (error) { setNotice(error instanceof Error ? error.message : '阶段状态保存失败。'); }
  }

  async function saveCurrentSegment(): Promise<boolean> {
    if (!project || !currentPlanSegment) return false;
    const durationSeconds = Number(segmentEdit.duration_seconds);
    if (!Number.isFinite(durationSeconds) || durationSeconds <= 0) { setNotice('分镜时长必须是大于 0 的数字。'); return false; }
    setPipelineValidation(null);
    try {
      const response = await fetch(`/api/projects/${encodeURIComponent(project.id)}/plan/segments/${encodeURIComponent(currentPlanSegment.id)}`, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...segmentEdit, workflow: generationEdit.mode === 'auto' ? undefined : segmentEdit.workflow, generation: currentPlanSegment.generation || generationEdit.mode === 'auto' ? generationEdit : undefined, expected_revision: planRevision, prompt: currentPrompt, duration_seconds: durationSeconds, dependencies: segmentEdit.dependencies.split(/[，,]/).map(value => value.trim()).filter(Boolean) }),
      });
      const data = await response.json(); if (!response.ok) throw new Error(data.detail?.message || data.detail || '分镜保存失败');
      if (activeProjectId.current !== project.id) return false;
      if (data.project) { setProject(data.project as ProjectManifest); setPipeline(data.project.pipeline ?? []); }
      setPlanRevision(Number(data.plan.revision) || 0);
      setPlanSegments(data.plan.segments); setPlanAssembly(data.plan.assembly ?? null); updateDraft(currentPlanSegment.id, { prompt: undefined }); await refreshProjectState(); setNotice(`${assetLabel(currentPlanSegment.id)} 已保存。`);
      return true;
    } catch (error) { if (activeProjectId.current === project.id) setNotice(error instanceof Error ? error.message : '分镜保存失败。'); return false; }
  }

  async function saveAndRegenerate() {
    if (!project || !currentPlanSegment) return;
    if (activeTaskStates.includes(taskState)) { setNotice('当前分镜仍有任务在运行，请等待或先停止任务。'); return; }
    const pendingKey = `director-workbench:${project.id}:${current.id}:pending-submission:v1`;
    if (localStorage.getItem(pendingKey)) { setNotice('上一次提交结果尚未确认，请先核对回执，再重新生成。'); return; }
    if (!await saveCurrentSegment()) return;
    await startCurrentTask({
      prompt: currentPrompt,
      'first-frame': segmentEdit.first_frame,
      'last-frame': segmentEdit.last_frame,
      guide: segmentEdit.audio_guide,
      duration: Number(segmentEdit.duration_seconds),
    });
  }

  async function previewH3Duration() {
    if (!currentGenerationStage || !supportsH3Duration) return;
    const duration = Number(segmentEdit.duration_seconds);
    if (!await saveCurrentSegment()) return;
    await validatePipelineStage(currentGenerationStage, true, {
      prompt: currentPrompt, 'first-frame': segmentEdit.first_frame,
      duration, seed: Number(pipelineValues.seed ?? 42),
    });
  }

  function openGenerationSettings() {
    const picker = document.querySelector<HTMLDetailsElement>('.director-preset-compact');
    if (!picker) return;
    picker.open = true;
    picker.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }

  async function operateCurrentSegment(operation: 'copy' | 'move' | 'split' | 'merge' | 'merge_range', options: Record<string, unknown> = {}) {
    if (!project || !currentPlanSegment) return;
    if (operation === 'move' && options.before_id === currentPlanSegment.id) options = { before_id: '__end__' };
    try {
      const response = await fetch(`/api/projects/${encodeURIComponent(project.id)}/plan/segments/${encodeURIComponent(currentPlanSegment.id)}/operate`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ operation, ...options, expected_revision: planRevision }) });
      const data = await response.json(); if (!response.ok) throw new Error(data.detail?.message || data.detail || '分镜操作失败');
      if (activeProjectId.current !== project.id) return;
      if (data.project) { setProject(data.project as ProjectManifest); setPipeline(data.project.pipeline ?? []); }
      setPlanRevision(Number(data.plan.revision) || 0);
      setPlanSegments(data.segments); setPlanAssembly(data.plan.assembly ?? null); await refreshProjectState(); setNotice(`${currentPlanSegment.id} 已完成${operation === 'copy' ? '复制' : operation === 'move' ? '排序' : operation === 'split' ? '拆分' : '合并'}，下游阶段已标为待复核。`);
    } catch (error) { if (activeProjectId.current === project.id) setNotice(error instanceof Error ? error.message : '分镜操作失败。'); }
  }

  function exportPlan() {
    const blob = new Blob([JSON.stringify({
      schema_version: 2,
      project: project?.id ?? 'current-project',
      type: 'review-plan-not-comfy-workflow',
      exported_at: new Date().toISOString(),
      assets: library,
      creative_draft: creativeDraft,
      source_plan_segments: planSegments,
      edit_decisions: drafts,
      current_context: {
        asset_id: current.id,
        generated: hasGeneratedCheckpoint,
        approved: hasApprovalCheckpoint,
        task_id: taskId || currentPlanSegment?.comfyui_task_id || null,
        task_status: taskState || (current.ready ? 'succeeded' : 'not_submitted'),
        checkpoints: productionRail,
        checkpoint_summary: checkpointSummary,
      },
    }, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a'); link.href = url; link.download = `${project?.id ?? 'project'}-审阅计划.json`; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    setNotice('已导出审阅计划，包含原始信息和编辑草稿；这不是 ComfyUI 工作流。');
  }

  function exportStageContract(stage: PipelineStage) {
    const blob = new Blob([JSON.stringify({ schema_version: 2, project: project?.id ?? 'current-project', stage }, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a'); link.href = url; link.download = `${project?.id ?? 'project'}-${stage.order.toString().padStart(2, '0')}-${stage.id}-步骤契约.json`; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    setNotice(`已导出“${stage.title}”步骤契约；其中只记录输入、输出和执行引用。`);
  }

  async function refreshProjects(): Promise<Array<{ id: string; title: string; series?: string; selected?: boolean }>> {
    const response = await fetch('/api/projects');
    if (!response.ok) throw new Error('项目列表读取失败');
    const data = await response.json();
    const items = Array.isArray(data.projects) ? data.projects : [];
    setProjectList(items);
    return items;
  }

  async function afterDirectoryDeleted() {
    setDeleteDirectory(null);
    if (archive) { setArchive(null); setProject(null); localStorage.removeItem('director-workbench:last-project'); }
    setSeriesRefreshKey(value => value + 1);
    const items = await refreshProjects();
    if (project && !items.some(item => item.id === project.id)) {
      setProject(null);
      localStorage.removeItem('director-workbench:last-project');
    }
    setNotice(deleteDirectory?.projectId ? '旧作品已从工作台移出，D 盘文件仍保留。' : '目录已删除，左侧列表已更新。');
  }

  async function deleteCurrentShot() {
    if (!project || !currentPlanSegment || deletingShot) return;
    setDeletingShot(true);
    try {
      const response = await fetch(`/api/projects/${encodeURIComponent(project.id)}/plan/segments/${encodeURIComponent(currentPlanSegment.id)}`, { method: 'DELETE', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ expected_revision: planRevision, confirm: currentPlanSegment.id }) });
      const data = await response.json();
      if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : data.detail?.message || '删除分镜失败');
      setDeleteShotOpen(false); setSelected('');
      await refreshPlan();
      setNotice('分镜已从计划移除；可能被其他分镜使用的素材仍保留。');
    } catch (error) { setNotice(error instanceof Error ? error.message : '删除分镜失败'); }
    finally { setDeletingShot(false); }
  }

  async function refreshRegisteredMaterials(count: number) {
    const projectId = project?.id;
    try {
      const response = await fetch('/api/project');
      if (!response.ok) throw new Error('素材已登记，但项目刷新失败，请重试。');
      const data = await response.json() as ProjectManifest;
      if (data.id !== projectId) throw new Error('当前项目已改变，请重新选择项目。');
      setProject(previous => previous?.id === projectId ? data : previous);
      setNotice(`已登记 ${count} 个素材，可在分镜中选择。`);
      void refreshProjects();
    } catch (error) { setNotice(error instanceof Error ? error.message : '素材刷新失败。'); }
  }

  async function selectProject(projectId: string, reportFailure = false, expectedInteraction?: number) {
    try {
      const response = await fetch('/api/projects/select', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ project_id: projectId }) });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || '项目切换失败');
      if (reportFailure && data.id !== projectId) throw new Error('项目切换回执与目标不一致');
      if (reportFailure && expectedInteraction !== pageInteraction.current) throw new Error('用户已接管页面，未更换编辑器。');
      const nextAssets: Asset[] = Array.isArray(data.assets) ? data.assets.map((asset: Record<string, unknown>) => normalizeManifestAsset(asset)) : [];
      setProject(data as ProjectManifest); setProjectState(null); setCreativeConflict(null); setProjectMenuOpen(false); setPlanSegments([]); setPlanAssembly(null); setTaskId(''); setTaskState(''); setCurrentTask(null); setProjectTasks([]); setAssemblyTaskId(''); setAssemblyTaskState(''); setBatchId(''); setBatchTasks([]); setSelected(kind === 'video' ? '' : nextAssets.find(asset => asset.kind === kind)?.id ?? ''); setDrafts(loadDrafts(`director-workbench:${String(data.id)}:drafts:v1`)); setCreativeDraft(loadCreativeDraft(`director-workbench:${String(data.id)}:creative:v1`));
      setArchive(null);
      localStorage.setItem('director-workbench:last-project', data.id);
      setNotice(`已切换项目：${data.title ?? data.id}`);
      void refreshProjects();
      setSeriesRefreshKey(value => value + 1);
      return data as ProjectManifest;
    } catch (error) { if (reportFailure) throw error; setNotice(error instanceof Error ? error.message : '项目切换失败。'); }
  }

  async function presentAgentAction(action: AgentPageAction): Promise<AgentPageActionReceipt> {
    const startingInteraction = pageInteraction.current;
    const target = action.target, values = action.values;
    const savedFields: Partial<SegmentEdit> = currentPlanSegment ? {
      prompt: currentPlanSegment.prompt ?? '', location: currentPlanSegment.location ?? '', shot_size: currentPlanSegment.shot_size ?? '',
      camera: currentPlanSegment.camera ?? '', wardrobe: currentPlanSegment.wardrobe ?? '', performance: currentPlanSegment.performance ?? '',
      workflow: currentPlanSegment.workflow ?? '', first_frame: currentPlanSegment.keyframes?.first ?? '', last_frame: currentPlanSegment.keyframes?.last ?? '',
      audio_guide: currentPlanSegment.audio?.guide ?? '', delivery_master: currentPlanSegment.audio?.delivery_master ?? '', dependencies: (currentPlanSegment.dependencies ?? []).join(', '),
    } : {};
    const savedGeneration = currentPlanSegment?.generation ?? { mode: 'advanced', aspect_ratio: '16:9', sound_mode: 'performance_reference', seed: 42 };
    const dirty = Boolean(drafts[current.id]?.prompt !== undefined ||
      (currentPlanSegment && (Object.entries(savedFields).some(([key, value]) => segmentEdit[key as keyof SegmentEdit] !== value) ||
        Number(segmentEdit.duration_seconds) !== currentPlanSegment.duration_seconds ||
        (['mode', 'aspect_ratio', 'sound_mode', 'seed'] as const).some(key => generationEdit[key] !== savedGeneration[key]))) ||
      (segmentFormOpen && segmentDraft.prompt.trim() && !(ownedAgentDraft && agentDraft.current?.interaction === startingInteraction)));
    if (dirty) return { status: 'takeover', message: '编辑器有未保存输入；没有覆盖，请先保存或手动接管。' };
    setAgentStep('Agent正在定位当前操作…');
    try {
      const pid = target.project_id ?? project?.id;
      if (!pid) throw new Error('请先由 Agent 创建作品，再指定真实作品编号。');
      if (action.kind === 'show_task' && !target.task_id) throw new Error('定位任务需要真实任务编号。');
      if (!['show_draft', 'locate', 'navigate', 'open_candidate', 'play', 'pause', 'show_preflight', 'show_task'].includes(action.kind)) throw new Error('页面动作不受支持。');
      if (target.task_id && ['open_candidate', 'play', 'pause'].includes(action.kind)) {
        if (!target.asset_id) throw new Error('定位任务候选需要明确目标分镜编号。');
        const response = await fetch(`/api/projects/${encodeURIComponent(pid)}/segments/${encodeURIComponent(target.asset_id)}/versions?limit=100&offset=0`);
        if (!response.ok) throw new Error('目标候选版本不可读取，未操作播放器。');
        const versions = await response.json() as { current_version_task_id?: string; items: ShotVersion[] };
        const currentTaskId = versions.current_version_task_id ?? versions.items.find(version => version.is_current)?.task_id;
        if (currentTaskId !== target.task_id) return { status: 'not_presented', message: '指定任务未确认为当前候选；请在历史版本中查看，页面没有切换、恢复或播放其他候选。' };
      }
      const settled = () => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve())));
      let documentPlan: { segments: PlanSegment[]; revision: number; assembly?: PlanAssembly | null } | undefined;
      if (target.project_id) {
        const response = await fetch(`/api/projects/${encodeURIComponent(target.project_id)}/plan`);
        const document = await response.json(); documentPlan = document;
        if (!response.ok) throw new Error('目标作品不可读取，请核对本人作品。');
        if (target.revision !== undefined && Number(target.revision) !== (Number(document.revision) || 0)) {
          return { status: 'not_presented', message: '目标计划版本已变化；没有填入或执行页面动作。' };
        }
        if (startingInteraction !== pageInteraction.current) return { status: 'takeover', message: '用户已接管页面，停止后续定位。' };
        if (target.project_id !== project?.id) await selectProject(target.project_id, true, startingInteraction);
        else {
          const manifestResponse = await fetch(`/api/projects/${encodeURIComponent(pid)}`);
          if (!manifestResponse.ok) throw new Error('目标作品刷新失败。');
          const manifest = await manifestResponse.json() as ProjectManifest;
          if (manifest.id !== pid) throw new Error('目标作品回执不一致。');
          if (startingInteraction !== pageInteraction.current) return { status: 'takeover', message: '用户已接管页面，停止后续定位。' };
          setProject(manifest);
        }
        await settled();
        if (agentView.current.projectId !== pid) throw new Error('目标作品尚未定位，未执行页面动作。');
        if (startingInteraction !== pageInteraction.current) return { status: 'takeover', message: '用户已接管页面，停止后续定位。' };
        setPlanSegments(document.segments); setPlanRevision(Number(document.revision) || 0); setPlanAssembly(document.assembly ?? null);
      }
      if (startingInteraction !== pageInteraction.current) return { status: 'takeover', message: '用户已接管页面，停止后续定位。' };
      setWorkspaceMode('production'); setArchive(null);
      if (action.kind === 'navigate') {
        const page = values.page ?? 'shots';
        if (!['shots', 'script', 'creative', 'delivery'].includes(page)) throw new Error('目标页面不受支持。');
        setWorkspacePage(page as 'shots' | 'script' | 'creative' | 'delivery');
      } else if (action.kind === 'show_draft') {
        const raw = newSegmentDrafts[pid] ?? readNewSegmentDraft(pid);
        if (raw.prompt.trim() && !(agentDraft.current?.projectId === pid && agentDraft.current.interaction === startingInteraction && JSON.stringify(agentDraft.current.draft) === JSON.stringify(raw))) return { status: 'takeover', message: '目标作品有未保存草稿，没有覆盖。' };
        const draft = { ...raw,
          prompt: values.prompt ?? '', segment_id: target.asset_id ?? '', duration_seconds: String(values.duration_seconds ?? 5),
        };
        const generation: ShotGenerationSettings = { mode: 'auto', aspect_ratio: '16:9', sound_mode: 'performance_reference', seed: 42, ...(values.generation as Partial<ShotGenerationSettings> | undefined) };
        agentDraft.current = { projectId: pid, draft, generation, interaction: startingInteraction };
        setNewSegmentDrafts(previous => ({ ...previous, [pid]: draft }));
        setGenerationDraft(generation); setAdvancedOpen(false);
        setWorkspacePage('shots'); setSegmentFormOpen(true);
        setAgentStep('已在添加分镜编辑器填入草稿；尚未保存或生成。');
      } else {
        setSegmentFormOpen(false);
        const owned = agentDraft.current;
        if (owned?.projectId === pid && owned.interaction === startingInteraction && documentPlan?.segments.some(segment => segment.id === owned.draft.segment_id && segment.prompt === owned.draft.prompt && segment.duration_seconds === Number(owned.draft.duration_seconds))) {
          // Clear only this unchanged Agent draft after its actual save is read back from the plan.
          setNewSegmentDrafts(previous => JSON.stringify(previous[pid] ?? readNewSegmentDraft(pid)) === JSON.stringify(owned.draft) ? { ...previous, [pid]: emptySegmentDraft() } : previous);
          agentDraft.current = null;
        }
        if (target.asset_id && documentPlan && !documentPlan.segments.some(segment => segment.id === target.asset_id) && project?.assembly_asset_id !== target.asset_id) throw new Error('目标分镜不存在，未回退到其他素材。');
        if (target.asset_id) { mediaRef.current?.pause(); setKind('video'); setSelected(target.asset_id); }
        setWorkspacePage(action.kind === 'show_task' ? 'delivery' : 'shots');
        if (action.kind === 'show_task' && target.project_id && target.task_id) {
          const response = await fetch(`/api/projects/${encodeURIComponent(target.project_id)}/tasks/${encodeURIComponent(target.task_id)}`);
          const task = await response.json();
          if (!response.ok || task.id !== target.task_id || task.project_id !== pid || (target.asset_id && task.asset_id !== target.asset_id)) throw new Error('原任务不可读取或目标不一致，未伪造执行状态。');
          setCurrentTask(task); setTaskId(task.id); setTaskState(task.status);
          setProjectTasks(previous => [task, ...previous.filter(record => record.id !== task.id)]);
          setAgentStep(`任务 ${task.id} · ${taskStateLabel(task.status)}；来自原任务回执，没有再次提交。`);
        }
        if (action.kind === 'show_preflight') {
          const stateResponse = await fetch(`/api/projects/${encodeURIComponent(pid)}/state`);
          if (!stateResponse.ok) throw new Error('原准备度暂不可读取。');
          const state = await stateResponse.json() as ProjectState;
          if (state.project_id !== pid || agentView.current.projectId !== pid) throw new Error('准备度作品归属不一致。');
          setProjectState(state);
          setAgentStep('已定位原准备度和服务端阻塞原因；页面定位未额外提交生成。');
        }
      }
      await settled();
      if (startingInteraction !== pageInteraction.current) return { status: 'takeover', message: '用户正在操作，停止后续页面动作。' };
      if (action.kind === 'locate') { setShotEditTab('prompt'); await settled(); }
      if (action.kind === 'play' || action.kind === 'pause') {
        if (!target.asset_id) throw new Error('播放或暂停需要明确目标分镜编号。');
        const expectedSource = sharedMediaUrl(documentPlan?.segments.find(segment => segment.id === target.asset_id)?.video?.path) ?? agentView.current.source;
        if (!expectedSource) throw new Error('目标分镜尚无可播放的候选。');
        const expectedUrl = new URL(expectedSource, location.href).href;
        const matches = (media: HTMLMediaElement | null) => Boolean(media?.isConnected && agentView.current.projectId === pid && agentView.current.assetId === target.asset_id && media.getAttribute('aria-label') === `${target.asset_id} 视频预览` && media.src === expectedUrl && (!media.currentSrc || media.currentSrc === expectedUrl));
        const deadline = Date.now() + 5000;
        while (Date.now() < deadline && (!matches(mediaRef.current) || (mediaRef.current?.readyState ?? 0) < 3)) {
          if (startingInteraction !== pageInteraction.current) return { status: 'takeover', message: '用户已接管播放器，未继续播放。' };
          if (matches(mediaRef.current) && mediaRef.current?.error) throw new Error('目标候选加载失败，未播放其他素材。');
          await settled();
        }
        const media = mediaRef.current;
        if (!matches(media) || !media || media.readyState < 3) return { status: 'not_presented', message: '目标候选尚未载入，请在原播放器点击播放。' };
        if (action.kind === 'pause') media.pause();
        else await media.play();
        if (!matches(media) || mediaRef.current !== media || startingInteraction !== pageInteraction.current) return { status: 'takeover', message: '播放器目标已改变，停止后续操作。' };
        if (action.kind === 'play' && media.paused) return { status: 'not_presented', message: '浏览器未开始播放，请点击原播放按钮。' };
        setAgentStep(action.kind === 'play' ? '原播放器已开始播放。' : '原播放器已暂停。');
      }
      const editor = document.querySelector<HTMLElement>(action.kind === 'show_draft' ? '[aria-label="添加分镜"]' : '.director-edit, .director-readiness');
      editor?.scrollIntoView({ block: 'nearest' });
      if (action.kind === 'show_draft' && !editor) return { status: 'not_presented', message: '添加分镜编辑器未显示，未声称已填入。' };
      if (action.kind === 'show_draft' && (editor?.querySelector('textarea')?.value !== (values.prompt ?? '') || agentView.current.projectId !== pid)) return { status: 'not_presented', message: '目标编辑器原文尚未显示，未声称已填入。' };
      return { status: 'presented', message: action.kind === 'show_draft' ? '原编辑器已显示未保存草稿。' : '目标页面已呈现；不代表审核、采用或交付。' };
    } catch (error) {
      if (startingInteraction !== pageInteraction.current) return { status: 'takeover', message: '用户已接管页面，没有继续呈现。' };
      return { status: 'not_presented', message: error instanceof Error ? error.message : '页面未呈现，请使用原控件继续。' };
    }
  }

  async function presentAssistantEvent(event: AssistantPresentationEvent): Promise<AgentPageActionReceipt> {
    const kinds: Record<string, string> = { saved: 'locate', preflight: 'show_preflight', submitted: 'show_task', candidate: 'open_candidate' };
    return presentAgentAction({ id: `assistant-${event.seq}`, seq: event.seq, status: 'pending',
      kind: kinds[event.kind] ?? event.kind,
      target: { project_id: event.project_id, asset_id: event.asset_id, task_id: event.task_id },
      values: { prompt: event.text, duration_seconds: event.duration_seconds, generation: event.generation, page: 'shots' },
    });
  }

  async function openAssistantResult(result: { project_id?: string; asset_id?: string; task_id?: string }) {
    let kind = result.asset_id ? 'locate' : 'navigate';
    if (result.task_id && result.project_id) {
      try {
        const response = await fetch(`/api/projects/${encodeURIComponent(result.project_id)}/tasks/${encodeURIComponent(result.task_id)}`);
        if (!response.ok) throw new Error('原任务暂不可读取，请重新登录或查询。');
        const task = await response.json();
        kind = task.status === 'succeeded' ? 'open_candidate' : 'show_task';
      } catch (error) { setNotice(error instanceof Error ? error.message : '原任务读取失败。'); return; }
    }
    const receipt = await presentAgentAction({ id: 'assistant-result', seq: 0, status: 'pending', kind,
      target: result, values: { page: 'shots' } });
    if (receipt.status !== 'presented') setNotice(receipt.message ?? '页面尚未呈现，请保留原输入后继续。');
  }

  async function importWorkspace() {
    if (!importPath.trim()) { setNotice('请输入 D:\\Comfy-Desktop 内的工作目录路径。'); return; }
    try {
      const response = await fetch('/api/projects/import', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ path: importPath.trim() }) });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || '工作目录导入失败');
      setProject(data.project as ProjectManifest); setCreativeConflict(null); setImportReport(data.report ?? null); setImportPath(''); setNotice(`已导入工作目录，发现 ${data.report?.counts?.video ?? 0} 个视频、${data.report?.counts?.audio ?? 0} 个音频和 ${data.report?.counts?.image ?? 0} 张图片。`); void refreshProjects();
    } catch (error) { setNotice(error instanceof Error ? error.message : '工作目录导入失败。'); }
  }

  async function createProject() {
    if (creatingProject) return;
    if (!newSeries.trim() || !newTitle.trim()) { setNotice('请先填写系列名和作品名。'); return; }
    setCreatingProject(true);
    try {
      let initialCreative = { ...blankCreativeDraft(), creation_mode: newCreationMode };
      if (newCreationMode === 'reference-series' && newReferenceProject) {
        const sourceResponse = await fetch(`/api/projects/${encodeURIComponent(newReferenceProject)}/creative-template`);
        if (!sourceResponse.ok) throw new Error('读取参考作品失败，请重新选择来源。');
        const template = await sourceResponse.json();
        initialCreative = normalizeCreativeDraft(template.values);
      }
      const response = await fetch('/api/projects/create', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ series: newSeries.trim(), title: newTitle.trim(), project_id: newProjectId.trim() || undefined }) });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || '创建项目失败');
      const saved = await fetch(`/api/projects/${encodeURIComponent(data.project.id)}/creative`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ values: initialCreative, status: 'pending_review', source: 'director-ui', expected_revision: 0 }) }).catch(() => null);
      setProject(data.project as ProjectManifest); setProjectState(null); setCreativeConflict(null); setProjectMenuOpen(false); setCreateOpen(false); setPlanSegments([]); setPlanAssembly(null); setTaskId(''); setTaskState(''); setCurrentTask(null); setProjectTasks([]); setAssemblyTaskId(''); setAssemblyTaskState(''); setBatchId(''); setBatchTasks([]); setSelected(''); setDrafts({}); setCreativeDraft(initialCreative); setNewSeries(''); setNewTitle(''); setNewProjectId('');
      localStorage.setItem('director-workbench:last-project', data.project.id);
      setWorkspaceMode(newCreationMode === 'reference-video' ? 'reverse' : 'production');
      setCreativeOpen(newCreationMode !== 'reference-video');
      setSegmentFormOpen(false); setStageFormOpen(false); setPipelineStageId('');
      setNewReferenceProject('');
      setWorkspacePage(newCreationMode === 'reference-video' ? 'shots' : newCreationMode === 'script' ? 'script' : 'creative');
      setNotice(saved?.ok ? `已创建“${data.project.title}”，从${creationRoutes.find(route => route.id === newCreationMode)?.title ?? '创作设定'}开始。` : '作品已创建，初始设定保存未获确认；草稿已保留，请查看服务器版本后继续保存。');
      void refreshProjects();
      setSeriesRefreshKey(value => value + 1);
    } catch (error) { setNotice(error instanceof Error ? error.message : '创建项目失败。'); }
    finally { setCreatingProject(false); }
  }

  async function uploadSelectedFiles() {
    if (!project || !importFiles.length) { setNotice('请先选择一个工作目录或文件。'); return; }
    const form = new FormData();
    importFiles.forEach(file => form.append('files', file, (file as File & { webkitRelativePath?: string }).webkitRelativePath || file.name));
    try {
      const response = await fetch(`/api/projects/${encodeURIComponent(project.id)}/upload`, { method: 'POST', body: form });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || '资源导入失败');
      setProject(data.project as ProjectManifest); setImportReport(data.report ?? null); setImportFiles([]); setNotice(`已登记 ${data.uploaded?.length ?? 0} 个资源，工作流和媒体仍由 ComfyUI 负责执行。`); void refreshProjects();
    } catch (error) { setNotice(error instanceof Error ? error.message : '资源导入失败。'); }
  }

  async function saveMaterialTitle() {
    if (!project || !editableMaterial || savingMaterialTitle) return;
    const title = materialTitle.trim();
    if (!title) { setNotice('请填写素材名称。'); return; }
    const projectId = project.id;
    setSavingMaterialTitle(true);
    try {
      const response = await fetch(`/api/projects/${encodeURIComponent(projectId)}/assets/${encodeURIComponent(current.id)}/label`, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title, expected_title: String(editableMaterial.title ?? '') }),
      });
      const data = await response.json();
      if (activeProjectId.current !== projectId) return;
      if (response.status === 409) {
        const fresh = await fetch(`/api/projects/${encodeURIComponent(projectId)}`);
        if (fresh.ok) setProject(await fresh.json() as ProjectManifest);
        setNotice('素材名称已被修改，请核对当前名称后再保存。');
        return;
      }
      if (!response.ok) throw new Error(data.detail || '素材命名失败');
      setProject(data.project as ProjectManifest);
      setNotice('素材名称已保存。');
    } catch (error) { if (activeProjectId.current === projectId) setNotice(error instanceof Error ? error.message : '素材命名失败。'); }
    finally { setSavingMaterialTitle(false); }
  }

  async function createPipelineStage() {
    if (!project || !stageTitle.trim()) { setNotice('请先填写步骤名称。'); return; }
    try {
      const response = await fetch(`/api/projects/${encodeURIComponent(project.id)}/pipeline/stages`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ title: stageTitle.trim(), purpose: stagePurpose.trim(), backend: stageBackend, inputs: stageInputs.split(/[，,\n]/).map(value => value.trim()).filter(Boolean), outputs: stageOutputs.split(/[，,\n]/).map(value => value.trim()).filter(Boolean), workflow: stageWorkflow.trim() }) });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || '制作步骤登记失败');
      setProject(data.project as ProjectManifest); setPipeline(Array.isArray(data.project?.pipeline) ? data.project.pipeline : []); setPipelineStageId(String(data.stage?.id ?? '')); setStageFormOpen(false); setStageTitle(''); setStagePurpose(''); setStageInputs(''); setStageOutputs(''); setStageWorkflow(''); setNotice(`已登记制作步骤“${data.stage?.title ?? stageTitle}”。`);
    } catch (error) { setNotice(error instanceof Error ? error.message : '制作步骤登记失败。'); }
  }

  async function unlinkScriptScene() {
    if (!project || !currentPlanSegment || scriptLinkSaving) return;
    const projectId = project.id;
    const segmentId = currentPlanSegment.id;
    setScriptLinkSaving(true);
    try {
      const scriptResponse = await fetch(`/api/projects/${encodeURIComponent(projectId)}/script`);
      if (!scriptResponse.ok) throw new Error('读取剧本失败，请重试。');
      const script = await scriptResponse.json();
      if (activeProjectId.current !== projectId) return;
      const response = await fetch(`/api/projects/${encodeURIComponent(projectId)}/plan/segments/${encodeURIComponent(segmentId)}`, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ script_scene_id: '', expected_script_revision: script.revision, expected_revision: planRevision }),
      });
      const data = await response.json();
      if (activeProjectId.current !== projectId) return;
      if (!response.ok) throw new Error(response.status === 409 ? '剧本或分镜已变化，请刷新后重试。' : data.detail?.message || data.detail || '解除关联失败。');
      setPlanSegments(data.plan.segments); setPlanRevision(data.plan.revision); setPlanAssembly(data.plan.assembly ?? null);
      setNotice(`已解除 ${segmentId} 的场次关联。`);
    } catch (error) {
      if (activeProjectId.current === projectId) setNotice(error instanceof Error ? error.message : '解除关联失败。');
    } finally { setScriptLinkSaving(false); }
  }

  async function createPlanSegment() {
    if (creatingSegment.current) return;
    if (!project) { setNotice('请先创建或选择一个项目。'); return; }
    const duration = Number(segmentDraft.duration_seconds);
    if (!Number.isFinite(duration) || duration <= 0) { setNotice('片段时长必须是大于 0 的数字。'); return; }
    creatingSegment.current = true;
    try {
      const response = await fetch(`/api/projects/${encodeURIComponent(project.id)}/plan/segments`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ...segmentDraft, workflow: autoCreation && !advancedOpen ? undefined : segmentDraft.workflow, generation: autoCreation && !advancedOpen ? generationDraft : undefined, script_revision: undefined, script_scene_id: segmentDraft.script_scene_id || undefined, expected_script_revision: segmentDraft.script_scene_id ? Number(segmentDraft.script_revision) : undefined, expected_revision: planRevision, segment_id: segmentDraft.segment_id || undefined, duration_seconds: duration }) });
      const data = await response.json();
      if (activeProjectId.current !== project.id) return;
      if (response.status === 409) {
        await refreshPlan();
        if (activeProjectId.current !== project.id) return;
        setNotice(segmentDraft.script_scene_id ? '剧本或分镜计划已变化，草稿已保留。请回到剧本与场次核对，再从对应场次打开新增分镜。' : '分镜计划已变化，已刷新列表并保留草稿；请核对是否已有该镜头，再决定是否保存。');
        return;
      }
      if (!response.ok) throw new Error(data.detail?.message || data.detail || '分段登记失败');
      if (Array.isArray(data.plan?.segments)) setPlanSegments(data.plan.segments);
      if (data.project) { setProject(data.project as ProjectManifest); setPipeline(data.project.pipeline ?? []); }
      setPlanAssembly(data.plan?.assembly ?? null);
      setPlanRevision(Number(data.plan?.revision) || 0);
      if (data.segment?.id) { setKind('video'); setSelected(data.segment.id); }
      setSegmentDraft({ script_scene_id: '', script_revision: '', audio_guide: '', delivery_master: '', segment_id: '', duration_seconds: '5', location: '', shot_size: '中景', camera: '固定机位', wardrobe: '沿用角色设定', performance: '', prompt: '', workflow: '', first_frame: '', last_frame: '' }); setSegmentFormOpen(false); setNotice(`已登记分段 ${data.segment?.id ?? ''}，可以继续添加下一个镜头。`);
    } catch (error) { if (activeProjectId.current === project.id) setNotice(error instanceof Error ? error.message : '分段登记失败。'); }
    finally { creatingSegment.current = false; }
  }

  async function registerPipelineAsset(inputId: string, file: File) {
    if (!project) { setNotice('请先创建或选择一个项目。'); return; }
    const form = new FormData(); form.append('files', file, `assets/${file.name}`);
    try {
      const response = await fetch(`/api/projects/${encodeURIComponent(project.id)}/upload`, { method: 'POST', body: form });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || '素材登记失败');
      const nextProject = data.project as ProjectManifest;
      const uploadedPath = data.uploaded?.[0];
      const registered = nextProject.assets?.find(asset => asset.image_path === uploadedPath || Object.values((asset.sources ?? {}) as Record<string, unknown>).includes(uploadedPath));
      setProject(nextProject); setPipelineValues(previous => ({ ...previous, [inputId]: registered?.id ? String(registered.id) : file.name })); setNotice(`已登记素材：${file.name}`); void refreshProjects();
    } catch (error) { setNotice(error instanceof Error ? error.message : '素材登记失败。'); }
  }

  async function exportProjectBundle() {
    if (!project) return;
    try {
      const response = await fetch(`/api/projects/${encodeURIComponent(project.id)}/export`);
      if (!response.ok) throw new Error('项目导出失败');
      const data = await response.json();
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob); const link = document.createElement('a'); link.href = url; link.download = `${project.id}-director-project.json`; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
      setNotice('已导出项目清单、分段计划、工作流快照和任务回执；媒体文件仍保留在 ComfyUI 工作区。');
    } catch (error) { setNotice(error instanceof Error ? error.message : '项目导出失败。'); }
  }

  function updatePipelineValue(id: string, value: string | number | boolean | string[]) {
    setDraftReadiness(null);
    setPipelineValues(previous => ({ ...previous, [id]: value }));
    if (id === 'prompt' && typeof value === 'string') updateDraft(current.id, { prompt: value });
    setPipelineValidation(null);
  }

  async function validatePipelineStage(stage: PipelineStage, announce = true, overrides: Record<string, string | number | boolean | string[]> = {}): Promise<PipelineValidation | null> {
    if (!project?.id) return null;
    setPipelineChecking(true);
    setPipelineValidation(null);
    setDraftReadiness(null);
    setPipelineError('');
    try {
      const automatic = generationEdit.mode === 'auto';
      const allowed = new Set(automatic ? ['prompt', 'duration', 'seed'] : (stage.inputSpecs ?? []).map(input => input.id));
      const values = Object.fromEntries(Object.entries({ ...pipelineValues, ...overrides }).filter(([id]) => allowed.has(id)));
      const response = await fetch(`/api/projects/${encodeURIComponent(project.id)}/pipeline/${encodeURIComponent(automatic ? 'auto' : stage.id)}/validate`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ asset_id: current.id, values }),
      });
      const data = await response.json();
      if (data.readiness || data.detail?.readiness) setDraftReadiness(data.readiness ?? data.detail.readiness);
      if (!response.ok) {
        const reasons = (data.detail?.blockers ?? data.detail?.readiness?.blockers ?? []).map(blockerMessage);
        const message = data.detail?.message || (typeof data.detail === 'string' ? data.detail : '参数校验失败，请查看准备度缺口。');
        throw new Error([message, ...reasons].filter((value, index, items) => items.indexOf(value) === index).join(' · '));
      }
      setPipelineValidation(data as PipelineValidation);
      setPipelineValues(previous => ({ ...previous, ...(data.values as Record<string, string | number | boolean | string[]>) }));
      if (announce) setNotice(`“${stage.title}”参数已通过预检，${data.binding_count ?? 0} 项输入会写入冻结 API 图。`);
      return data as PipelineValidation;
    } catch (error) {
      const message = error instanceof Error ? error.message : '参数校验失败。';
      setPipelineError(message);
      setNotice(message);
      return null;
    } finally {
      setPipelineChecking(false);
    }
  }

  function assetKindForInput(kind: string): Kind | undefined {
    if (kind === '音频') return 'audio';
    if (kind === '图片') return 'image';
    if (kind === '视频') return 'video';
    return undefined;
  }

  function pipelineAssetOptions(input: PipelineInput): Array<{ value: string; label: string }> {
    const contextual: Array<{ value: string; label: string }> = [];
    if (input.id === 'first-frame' && currentPlanSegment?.keyframes?.first) contextual.push({ value: currentPlanSegment.keyframes.first, label: `${current.id} · 当前首帧` });
    if (input.id === 'last-frame' && currentPlanSegment?.keyframes?.last) contextual.push({ value: currentPlanSegment.keyframes.last, label: `${current.id} · 当前尾帧` });
    if (input.id === 'guide' && currentPlanSegment?.audio?.guide) contextual.push({ value: currentPlanSegment.audio.guide, label: `${current.id} · 当前表演引导` });
    const kind = assetKindForInput(input.kind);
    const registered = baseAssets.filter(asset => !kind || asset.kind === kind).map(asset => ({ value: asset.id, label: `${asset.id} · ${asset.title}` }));
    const currentValue = pipelineValues[input.id];
    if (typeof currentValue === 'string' && currentValue && !contextual.some(item => item.value === currentValue) && !registered.some(item => item.value === currentValue)) {
      contextual.unshift({ value: currentValue, label: `${current.id} · 当前执行文件` });
    }
    return [...contextual, ...registered].filter((item, index, items) => items.findIndex(candidate => candidate.value === item.value) === index);
  }


  async function startCurrentTask(overrides: Record<string, string | number | boolean | string[]> = {}) {
    if (!project?.id || submittingShot.current) return;
    const assetId = current.id || project.default_task_asset_id || '';
    const storageKey = `director-workbench:${project.id}:${assetId}:pending-submission:v1`;
    submittingShot.current = true;
    let uncertain = false;
    try {
      const pending = localStorage.getItem(storageKey);
      let body = pending;
      if (!body) {
        if (!currentGenerationStage) { setNotice('当前片段尚未登记可执行的制作步骤。'); return; }
        const validation = await validatePipelineStage(currentGenerationStage, false, { prompt: currentPrompt || '', ...overrides });
        if (!validation) return;
        const contractResponse = await fetch('/openapi.json');
        if (!contractResponse.ok) throw new Error('无法核对后端生成契约，请稍后重试。');
        const contract = await contractResponse.json();
        if (!contract.components?.schemas?.StartRequest?.properties?.idempotency_key) throw new Error('请更新工作台后端以启用生成请求去重。');
        body = JSON.stringify({ idempotency_key: crypto.randomUUID(), asset_id: assetId, prompt: currentPrompt || undefined, expected_revision: validation.revision, pipeline_stage_id: generationEdit.mode === 'auto' ? 'auto' : currentGenerationStage.id, pipeline_values: validation.values });
        localStorage.setItem(storageKey, body);
      }
      uncertain = true;
      setTaskState('preparing');
      setNotice(pending ? '正在核对上次提交回执，沿用原请求和参数。' : `${assetId} 已通过预检，正在冻结输入并登记排队任务。`);
      const response = await fetch(`/api/projects/${encodeURIComponent(project.id)}/tasks`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body });
      const data = await response.json();
      if (!response.ok) {
        if (response.status >= 400 && response.status < 500 && data.detail?.code !== 'idempotency_key_conflict') {
          localStorage.removeItem(storageKey); uncertain = false;
        }
        throw new Error(data.detail?.message || data.detail || '提交失败');
      }
      if (!data.id) throw new Error('提交回执缺少任务 ID');
      localStorage.removeItem(storageKey); uncertain = false;
      setCurrentTask(data as TaskRecord); setProjectTasks(previous => [data as TaskRecord, ...previous.filter(task => task.id !== data.id)]); setTaskState(normalizeLiveTaskState(data.status ?? '')); setTaskId(data.id);
      setNotice(pending ? '已核对上次提交回执，没有创建重复任务。' : '已收到生成任务回执。');
    } catch (error) {
      setTaskState('');
      setNotice(uncertain ? '提交结果尚未确认。原请求已保留；再次点击生成会核对同一请求，不会使用新参数重复提交。' : error instanceof Error ? error.message : '提交准备失败。');
    } finally { submittingShot.current = false; }
  }

  async function checkPendingSubmission() {
    if (!project?.id || !current.id) return;
    const key = `director-workbench:${project.id}:${current.id}:pending-submission:v1`;
    try {
      const saved = localStorage.getItem(key);
      if (!saved) { setNotice('当前片段没有待核对的本机提交请求。'); return; }
      const pending = JSON.parse(saved);
      const response = await fetch(`/api/projects/${encodeURIComponent(project.id)}/submission-receipt?key=${encodeURIComponent(pending.idempotency_key)}`);
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail?.message || data.detail || '暂时无法核对提交回执');
      if (data.project_id !== project.id || data.asset_id !== current.id) throw new Error('任务回执与当前作品或片段不匹配');
      setCurrentTask(data as TaskRecord); setTaskId(data.id); setTaskState(normalizeLiveTaskState(data.status ?? ''));
      setProjectTasks(previous => [data as TaskRecord, ...previous.filter(task => task.id !== data.id)]);
      localStorage.removeItem(key);
      setNotice('已找回提交回执；本次核对没有提交生成。');
    } catch (error) { setNotice(error instanceof Error ? error.message : '核对失败，原请求仍保留。'); }
  }

  async function stopCurrentTask() {
    if (!taskId || !project?.id) return;
    try { const response = await fetch(`/api/projects/${encodeURIComponent(project!.id)}/tasks/${encodeURIComponent(taskId)}/stop`, { method: 'POST' }); const data = await response.json(); if (!response.ok) throw new Error(data.detail || '停止失败'); setTaskState(data.status); setNotice('已请求停止当前 ComfyUI 任务，等待执行确认。'); }
    catch (error) { setNotice(error instanceof Error ? error.message : '停止请求失败。'); }
  }

  async function resumeCurrentTask() {
    if (!taskId || !project?.id) return;
    const reconcileOnly = taskState === 'needs_reconcile';
    if (!taskId || !project?.id) return;
     try { const response = await fetch(`/api/projects/${encodeURIComponent(project!.id)}/tasks/${encodeURIComponent(taskId)}/${reconcileOnly ? 'reconcile' : 'resume'}`, { method: 'POST' }); const data = await response.json(); if (!response.ok) throw new Error(data.detail || '恢复失败'); setCurrentTask(data as TaskRecord); setProjectTasks(previous => [data as TaskRecord, ...previous.filter(task => task.id !== data.id)]); setTaskId(data.id ?? ''); setTaskState(normalizeLiveTaskState(data.status ?? '')); if (data.status === 'succeeded') { await refreshPlan(); setNotice('已找回原任务的生成结果，没有重复提交。'); } else if (reconcileOnly) { setNotice('已核对原任务未成功，没有提交新任务；检查原因后可明确重试。'); } else { setNotice('已按原任务冻结快照重新提交 ComfyUI。'); } }
    catch (error) { setNotice(error instanceof Error ? error.message : '恢复请求失败。'); }
  }

  async function resolveMissingExecution(id: string) {
    const projectId = project?.id;
    if (!projectId || !id) return;
    const note = window.prompt('结束核对前，请确认原执行进程已结束，并检查输出目录没有遗漏的生成结果。\n填写核对说明（必填）；原任务与历史记录会保留，不会自动重试。');
    if (note === null) return;
    if (!note.trim()) { setNotice('请填写原执行已结束及输出检查的核对说明。'); return; }
    if (!window.confirm('确认原执行已结束、输出已检查？\n将把原任务标记为失败并保留历史。此操作不会生成新任务；需要重做时请另行点击生成。')) return;
    try {
      const response = await fetch(`/api/projects/${encodeURIComponent(projectId)}/tasks/${encodeURIComponent(id)}/resolve-missing`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ confirm_execution_ended: true, note: note.trim() }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail?.message || data.detail || '结束核对失败，请刷新后核查原任务。');
      if (activeProjectId.current !== projectId) return;
      setProjectTasks(previous => [data as TaskRecord, ...previous.filter(task => task.id !== id)]);
      if (id === taskId) { setCurrentTask(data as TaskRecord); setTaskState(data.status); }
      if (id === assemblyTaskId) setAssemblyTaskState(data.status);
      setNotice('原任务已标记为失败，历史与提交凭据保留；尚未重试。需要生成时请另行发起。');
    } catch (error) {
      if (activeProjectId.current === projectId) setNotice(error instanceof Error ? error.message : '结束核对失败，原任务仍保留。');
    }
  }

  async function startAssembly(checkOnly = false) {
    if (!project?.id || submittingAssembly.current) return;
    const projectId = project.id;
    const storageKey = `director-workbench:${projectId}:assembly-submission:v1`;
    submittingAssembly.current = true; setAssemblySubmitting(true);
    let uncertain = false;
    try {
      let body = localStorage.getItem(storageKey);
      if (!body) {
        if (checkOnly) { setNotice('当前作品没有待核对的装配请求。'); return; }
        if (!assemblyReady) { setNotice(savedReadiness?.assembly.blockers.map(blockerMessage).join(' · ') || '请先完成服务器装配预检要求。'); return; }
        const contractResponse = await fetch('/openapi.json');
        if (!contractResponse.ok) throw new Error('无法核对装配提交契约，请稍后重试。');
        const contract = await contractResponse.json();
        const requestSchema = contract.paths?.['/api/projects/{project_id}/assembly']?.post?.requestBody?.content?.['application/json']?.schema;
        const schemas = requestSchema?.anyOf || [requestSchema];
        const supportsReceipt = schemas.some((schema: { $ref?: string } | undefined) => {
          const properties = contract.components?.schemas?.[schema?.$ref?.split('/').pop() || '']?.properties;
          return properties?.idempotency_key && properties?.expected_revision;
        });
        if (!supportsReceipt) throw new Error('请更新工作台后端以启用装配提交去重。');
        if (activeProjectId.current !== projectId) return;
        body = JSON.stringify({ idempotency_key: crypto.randomUUID(), expected_revision: planRevision });
        localStorage.setItem(storageKey, body);
        setPendingAssemblyProject(projectId);
      }
      const pending = JSON.parse(body);
      if (typeof pending.idempotency_key !== 'string' || !Number.isInteger(pending.expected_revision)) throw new Error('本机装配凭据无效，请保留记录并检查。');
      uncertain = true;
      const base = `/api/projects/${encodeURIComponent(projectId)}`;
      const response = checkOnly
        ? await fetch(`${base}/submission-receipt?key=${encodeURIComponent(pending.idempotency_key)}`)
        : await fetch(`${base}/assembly`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body });
      const data = await response.json();
      if (!response.ok) {
        if (!checkOnly && (response.status === 422 || data.detail?.code === 'plan_revision_conflict')) {
          localStorage.removeItem(storageKey); uncertain = false;
          if (activeProjectId.current === projectId) setPendingAssemblyProject('');
          if (data.detail?.code === 'plan_revision_conflict' && activeProjectId.current === projectId) void refreshPlan();
        }
        throw new Error(data.detail?.message || (typeof data.detail === 'string' ? data.detail : '装配请求未获确认'));
      }
      if (!data.id || data.project_id !== projectId || data.asset_id !== project.assembly_asset_id) throw new Error('装配回执与当前请求不匹配');
      localStorage.removeItem(storageKey); uncertain = false;
      if (activeProjectId.current !== projectId) return;
      setPendingAssemblyProject(''); setAssemblyTaskState(data.status); setAssemblyTaskId(data.id);
      setProjectTasks(previous => [data as TaskRecord, ...previous.filter(task => task.id !== data.id)]);
      setNotice(checkOnly ? '已找回原装配任务，没有提交新任务。' : '已收到装配任务回执。');
      if (data.status === 'succeeded') void refreshPlan();
    } catch (error) {
      if (activeProjectId.current === projectId) setNotice(uncertain ? '装配结果尚未确认，原请求已保留；请核对回执或沿用原凭据重试。' : error instanceof Error ? error.message : '装配提交准备失败。');
    } finally { submittingAssembly.current = false; setAssemblySubmitting(false); }
  }

  async function stopAssembly() {
    if (!assemblyTaskId || !project?.id) return;
    try {
      const response = await fetch(`/api/projects/${encodeURIComponent(project.id)}/tasks/${assemblyTaskId}/stop`, { method: 'POST' });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || '停止装配失败');
      setAssemblyTaskState(data.status); setNotice('已请求停止完整版装配，等待 ComfyUI 执行确认。');
    } catch (error) { setNotice(error instanceof Error ? error.message : '停止装配请求失败。'); }
  }

  async function resumeAssembly() {
    if (!assemblyTaskId || !project?.id) return;
    const reconcileOnly = assemblyTaskState === 'needs_reconcile';
    try {
      const response = await fetch(`/api/projects/${encodeURIComponent(project.id)}/tasks/${assemblyTaskId}/${reconcileOnly ? 'reconcile' : 'resume'}`, { method: 'POST' });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || '恢复装配失败');
      setAssemblyTaskState(data.status); setAssemblyTaskId(data.id ?? ''); if (data.status === 'succeeded') { await refreshPlan(); setNotice('已找回原装配结果，没有重新提交。'); } else { setNotice(reconcileOnly ? '已核对原装配未成功，没有提交新任务。' : '已从中断状态重新提交完整版装配。'); }
    } catch (error) { setNotice(error instanceof Error ? error.message : '恢复装配请求失败。'); }
  }

  async function startBatch() {
    if (!project?.id) return;
    const assetIds = planSegments.filter(segment => segment.workflow || project?.production_preset?.workflow).map(segment => segment.id);
    if (!assetIds.length) { setNotice('当前计划还没有可提交的分段工作流。'); return; }
    try {
      const generationStage = selectVideoGenerationStage(pipeline, project.production_preset?.workflow);
      if (!generationStage) { setNotice('当前项目没有可批量执行的视频制作步骤。'); return; }
      const response = await fetch(`/api/projects/${encodeURIComponent(project.id)}/batches`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ asset_ids: assetIds, pipeline_stage_id: generationStage.id, pipeline_values_by_asset: {} }) });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || '批次提交失败');
      setBatchId(data.batch_id ?? '');
      setBatchTasks((data.task_ids ?? []).map((id: string, index: number) => ({ id, asset_id: assetIds[index], status: 'batch_waiting', sequence: index + 1, batch_id: data.batch_id })));
      setNotice(`已提交 ${assetIds.length} 个片段；每段按自己的计划和源工作流冻结参数，当前片段修改不会隐式覆盖其他片段。`);
    } catch (error) { setNotice(error instanceof Error ? error.message : '全片段批次提交失败。'); }
  }

  async function stopBatch() {
    if (!batchId || !project?.id) return;
    try {
      const response = await fetch(`/api/projects/${encodeURIComponent(project!.id)}/batches/${encodeURIComponent(batchId)}/stop`, { method: 'POST' });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || '停止批次失败');
      const currentBatchTasks = (data.tasks ?? []).filter((task: BatchTask) => task.batch_id === batchId);
      if (currentBatchTasks.length) setBatchTasks(currentBatchTasks);
      setNotice('已请求停止批次；当前片段结束后会保留已完成结果。');
    } catch (error) { setNotice(error instanceof Error ? error.message : '停止批次请求失败。'); }
  }

  async function resumeBatch() {
    if (!batchId || !project?.id) return;
    try {
      const response = await fetch(`/api/projects/${encodeURIComponent(project!.id)}/batches/${encodeURIComponent(batchId)}/resume`, { method: 'POST' });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || '恢复批次失败');
      setBatchId(data.batch_id ?? '');
      setBatchTasks((data.task_ids ?? []).map((id: string, index: number) => ({ id, asset_id: '待恢复', status: 'batch_waiting', sequence: index + 1, batch_id: data.batch_id })));
      setNotice('批次已恢复，将从第一个未完成片段继续。');
    } catch (error) { setNotice(error instanceof Error ? error.message : '恢复批次请求失败。'); }
  }

  const materialOptions = projectMaterialOptions(project, planSegments);

  const mediaProps = {
    controls: true, preload: 'metadata',
    onPlay: () => { setPlaying(true); advancing.current = false; },
    onPause: () => setPlaying(false),
    onEnded: finish,
    onError: (event: { currentTarget: HTMLMediaElement }) => { if (mediaRef.current !== event.currentTarget) return; setMediaLoad({ key: mediaKey, status: 'error' }); setNotice('素材加载失败，请检查本机作品文件。'); },
    onTimeUpdate: () => {
      const media = mediaRef.current;
      if (!media) return;
      setPosition(Math.max(0, Math.min(duration, media.currentTime - start)));
      if (kind === 'audio' && !media.paused && media.currentTime >= (current.end ?? Infinity) - 0.04) finish();
    },
  };

  function workflowEntriesForAsset(asset: Asset): WorkflowCatalogEntry[] {
    const entries = (project?.workflow_catalog ?? []).filter(item => item.asset_id === asset.id);
    const inlineReference = asset.workflow?.split(' · ')[0]?.trim();
    if (inlineReference?.toLowerCase().endsWith('.json') && !entries.some(item => item.path === inlineReference)) {
      entries.unshift({ id: `${asset.id}-inline`, asset_id: asset.id, path: inlineReference, role: 'api-exact', comfyui_import: 'api' });
    }
    return entries;
  }

  const visibleReviewConflict = reviewConflict?.projectId === project?.id && reviewConflict?.assetId === current.id ? reviewConflict : null;
  const conflictingReview = visibleReviewConflict?.state.reviews[current.id]?.[visibleReviewConflict.stage];
  function resolveReviewConflict(useServerNote: boolean) {
    if (!visibleReviewConflict || !conflictingReview) return;
    setProjectState(visibleReviewConflict.state);
    if (useServerNote) updateDraft(current.id, { note: conflictingReview.note ?? '' });
    setReviewConflict(null);
    setNotice(useServerNote ? '已载入服务器审核意见；尚未提交任何更改。' : '已保留本地意见；请检查后保存或采用候选。');
  }
  function openGuideAction(action: string, route?: 'idea' | 'reference') {
    setArchive(null); setWorkspaceMode('production');
    if (action === 'project' || action === 'create_project') {
      setNewSeries('教学练习'); setNewTitle('教学小样（我的素材）');
      setNewCreationMode(route === 'reference' ? 'reference-video' : 'original');
      setCreateOpen(true); setProjectMenuOpen(false); return;
    }
    if (!project && action !== 'goal') { setNotice('请先新建或选择一部作品，再准备素材和分镜。'); setCreateOpen(true); return; }
    if (action === 'goal') { setWorkspacePage('creative'); setCreativeOpen(true); if (route === 'reference') setWorkspaceMode('reverse'); return; }
    if (['materials', 'upload', 'upload_material'].includes(action)) { setMaterialBridgeDevice(true); setMaterialBridgeOpen(true); return; }
    if (['joins', 'sound', 'assembly', 'queue', 'export', 'delivery', 'review_join', 'set_assembly_sound', 'assemble', 'inspect_task'].includes(action)) { setWorkspacePage('delivery'); return; }
    setWorkspacePage('shots');
    if (action === 'preset' || action === 'choose_stage') requestAnimationFrame(() => { const panel = document.querySelector<HTMLDetailsElement>('.director-preset-compact'); if (panel) { panel.open = true; panel.scrollIntoView({ block: 'nearest' }); } });
    else if (action === 'shots' || action === 'add_segment') setSegmentFormOpen(true);
    else if (['review', 'adopt', 'redo', 'reconcile'].includes(action)) { setShotEditTab('history'); setInspectorTab('history'); }
    else if (action === 'preflight' || action === 'validate') { setPipelineOpen(true); setNotice('请检查当前输入，再点击预检；预检不会生成视频。'); }
    else if (action === 'edit_inputs') setShotEditTab('materials');
    else setNotice('请在当前页面查看准备度和可用操作。');
  }
  function performReadinessAction(action: string, assemblyAction = false) {
    if (action === 'submit') { void saveAndRegenerate(); return; }
    if (action === 'assemble') { void startAssembly(); return; }
    if (action === 'validate' && currentGenerationStage) { void validatePipelineStage(currentGenerationStage, true, {
      prompt: currentPrompt || '', 'first-frame': segmentEdit.first_frame,
      'last-frame': segmentEdit.last_frame, guide: segmentEdit.audio_guide,
      duration: Number(segmentEdit.duration_seconds),
    }); return; }
    if (action === 'stop') { if (assemblyAction) void stopAssembly(); else void stopCurrentTask(); return; }
    if (action === 'reconcile') { if (assemblyAction) void resumeAssembly(); else void resumeCurrentTask(); return; }
    openGuideAction(action);
  }
  async function refreshPresetSelection() {
    const response = await fetch(`/api/projects/${encodeURIComponent(project!.id)}`);
    if (!response.ok) throw new Error('生成方式已保存，但作品刷新失败，请刷新页面。');
    const data = await response.json() as ProjectManifest;
    if (data.id !== activeProjectId.current) return;
    setProject(data); setPipelineStageId(selectVideoGenerationStage(data.pipeline ?? [], data.production_preset?.workflow)?.id ?? ''); setPipelineOpen(false);
    await refreshProjectState();
    setNotice('已选择生成方式，可以继续准备分镜和素材。');
  }
  const reviewConflictPanel = visibleReviewConflict && conflictingReview && <section className="director-creative-conflict" role="alert" aria-label="审核版本冲突"><div><strong>审核已更新 · revision {conflictingReview.revision}</strong><p>服务器意见：{conflictingReview.note || '无文字意见'}</p><p>本地意见：{currentNote || '无文字意见'}</p><p>选择后仍需明确保存；不会自动覆盖审核结论。</p></div><div className="director-creation-actions"><button className="button secondary compact" onClick={() => resolveReviewConflict(true)}>载入服务器审核意见</button><button className="button primary compact" onClick={() => resolveReviewConflict(false)}>保留本地审核意见</button></div></section>;
  const creativeConflictPanel = creativeConflict && <section className="director-creative-conflict" role="alert"><div><strong>本机草稿与服务器 revision {creativeConflict.revision} 不一致</strong><p>当前编辑框保留的是本机未提交版本；先选择载入服务器版本，或明确保留本机草稿后再保存。</p></div><div className="director-creation-actions"><button className="button secondary compact" onClick={() => { setCreativeDraft(creativeConflict.server); localStorage.setItem(creativeStorageKey, JSON.stringify(creativeConflict.server)); setCreativeConflict(null); setNotice(`已载入服务器创作设定 revision ${creativeConflict.revision}。`); }}>载入服务器版本</button><button className="button primary compact" onClick={() => { setCreativeConflict(null); setNotice('已保留本机草稿，请保存后写入新的服务器 revision。'); }}>保留本机草稿</button></div></section>;
  return <div className="director-workbench" style={{ '--sidebar-width': `${sidebarWidth}px` } as CSSProperties}>
    <aside className="director-sidebar">
      <div className="director-brand"><span className="director-brand-mark"><Clapperboard size={20} /></span><div><strong>片场</strong><small>导演工作台</small></div></div>
      <WorkspaceExplorer projects={projectList} activeProjectId={archive ? undefined : project?.id} activeSegments={planSegments} activeAssets={project?.assets ?? []} selectedKind={kind} selectedId={current.id} onOpenProject={openTreeProject} onOpenItem={openTreeItem} privateMode={privateMode} refreshKey={seriesRefreshKey} onOpenArchive={value => { setArchive(value); setCreateOpen(false); setProjectMenuOpen(false); }} onCreateWork={series => { setNewSeries(series); setNewTitle(''); setCreateOpen(true); setProjectMenuOpen(false); }} onDeleteDirectory={setDeleteDirectory} onRefreshProjects={() => { void refreshProjects(); if (project?.id) void refreshPlan().catch(() => setNotice('分镜读取失败，请稍后重试。')); }} />
      <div className="director-sidebar-bottom"><div className="director-connection"><span className={comfyOnline ? 'online' : ''} />{comfyOnline ? `ComfyUI ${comfyInfo.version ?? '已连接'}` : 'ComfyUI 离线'}</div><button className="director-resource-link" onClick={() => setMaterialBridgeOpen(true)}><FolderOpen size={14} />跨设备素材桥</button><button className="director-resource-link" onClick={() => setResourceOpen(open => !open)}><Settings2 size={14} />资源与连接</button></div>
    </aside>
    <div className="director-sidebar-resizer" role="separator" aria-label="调整作品目录宽度" aria-orientation="vertical" aria-valuemin={240} aria-valuemax={Math.floor(window.innerWidth / 2)} aria-valuenow={sidebarWidth} tabIndex={0} onPointerDown={beginSidebarResize} onKeyDown={event => { if (event.key === 'ArrowLeft') resizeSidebar(sidebarWidth - 24); if (event.key === 'ArrowRight') resizeSidebar(sidebarWidth + 24); }} />
    <main className="director-main">
      <header className="director-topbar"><div className="director-breadcrumb"><span>{archive?.series ?? project?.series ?? '我的作品'}</span><ChevronRight size={13} /><strong>{archive?.work ?? project?.title ?? '选择或新建作品'}</strong></div><nav className="director-topnav" aria-label="工作台功能"><button className={!archive && workspaceMode === 'production' && workspacePage === 'script' ? 'active' : ''} onClick={() => { setArchive(null); setWorkspaceMode('production'); setWorkspacePage('script'); }}>剧本与场次</button><button className={!archive && workspaceMode === 'production' && workspacePage === 'shots' ? 'active' : ''} onClick={() => { setArchive(null); setWorkspaceMode('production'); setWorkspacePage('shots'); }}>分镜制作</button><button className={!archive && workspaceMode === 'production' && workspacePage === 'creative' ? 'active' : ''} onClick={() => { setArchive(null); setWorkspaceMode('production'); setWorkspacePage('creative'); setCreativeOpen(true); }}>创作设定</button><button className={!archive && workspaceMode === 'reverse' ? 'active' : ''} onClick={() => { setArchive(null); setWorkspaceMode('reverse'); }}>参考拆解</button><button className={!archive && workspaceMode === 'production' && workspacePage === 'delivery' ? 'active' : ''} onClick={() => { setArchive(null); setWorkspaceMode('production'); setWorkspacePage('delivery'); }}>任务与成片</button></nav><div className="director-topbar-status"><span className={comfyOnline ? 'online-dot' : 'offline-dot'} />{comfyOnline ? '引擎已连接' : '引擎离线'}</div><button className="button primary compact" onClick={() => { setCreateOpen(open => !open); setProjectMenuOpen(false); }}>新建作品</button><button className="button secondary compact" onClick={() => { setProjectMenuOpen(open => !open); setCreateOpen(false); }}>导入素材</button>{privateMode && <button className="director-logout" onClick={onLogout} disabled={logoutBusy} title={sessionError || '退出登录'}><LogOut size={14} />退出登录</button>}</header>
      {privateMode && <FirstUseGuide projectId={project?.id} refreshKey={`${planRevision}:${projectState?.history.length ?? 0}:${projectTasks.length}`} onAction={openGuideAction} onOpenProject={id => void selectProject(id)} />}
      {(createOpen || projectMenuOpen) && <section className="director-top-popover" aria-label={createOpen ? '新建作品' : '导入素材'}>{createOpen ? <div className="director-mini-form"><strong>新建作品</strong><label>系列名称<input value={newSeries} onChange={event => setNewSeries(event.target.value)} placeholder="例如：家庭短片" /></label><label>作品名称<input value={newTitle} onChange={event => setNewTitle(event.target.value)} placeholder="例如：第一集" /></label><label>创作起点<select value={newCreationMode} onChange={event => setNewCreationMode(event.target.value)}>{creationRoutes.map(route => <option key={route.id} value={route.id}>{route.title}</option>)}</select></label>{newCreationMode === 'reference-series' && <label>沿用作品设定<select value={newReferenceProject} onChange={event => setNewReferenceProject(event.target.value)}><option value="">手动填写系列设定</option>{projectList.map(item => <option key={item.id} value={item.id}>{item.series} · {item.title}</option>)}</select></label>}<button className="button primary compact" disabled={creatingProject || !newSeries.trim() || !newTitle.trim()} onClick={() => void createProject()}>{creatingProject ? '正在创建…' : '创建并进入'}</button></div> : <div className="director-import"><strong>导入工作目录</strong><input ref={folderInputRef} className="visually-hidden-file" type="file" multiple {...({ webkitdirectory: '' } as unknown as InputHTMLAttributes<HTMLInputElement>)} onChange={event => setImportFiles(Array.from(event.target.files ?? []))} /><button className="button secondary compact" onClick={() => folderInputRef.current?.click()}><ArrowUpRight size={13} />选择文件夹</button><span>{importFiles.length ? `已选择 ${importFiles.length} 个文件` : '未选择文件夹'}</span><button className="button primary compact" disabled={!project || !importFiles.length} onClick={() => void uploadSelectedFiles()}>导入当前作品</button></div>}<button className="icon-button" aria-label="关闭面板" onClick={() => { setCreateOpen(false); setProjectMenuOpen(false); }}><X size={16} /></button></section>}
      {resourceOpen && <section className="director-resource-panel"><div><strong>连接与资源</strong><p>{comfyOnline ? `ComfyUI ${comfyInfo.version ?? ''} · 队列运行 ${comfyInfo.queue_running ?? 0} · 等待 ${comfyInfo.queue_pending ?? 0} · 显存空闲约 ${comfyInfo.gpu_free_mib ?? '?'} MiB。` : '引擎暂时离线。通过预检的请求仍可排队，服务器会在资源恢复后继续处理。'}</p></div><button className="icon-button" aria-label="关闭资源状态" onClick={() => setResourceOpen(false)}><X size={16} /></button></section>}
      <div className="director-content" data-workspace-page={workspacePage}>
        {archive ? <ArchiveBrowser archive={archive} /> : privateMode && !project ? <section className="director-no-project"><FolderOpen size={38} /><h1>从一部作品开始</h1><p>左侧会按系列整理你能访问的全部作品。新作品创建后，分镜、声音和图片都可以从目录中展开查看。</p><button className="button primary" onClick={() => setCreateOpen(true)}>新建第一部作品</button></section> : workspaceMode === 'reverse' ? <ReverseWorkbench key={project?.id} initialAnalysisId={referenceTarget?.projectId === project?.id ? referenceTarget?.analysisId : undefined} projectId={project?.id ?? ''} onConverted={count => { void refreshPlan(); setWorkspaceMode('production'); setWorkspacePage('shots'); setNotice(count ? `已从参考拆解新增 ${count} 个分镜草案，请准备本作关键帧和制作方案。` : '已打开对应制作草案，原有分镜与修改均已保留。'); }} /> : <>
        <header className="director-heading"><div><span className="director-kicker">{project?.eyebrow ?? '导演工作区'}</span><h1>{workspacePage === 'script' ? '剧本与场次' : workspacePage === 'creative' ? '创作设定' : workspacePage === 'delivery' ? '任务与成片' : '分镜制作'}</h1><p>{workspacePage === 'script' ? '先读故事，再把每场戏安排成镜头。' : workspacePage === 'creative' ? '确定这部作品的起点、角色与表达。' : workspacePage === 'delivery' ? '运行批次，检查并导出成片。' : '选择一段，修改素材与提示词，然后生成。'}</p></div><div className="director-heading-actions" hidden={workspacePage !== 'delivery'}><button className="button secondary" onClick={exportPlan}><ArrowDownToLine size={14} />导出审阅计划</button><button className="button secondary" onClick={exportProjectBundle}><ArrowDownToLine size={14} />导出项目清单</button></div></header>
        <div className="director-status-line"><span className="status-pill"><span className="status-pill-dot" />{planSegments.length ? `${planSegments.length} 个分镜已登记` : `${library.length} 个素材已登记`}</span><span className="status-pill status-pill-muted">{assemblyIsStale ? '历史成片 · 需重新装配' : hasAssemblyCheckpoint ? '当前成片已生成' : hasAssemblyArtifact ? '历史成片可预览' : '等待各分镜生成视频'}</span>{assemblyAsset && <button className="text-button" onClick={() => { choose(assemblyAsset); setWorkspacePage('shots'); }}><Film size={13} />打开历史成片</button>}<span className="director-status-spacer" /><span className="director-save-status">{storageError || segmentStorageError ? '本机草稿缓存失败' : '编辑后请点击保存，未保存修改仅留在本机'}</span></div>
        {workspacePage === 'delivery' && projectTasks.map(task => <TaskQueueStatus key={task.id} task={task} />)}
        {workspacePage === 'delivery' && savedReadiness && <div className={`director-readiness ${savedReadiness.assembly.ready ? 'ready' : 'has-gaps'}`} aria-label="装配准备度"><strong>装配准备度 · revision {savedReadiness.assembly.revision}</strong><span>{savedReadiness.assembly.blockers.map(blockerMessage).join(' · ') || (savedReadiness.assembly.ready ? '准备完成，可明确提交装配。' : readinessStateLabel(savedReadiness.assembly.state))}</span>{savedReadiness.assembly.next_actions.map(action => <button key={action.id} className="text-button" onClick={() => performReadinessAction(action.id, true)}>{action.label}</button>)}</div>}
        {workspacePage === 'delivery' && project && <AssemblyEditPanel projectId={project.id} planRevision={planRevision} assemblyPath={planAssembly?.output} onPlanChanged={() => { void refreshPlan(); }} onBrowse={() => setMaterialBridgeOpen(true)} onOpenShot={id => openTreeItem('video', id)} />}
        {workspacePage === 'delivery' && project && <MediaToolsPanel projectId={project.id} assets={[...new Map([...baseAssets, ...planVideoAssets, ...(assemblyAsset ? [assemblyAsset] : [])].map(a => [a.id, a])).values()].filter(a => (a.kind === 'audio' || a.kind === 'video') && Boolean(a.sources?.A || a.sources?.B)).map(a => ({ id: a.id, title: a.title, kind: a.kind }))} onBrowse={() => setMaterialBridgeOpen(true)} onComplete={() => {
          const completedProjectId = project.id;
          void fetch(`/api/projects/${encodeURIComponent(completedProjectId)}`).then(r => { if (!r.ok) throw new Error(); return r.json() as Promise<ProjectManifest>; }).then(data => setProject(previous => previous?.id === completedProjectId ? data : previous)).catch(() => { if (activeProjectId.current === completedProjectId) setNotice('媒体处理已完成，素材刷新失败，请重新选择作品。'); });
        }} />}
        {workspacePage === 'script' && project && <ScriptWorkbench key={project.id} projectId={project.id} shots={planSegments} request={fetch} onCreateShot={(sceneId, revision) => { setSegmentDraft(previous => ({ ...previous, script_scene_id: sceneId, script_revision: String(revision) })); setWorkspacePage('shots'); setSegmentFormOpen(true); setStageFormOpen(false); }} onSelectShot={id => openTreeItem('video', id)} />}
        {workspacePage === 'creative' && <CreationRoutes onScript={() => setWorkspacePage('script')} mode={creativeDraft.creation_mode} segmentCount={planSegments.length} onMode={mode => setCreativeDraft(previous => ({ ...previous, creation_mode: mode }))} onReference={() => setWorkspaceMode('reverse')} onSettings={() => setCreativeOpen(open => !open)} onSegment={() => { setWorkspacePage('shots'); setSegmentFormOpen(true); setStageFormOpen(false); }} onStage={() => { setWorkspacePage('shots'); setStageFormOpen(true); setSegmentFormOpen(false); }} />}
        {workspacePage === 'creative' && creativeOpen && <section className="director-creation-panel" aria-label="创作意图与世界观"><div className="director-form-heading"><div><span className="director-kicker">人工草稿 · 可随时退回修改</span><h2>创作意图与身份锚点</h2></div><span>{projectState?.creative ? `服务器档案 revision ${projectState.creative.revision} · ${projectState.creative.status === 'approved' ? '已确认' : '待确认'}` : '尚未提交服务器档案'}</span></div><div className="director-creation-grid"><label>创作意图<textarea rows={3} value={creativeDraft.intent} onChange={event => setCreativeDraft(previous => ({ ...previous, intent: event.target.value }))} placeholder="这部作品希望观众感受到什么？"></textarea></label><label>世界观 / 规则<textarea rows={3} value={creativeDraft.world} onChange={event => setCreativeDraft(previous => ({ ...previous, world: event.target.value }))} placeholder="时间、地点、叙事规则和不能违背的约束"></textarea></label><label>角色与身份锚点<input value={creativeDraft.character} onChange={event => setCreativeDraft(previous => ({ ...previous, character: event.target.value }))} placeholder="外形、服装、身份连续性"></input></label><label>声音与配音设定<input value={creativeDraft.voice} onChange={event => setCreativeDraft(previous => ({ ...previous, voice: event.target.value }))} placeholder="声线、情绪、演唱或对白规则"></input></label><label>参考视频备注（可选）<input value={creativeDraft.reference_video} onChange={event => setCreativeDraft(previous => ({ ...previous, reference_video: event.target.value }))} placeholder="描述想借鉴的参考；导入和分析请使用参考拆解入口"></input></label><label>参考系列（可选）<textarea rows={2} value={creativeDraft.reference_series} onChange={event => setCreativeDraft(previous => ({ ...previous, reference_series: event.target.value }))} placeholder="系列名称、代表作品、角色关系与风格规则" /></label><label>保留什么 / 改编什么<textarea rows={2} value={creativeDraft.adaptation} onChange={event => setCreativeDraft(previous => ({ ...previous, adaptation: event.target.value }))} placeholder="保留的结构、节奏与角色特征；本集的新内容及需要避免的重复" /></label><label>导演备注<textarea rows={2} value={creativeDraft.notes} onChange={event => setCreativeDraft(previous => ({ ...previous, notes: event.target.value }))} placeholder="需要人工逐项确认的风险或取舍"></textarea></label></div><div className="director-form-actions"><span>创作方式与设定一起保存；未提交修改仅缓存在当前浏览器。</span><div className="director-creation-actions"><button className="button secondary compact" disabled={stateSaving} onClick={() => void saveCreativeSettings('pending_review')}>保存版本</button><button className="button primary compact" disabled={stateSaving} onClick={() => void saveCreativeSettings('approved')}>保存并确认</button></div></div></section>}
        {workspacePage === 'shots' && segmentFormOpen && autoCreation && !advancedOpen && <section className="director-creation-panel" aria-label="添加分镜"><h2>描述这一段画面</h2><label>画面与动作描述<textarea rows={3} value={segmentDraft.prompt} onChange={event => setSegmentDraft(previous => ({ ...previous, prompt: event.target.value }))} placeholder="例如：旧物店中，女孩拿起一只怀表，听见远处的钟声" /></label><div className="director-creation-grid"><label>剧情时长（秒）<input type="number" min="4" max="15" step="0.1" value={segmentDraft.duration_seconds} onChange={event => setSegmentDraft(previous => ({ ...previous, duration_seconds: event.target.value }))} /></label><label>目标画幅<select value={generationDraft.aspect_ratio} onChange={event => setGenerationDraft(previous => ({ ...previous, aspect_ratio: event.target.value as ShotGenerationSettings['aspect_ratio'] }))}><option value="16:9">横屏 · 864×480</option><option value="1:1">方形 · 640×640</option><option value="9:16">竖屏 · 480×864</option></select></label></div><p>默认5秒横屏样片；无需上传图片。保存后可按需增加开始画面或表演声音，预检不生成。</p><button className="button primary" onClick={() => void createPlanSegment()}>保存分镜</button><button className="button secondary" onClick={() => setSegmentFormOpen(false)}>收起</button></section>}
        {workspacePage === 'shots' && segmentFormOpen && (!autoCreation || advancedOpen) && <section className="director-creation-panel" aria-label="添加分镜"><div className="director-form-heading"><div><span className="director-kicker">人工阶段 · 分镜 / 节拍</span><h2>添加分镜</h2>{segmentDraft.script_scene_id && <p>所属场次：{segmentDraft.script_scene_id} · 请单独设置这个镜头的时长。<button className="text-button" onClick={() => setSegmentDraft(previous => ({ ...previous, script_scene_id: '', script_revision: '' }))}>解除关联</button></p>}</div><button className="icon-button" aria-label="关闭添加分镜" onClick={() => setSegmentFormOpen(false)}><X size={16} /></button></div><div className="director-creation-grid"><label>片段编号<input value={segmentDraft.segment_id} onChange={event => setSegmentDraft(previous => ({ ...previous, segment_id: event.target.value }))} placeholder="留空则自动编号"></input></label><label>时长（秒）<input type="number" min="0.1" step="0.1" value={segmentDraft.duration_seconds} onChange={event => setSegmentDraft(previous => ({ ...previous, duration_seconds: event.target.value }))}></input></label><label>场景<input value={segmentDraft.location} onChange={event => setSegmentDraft(previous => ({ ...previous, location: event.target.value }))} placeholder="允许原创场景"></input></label><label>景别<input value={segmentDraft.shot_size} onChange={event => setSegmentDraft(previous => ({ ...previous, shot_size: event.target.value }))}></input></label><label>镜头运动<input value={segmentDraft.camera} onChange={event => setSegmentDraft(previous => ({ ...previous, camera: event.target.value }))}></input></label><label>服装 / 造型<input value={segmentDraft.wardrobe} onChange={event => setSegmentDraft(previous => ({ ...previous, wardrobe: event.target.value }))}></input></label><label className="director-creation-wide">动作与表演<input value={segmentDraft.performance} onChange={event => setSegmentDraft(previous => ({ ...previous, performance: event.target.value }))} placeholder="表情、口型、手势、节拍动作"></input></label><label className="director-creation-wide">提示词草稿<textarea rows={3} value={segmentDraft.prompt} onChange={event => setSegmentDraft(previous => ({ ...previous, prompt: event.target.value }))} placeholder="先写导演意图，再决定模型和工作流"></textarea></label>{advancedOpen && <label>工作流引用（可选）<input value={segmentDraft.workflow} onChange={event => setSegmentDraft(previous => ({ ...previous, workflow: event.target.value }))} placeholder="ComfyUI JSON 路径"></input></label>}<MaterialSelect excludedValues={diagnosticPaths} label="首帧" kind="image" value={segmentDraft.first_frame} options={materialOptions} onChange={value => setSegmentDraft(previous => ({ ...previous, first_frame: value }))} onBrowse={() => setMaterialBridgeOpen(true)} /><MaterialSelect excludedValues={diagnosticPaths} label="尾帧" kind="image" value={segmentDraft.last_frame} options={materialOptions} onChange={value => setSegmentDraft(previous => ({ ...previous, last_frame: value }))} onBrowse={() => setMaterialBridgeOpen(true)} /><MaterialSelect excludedValues={diagnosticPaths} label="表演引导音频" kind="audio" value={segmentDraft.audio_guide} options={materialOptions} onChange={value => setSegmentDraft(previous => ({ ...previous, audio_guide: value }))} onBrowse={() => setMaterialBridgeOpen(true)} /><MaterialSelect excludedValues={diagnosticPaths} label="交付音频" kind="audio" value={segmentDraft.delivery_master} options={materialOptions} onChange={value => setSegmentDraft(previous => ({ ...previous, delivery_master: value }))} onBrowse={() => setMaterialBridgeOpen(true)} /></div><div className="director-form-actions"><span>保存后可直接在当前分镜下方修改和审核。</span><button className="button primary compact" onClick={() => void createPlanSegment()}>保存分镜计划</button></div></section>}
        {workspacePage === 'shots' && stageFormOpen && <section className="director-creation-panel" aria-label="添加制作步骤"><div className="director-form-heading"><div><span className="director-kicker">执行契约 · 可人工覆盖</span><h2>添加声音、图像或视频步骤</h2></div><button className="icon-button" aria-label="关闭添加步骤" onClick={() => setStageFormOpen(false)}><X size={16} /></button></div><div className="director-creation-grid"><label>步骤名称<input value={stageTitle} onChange={event => setStageTitle(event.target.value)} placeholder="例如：角色配音或动作设计"></input></label><label>执行方式<select value={stageBackend} onChange={event => setStageBackend(event.target.value)}><option>混合流程</option><option>ComfyUI 工作流</option><option>本地脚本</option></select></label><label className="director-creation-wide">这一步的目的<input value={stagePurpose} onChange={event => setStagePurpose(event.target.value)} placeholder="导演能判断输入和输出是否成立"></input></label><label>输入（逗号分隔）<input value={stageInputs} onChange={event => setStageInputs(event.target.value)} placeholder="角色设定, 音频, 提示词"></input></label><label>输出（逗号分隔）<input value={stageOutputs} onChange={event => setStageOutputs(event.target.value)} placeholder="候选素材"></input></label><label className="director-creation-wide">工作流或脚本引用（可选）<input value={stageWorkflow} onChange={event => setStageWorkflow(event.target.value)} placeholder="先保存契约，执行引用可后补"></input></label></div><div className="director-form-actions"><span>步骤登记后可在高级操作中查看。</span><button className="button primary compact" onClick={() => void createPipelineStage()}>保存制作步骤</button></div></section>}
        {workspacePage === 'shots' && project && advancedOpen && !planSegments.length && <ProductionPresetPicker key={project.id} projectId={project.id} installed={project.production_preset?.id} onInstalled={refreshPresetSelection} />}
        {workspacePage === 'shots' && <>
        <div className="director-shot-toolbar"><div className="director-kind-tabs" role="group" aria-label="素材类型">{(['video', 'audio', 'image'] as Kind[]).map(type => <button key={type} aria-pressed={kind === type} onClick={() => switchKind(type)}>{type === 'video' ? '分镜' : type === 'audio' ? '声音' : '关键帧'} <span>{type === 'video' ? planVideoAssets.length : library.filter(asset => asset.kind === type).length}</span></button>)}</div><div className="director-creation-actions"><button className="button secondary compact" onClick={() => setSegmentFormOpen(open => !open)}>+ 添加分镜</button>{advancedOpen && currentPlanSegment && generationEdit.mode !== 'auto' && <button className="button secondary compact" onClick={() => setGenerationEdit(previous => ({ ...previous, mode: 'auto' }))}>切换为自动创作（保存后生效）</button>}{advancedOpen && <button className="button secondary compact" onClick={() => setStageFormOpen(open => !open)}>高级制作步骤</button>}</div></div>
        {kind === 'audio' && project?.id && <SpeechPanel key={project.id} projectId={project.id} options={materialOptions} onBrowse={() => setMaterialBridgeOpen(true)} onComplete={() => {
          const completedProjectId = project.id;
          void fetch(`/api/projects/${encodeURIComponent(completedProjectId)}`).then(response => {
            if (!response.ok) throw new Error('声音素材刷新失败');
            return response.json() as Promise<ProjectManifest>;
          }).then(data => setProject(previous => previous?.id === completedProjectId ? data : previous)).catch(() => {
            if (activeProjectId.current === completedProjectId) setNotice('声音已完成，素材刷新失败，请重新选择作品。');
          });
        }} />}
        {kind === 'audio' && project?.id && <AudioEditPanel projectId={project.id}
          audioAssets={(project.assets ?? []).filter(asset => asset.kind === 'audio' && typeof asset.id === 'string').map(asset => ({ id: String(asset.id), title: String(asset.title ?? asset.id) }))}
          onBrowse={() => setMaterialBridgeOpen(true)} onComplete={() => {
            const completedProjectId = project.id;
            void fetch(`/api/projects/${encodeURIComponent(completedProjectId)}`).then(response => {
              if (!response.ok) throw new Error('音频候选刷新失败');
              return response.json() as Promise<ProjectManifest>;
            }).then(data => setProject(previous => previous?.id === completedProjectId ? data : previous)).catch(() => {
              if (activeProjectId.current === completedProjectId) setNotice('编辑已完成，素材刷新失败，请重新选择作品。');
            });
          }} />}
        {kind === 'image' && project?.id && <KeyframePanel key={project.id} projectId={project.id} options={materialOptions} onBrowse={() => setMaterialBridgeOpen(true)} onComplete={() => {
          const completedProjectId = project.id;
          void fetch(`/api/projects/${encodeURIComponent(completedProjectId)}`).then(response => {
            if (!response.ok) throw new Error('关键帧素材刷新失败');
            return response.json() as Promise<ProjectManifest>;
          }).then(data => setProject(previous => previous?.id === completedProjectId ? data : previous)).catch(() => {
            if (activeProjectId.current === completedProjectId) setNotice('关键帧已完成，素材刷新失败，请重新选择作品。');
          });
        }} />}
        <section className="director-board" aria-label="分镜工作区">
          <div className="director-board-main">
            <div className="director-timeline-header"><div><h2>分镜时间线</h2></div><div className="director-timeline-tools"><span>{timelineCountLabel}</span><button className="button secondary compact" onClick={() => setPipelineOpen(open => !open)}><Workflow size={14} />{pipelineOpen ? '收起制作流程' : '制作流程'}</button></div></div>
            <details className="director-progress-details"><summary>阶段检查与确认</summary><div className="director-stage-rail" aria-label="当前片段生产阶段">{productionRail.map((step, stepIndex) => <div key={step.id} className={`director-stage-step ${step.state}`}><span className="director-stage-marker">{step.state === 'done' ? '✓' : String(stepIndex + 1).padStart(2, '0')}</span><div><strong>{step.label}</strong><small>{step.detail}</small>{['intent', 'character', 'scene', 'performance', 'storyboard', 'materials'].includes(step.id) && step.state !== 'waiting' ? <button className="director-stage-action" onClick={() => void saveCheckpoint(step.id, step.state === 'done' ? 'changes_requested' : 'approved')}>{step.state === 'done' ? '退回修改' : '人工确认'}</button> : <em>{step.mode === 'auto' ? '执行器阶段' : '等待上游'}</em>}</div></div>)}</div></details>
            <div className="director-timeline" role="list" aria-label="分镜片段列表">{timelineAssets.map((asset, assetIndex) => <button key={asset.id} role="listitem" className={`director-shot ${asset.id === current.id ? 'selected' : ''} ${!asset.ready ? 'pending' : ''}`} onClick={() => choose(asset)} aria-label={`选择 ${asset.id} ${asset.title}`}><div className="director-shot-thumb">{asset.image ? <img src={asset.image} alt="" /> : <span>{asset.kind === 'audio' ? <Music2 size={20} /> : asset.kind === 'image' ? <ImageIcon size={20} /> : <Film size={20} />}</span>}<b>{asset.id === (assemblyAsset?.id ?? '') ? 'MASTER' : asset.id}</b><em>{asset.start !== undefined ? `${(asset.end! - asset.start).toFixed(asset.end! - asset.start! < 2 ? 1 : 0)}s` : '参考'}</em></div><strong>{asset.title.replace(`${asset.id} · `, '')}</strong><small>{assetIndex === 0 && asset.id === assemblyAsset?.id ? '完整候选' : asset.subtitle.split(' · ')[0]}</small></button>)}</div>
            <div className="director-focus-heading"><strong>{assetTitle(current.id, current.title, kind)}</strong><span>{isProjectVideoMaterial ? '项目素材' : focusIndex >= 0 ? `${focusIndex + 1} / ${focusList.length}` : '尚无分镜'}</span></div>
            <div className={`director-focus-strip ${focusList.length <= 1 ? 'single' : focusIndex <= 0 ? 'no-previous' : focusIndex >= focusList.length - 1 ? 'no-next' : ''}`}>
              <button type="button" className="director-neighbor director-neighbor-previous" disabled={focusIndex <= 0} onClick={() => choose(focusList[focusIndex - 1])} aria-label="查看上一段">{focusList[focusIndex - 1]?.image ? <img src={focusList[focusIndex - 1].image} alt="" /> : <Film size={24} />}<small>上一段</small><strong>{focusList[focusIndex - 1] ? assetTitle(focusList[focusIndex - 1].id, focusList[focusIndex - 1].title, kind) : '没有上一段'}</strong></button>
             <section className="director-preview-area" aria-label="当前片段预览"><div className="director-preview-head"><div><span className="director-kicker">{isAssemblyAsset ? '完整成片' : isProjectVideoMaterial ? '项目视频素材' : `当前${kind === 'video' ? '分镜' : kind === 'audio' ? '音频' : '图片'}`}</span><h2>{assetTitle(current.id, current.title, kind)}</h2></div><div className="director-preview-meta"><span>{isProjectVideoMaterial ? '已登记素材' : current.ready ? '当前版本' : current.image ? '已准备首帧' : '待准备'}</span>{kind !== 'image' && <span>{range(current)}</span>}</div></div><div className="director-preview-stage"><div className={`director-media director-media-${kind}`}>{current.ready && kind === 'video' && <video key={source} ref={attachMedia} src={source} poster={current.image} {...mediaProps} aria-label={`${current.id} 视频预览`} />}{current.ready && kind === 'image' && <><img src={current.image} alt={current.title} /><button className="image-zoom icon-button" aria-label="放大当前图片" onClick={() => setLightbox(true)}><Maximize2 size={17} /></button></>}{current.ready && kind === 'audio' && <div className="director-audio-stage"><div className={`record ${playing ? 'record-playing' : ''}`}><div><Music2 size={30} /></div></div><span className="director-kicker">音频片段</span><h2>{assetTitle(current.id, current.title, kind)}</h2><p>当前音频</p><audio key={source} ref={attachMedia} src={source} {...mediaProps} aria-label={`${current.id} 音频预览`} /></div>}{!current.ready && current.image && <img className="director-unrendered-frame" src={current.image} alt={`${assetTitle(current.id, current.title, kind)}的首帧`} />}{!current.ready && <div className={`director-empty ${current.image ? 'has-first-frame' : ''}`}><strong>{current.image ? '首帧已准备，视频还没生成' : `这一段还没有${kind === 'audio' ? '音频' : kind === 'image' ? '图片' : '视频'}`}</strong><p>{kind === 'video' ? `已准备：${[current.image ? '首帧' : '', currentAudioSource ? '音频' : '', currentPrompt ? '提示词' : ''].filter(Boolean).join('、') || '可在下方填写提示词和素材'}。生成后会自动显示在这里。` : '可在下方准备素材并生成。'}</p></div>}</div>{current.ready && <span className="director-stage-label">{isProjectVideoMaterial ? '参考视频' : kind === 'video' ? '当前视频' : kind === 'audio' ? '当前音频' : '当前图片'}</span>}</div><div className="director-transport"><button className="icon-button" aria-label="上一段" disabled={focusIndex <= 0} onClick={() => choose(focusList[focusIndex - 1])}><ChevronLeft size={18} /></button><button className="director-play-button" aria-label={playing ? '暂停当前片段' : '播放当前片段'} disabled={playbackDisabled} title={current.ready && kind !== 'image' && mediaStatus === 'loading' ? '素材加载中…' : undefined} onClick={togglePlay}>{playing ? <Pause size={16} /> : <Play size={16} />}</button><button className="icon-button" aria-label="下一段" disabled={focusIndex < 0 || focusIndex >= focusList.length - 1} onClick={() => choose(focusList[focusIndex + 1])}><ChevronRight size={18} /></button><span>{current.ready && kind !== 'image' && mediaStatus === 'loading' ? '素材加载中…' : assetTitle(current.id, current.title, kind)}</span><label className="director-continuous"><input type="checkbox" checked={continuous} onChange={event => setContinuous(event.target.checked)} /><span />连续播放</label></div></section>
              <button type="button" className="director-neighbor director-neighbor-next" disabled={focusIndex < 0 || focusIndex >= focusList.length - 1} onClick={() => choose(focusList[focusIndex + 1])} aria-label="查看下一段">{focusList[focusIndex + 1]?.image ? <img src={focusList[focusIndex + 1].image} alt="" /> : <Film size={24} />}<small>下一段</small><strong>{focusList[focusIndex + 1] ? assetTitle(focusList[focusIndex + 1].id, focusList[focusIndex + 1].title, kind) : '没有下一段'}</strong></button>
            </div>
            <section className="director-edit-sheet" aria-label="当前分镜修改">
              <div className="director-edit-heading"><div><span className="director-kicker">只编辑当前这一段</span><h2>{currentPlanSegment ? `${assetLabel(current.id, kind)}的内容` : isAssemblyAsset ? '完整成片' : editableMaterial ? '素材档案' : '请先添加分镜'}</h2>{currentPlanSegment?.script_scene_id && <><button className="text-button" onClick={() => setWorkspacePage('script')}>所属场次 {currentPlanSegment.script_scene_id} · 查看剧本</button><button className="text-button" disabled={scriptLinkSaving} onClick={() => void unlinkScriptScene()}>解除场次关联</button></>}</div><span>{isProjectVideoMaterial ? '素材已登记 · 未生成分镜' : taskState ? taskStateLabel(taskState) : current.ready ? '当前版本已生成' : current.image ? '已有首帧 · 视频待生成' : '视频待生成'}</span></div>
              {currentPlanSegment ? <>
                <div className="shot-edit-tabs" role="tablist" aria-label="分镜编辑栏目"><button type="button" role="tab" aria-selected={shotEditTab === 'materials'} onClick={() => setShotEditTab('materials')}>画面与声音</button><button type="button" role="tab" aria-selected={shotEditTab === 'prompt'} onClick={() => setShotEditTab('prompt')}>提示词 <span>{currentPrompt.length.toLocaleString('zh-CN')} 字</span></button><button type="button" role="tab" aria-selected={shotEditTab === 'history'} onClick={() => setShotEditTab('history')}>历史记录 <span>{shotVersionsTotal}</span></button></div>
                {shotEditTab === 'materials' && <div className="director-edit-grid" role="tabpanel">
                  {generationEdit.mode === 'auto' && <><label className="director-edit-wide">画面与动作描述<textarea rows={3} value={currentPrompt} onChange={event => { updateDraft(current.id, { prompt: event.target.value }); setSegmentEdit(previous => ({ ...previous, prompt: event.target.value })); }} placeholder="描述你想看到的画面、动作和声音" /></label><label>目标画幅<select value={generationEdit.aspect_ratio} onChange={event => setGenerationEdit(previous => ({ ...previous, aspect_ratio: event.target.value as ShotGenerationSettings['aspect_ratio'] }))}><option value="16:9">横屏 · 864×480</option><option value="1:1">方形 · 640×640</option><option value="9:16">竖屏 · 480×864</option></select><small>首轮低分辨率样片，4–15秒；无需选择工作流</small></label></>}
                  {!autoCreation && <label className="director-edit-wide">导演意图<textarea rows={2} value={segmentEdit.performance} onChange={event => setSegmentEdit(previous => ({ ...previous, performance: event.target.value }))} placeholder="这一段要表达什么、人物怎样行动？" /></label>}
                  <details className="director-edit-more director-edit-wide" open={!autoCreation}><summary>画面与声音控制（可选）</summary><div className="director-edit-grid">
                  {isTextGeneration && generationEdit.mode !== 'auto' ? <p className="director-edit-wide">当前方案仅用文字生成，无需首尾帧或表演音频。要控制起始画面，请明确选择图生方案；成片音频可在后期设置。</p> : <><MaterialSelect excludedValues={diagnosticPaths} label={generationEdit.mode === 'auto' ? '开始画面（可选）' : '首帧'} kind="image" value={segmentEdit.first_frame} options={materialOptions} onChange={value => setSegmentEdit(previous => ({ ...previous, first_frame: value }))} onBrowse={() => setMaterialBridgeOpen(true)} />
                  <MaterialSelect excludedValues={diagnosticPaths} label={generationEdit.mode === 'auto' ? '结束画面（可选）' : '尾帧'} kind="image" value={segmentEdit.last_frame} options={materialOptions} onChange={value => setSegmentEdit(previous => ({ ...previous, last_frame: value }))} onBrowse={() => setMaterialBridgeOpen(true)} />
                  <MaterialSelect excludedValues={diagnosticPaths} label={generationEdit.mode === 'auto' ? '用于表演的声音（可选）' : '表演音频'} kind="audio" value={segmentEdit.audio_guide} options={materialOptions} onChange={value => setSegmentEdit(previous => ({ ...previous, audio_guide: value }))} onBrowse={() => setMaterialBridgeOpen(true)} /></>}
                  {generationEdit.mode === 'auto' && segmentEdit.audio_guide && <label>声音含义<select value={generationEdit.sound_mode} onChange={event => setGenerationEdit(previous => ({ ...previous, sound_mode: event.target.value as ShotGenerationSettings['sound_mode'] }))}><option value="performance_reference">表演参考 · 生成声音可能变化</option><option value="locked_dialogue">严格使用已有对白 · 待小样验证</option></select></label>}
                  <MaterialSelect excludedValues={diagnosticPaths} label="成片音频" kind="audio" value={segmentEdit.delivery_master} options={materialOptions} onChange={value => setSegmentEdit(previous => ({ ...previous, delivery_master: value }))} onBrowse={() => setMaterialBridgeOpen(true)} /></div></details>
                  <details className="director-edit-more director-edit-wide"><summary>场景、时长与镜头设置</summary><div className="director-segment-edit-grid"><label>时长（秒）<input type="number" min="0.1" step="0.01" value={segmentEdit.duration_seconds} onChange={event => setSegmentEdit(previous => ({ ...previous, duration_seconds: event.target.value }))} /></label><label>场景<input value={segmentEdit.location} onChange={event => setSegmentEdit(previous => ({ ...previous, location: event.target.value }))} /></label><label>景别<input value={segmentEdit.shot_size} onChange={event => setSegmentEdit(previous => ({ ...previous, shot_size: event.target.value }))} /></label><label>镜头<input value={segmentEdit.camera} onChange={event => setSegmentEdit(previous => ({ ...previous, camera: event.target.value }))} /></label><label>造型<input value={segmentEdit.wardrobe} onChange={event => setSegmentEdit(previous => ({ ...previous, wardrobe: event.target.value }))} /></label></div></details>
                </div>}
                {shotEditTab === 'prompt' && <div role="tabpanel"><ShotPromptEditor key={currentPlanSegment.id} value={currentPrompt} savedValue={currentPlanSegment.prompt ?? ''} onChange={value => { updateDraft(current.id, { prompt: value }); setSegmentEdit(previous => ({ ...previous, prompt: value })); updatePipelineValue('prompt', value); }} /></div>}
                {shotEditTab === 'history' && <div className="shot-version-list" role="tabpanel"><p>每次成功生成都会自动成为当前版本。这里保留先前的画面和生成时的设置；恢复旧版本后仍可继续修改。</p>{shotVersionsError && <p role="alert">{shotVersionsError}</p>}{shotVersions.length ? shotVersions.map((version, versionIndex) => <article key={version.task_id} className={`shot-version ${version.is_current ? 'current' : ''}`}><div className="shot-version-preview"><video src={sharedMediaUrl(version.video_path)} preload="metadata" controls aria-label={`${assetLabel(current.id, kind)}历史版本${versionIndex + 1}`} /></div><div className="shot-version-info"><strong>{version.is_current ? '当前版本' : `第 ${shotVersionsTotal - versionIndex} 次生成`}</strong><time dateTime={version.generated_at}>生成于 {localSystemTime(version.generated_at)}</time>{version.restored_at && <small>最近恢复：{localSystemTime(version.restored_at)}</small>}<small>{[version.snapshot?.location ? String(version.snapshot.location) : '场景未登记', version.snapshot?.duration_seconds ? String(version.snapshot.duration_seconds) + ' 秒' : '时长未登记'].join(' · ')}{version.snapshot_complete === false ? ' · 旧任务信息不完整' : ''}</small><details><summary>查看当时的提示词与素材</summary><p>{String(version.snapshot?.prompt ?? (version.snapshot?.execution_snapshot as Record<string, unknown> | undefined)?.prompt ?? '未登记提示词')}</p><div className="shot-version-materials"><span>首帧{versionMaterial(version, 'first') ? <img src={sharedMediaUrl(versionMaterial(version, 'first'))} alt="历史首帧" /> : '未登记'}</span><span>尾帧{versionMaterial(version, 'last') ? <img src={sharedMediaUrl(versionMaterial(version, 'last'))} alt="历史尾帧" /> : '未登记'}</span><span>表演音频{versionMaterial(version, 'guide') ? <audio src={sharedMediaUrl(versionMaterial(version, 'guide'))} controls preload="none" /> : '未登记'}</span></div></details>{!version.is_current && <button type="button" className="button secondary compact" disabled={Boolean(restoringVersion)} onClick={() => void restoreShotVersion(version)}>{restoringVersion === version.task_id ? '恢复中…' : '恢复为当前'}</button>}</div></article>) : !shotVersionsError && <p>还没有成功生成的历史版本。</p>}{shotVersions.length < shotVersionsTotal && <button type="button" className="button secondary compact" onClick={() => setShotVersionsOffset(shotVersions.length)}>查看更多历史</button>}</div>}
                <div className="director-edit-actions">
                  <button className="button secondary" onClick={() => void saveCurrentSegment()}>保存修改</button>
                  <button className="button primary" disabled={activeTaskStates.includes(taskState)} onClick={() => currentGenerationStage ? void saveAndRegenerate() : openGenerationSettings()}><Play size={15} />{!currentGenerationStage ? '先选择生成方式' : current.ready ? '保存并重新生成' : '保存并生成这一段'}</button>
                  {currentGenerationStage && advancedOpen && <span className="director-muted" aria-label="当前生成方案">{currentGenerationStage.title}</span>}
                  <button className="button secondary" onClick={() => void checkPendingSubmission()}>核对上次提交</button>
                  {activeTaskStates.includes(taskState) && <button className="button secondary" onClick={() => void stopCurrentTask()}><Square size={14} />停止任务</button>}
                  <button className="button secondary" onClick={() => setAdvancedOpen(open => !open)}>{advancedOpen ? '收起更多操作' : '更多操作'}</button>
                </div>
                {supportsH3Duration && <section className="director-edit-wide director-operation-panel" aria-label="H3 镜头时长">
                  <div className="director-panel-title"><div><h3>镜头时长</h3><small>按剧情设定 4–15 秒；预检显示预计帧数，生成后另测实际媒体</small></div></div>
                  <div className="director-segment-edit-grid"><label>请求时长（秒）<input type="number" min="4" max="15" step="0.1" value={segmentEdit.duration_seconds} onChange={event => { setSegmentEdit(previous => ({ ...previous, duration_seconds: event.target.value })); updatePipelineValue('duration', Number(event.target.value)); }} /></label>{advancedOpen && <label>随机种子<input type="number" min="0" max="2147483647" step="1" value={String(pipelineValues.seed ?? 42)} onChange={event => { updatePipelineValue('seed', event.target.value); if (generationEdit.mode === 'auto') setGenerationEdit(previous => ({ ...previous, seed: Number(event.target.value) })); }} /></label>}</div>
                  <div className="director-edit-actions"><button type="button" className="button secondary compact" disabled={pipelineChecking} onClick={() => void previewH3Duration()}>{pipelineChecking ? '预检中…' : '保存并预检时长'}</button><span>预检不提交生成；实际输出按 17k+5 帧网格对齐。</span></div>
                  {pipelineValidation && pipelineValidation.stage_id === currentGenerationStage?.id && pipelineValidation.h3_duration && <dl className="director-fact-list"><div><dt>剧情请求／预计候选</dt><dd>{pipelineValidation.h3_duration.requested_seconds} 秒 → {pipelineValidation.h3_duration.frame_count} 帧／{pipelineValidation.h3_duration.playback_seconds} 秒</dd></div><div><dt>画幅</dt><dd>{pipelineValidation.h3_duration.width}×{pipelineValidation.h3_duration.height} · {pipelineValidation.h3_duration.aspect_ratio} · {pipelineValidation.h3_duration.fps} fps</dd></div><div><dt>生成模式</dt><dd>{pipelineValidation.h3_duration.generation_mode === 'text_to_video' ? '纯文本 · 首尾帧不适用' : advancedOpen ? `图生 · 首帧 ${pipelineValidation.h3_duration.first_frame_node_id} · ${pipelineValidation.h3_duration.first_frame}` : '图生 · 使用所选画面'}</dd></div><div><dt>{advancedOpen ? '提示词绑定' : '画面描述'}</dt><dd>{advancedOpen && `${pipelineValidation.h3_duration.prompt_node_id} · `}{pipelineValidation.h3_duration.prompt}</dd></div></dl>}
                </section>}
                {pipelineValidation?.stage_id === currentGenerationStage?.id && pipelineValidation?.creative_inspection && <details><summary>提交前参考差异核对</summary><ReferenceDifferenceSummary inspection={pipelineValidation.creative_inspection} /></details>}
                {project && advancedOpen && <ProductionPresetPicker key={project.id} projectId={project.id} installed={project.production_preset?.id} onInstalled={refreshPresetSelection} />}
              </> : isAssemblyAsset ? <>
                <p className="director-edit-empty">{assemblyIsStale ? '分镜已变化，请重新装配以更新成片。旧成片仍可预览。' : '这是当前装配成片。继续修改分镜后可重新装配。'}</p>
              </> : editableMaterial ? <div className="director-edit-grid"><label className="director-edit-wide">素材显示名称<input aria-label="素材显示名称" value={materialTitle} maxLength={160} onChange={event => setMaterialTitle(event.target.value)} /></label><div className="director-edit-actions"><button className="button secondary" disabled={savingMaterialTitle || !materialTitle.trim() || materialTitle.trim() === editableMaterial.title} onClick={() => void saveMaterialTitle()}>{savingMaterialTitle ? '保存中…' : '保存名称'}</button></div><p className="director-edit-empty">这是已登记素材。可预览并命名；生成候选只来自对应分镜任务。</p></div> : <p className="director-edit-empty">这项素材没有分镜计划。可以从上方添加分镜，或在左侧选择已有分镜。</p>}
            </section>
            {currentTask && <TaskQueueStatus task={currentTask} />}
            {currentPlanSegment && pipelineError && <div className="director-readiness has-gaps" role="alert"><strong>预检未通过</strong><span>{pipelineError}</span><span>请处理上述原因后重新预检；本次未提交生成。</span></div>}
            {currentPlanSegment && <div className={`director-readiness ${currentReadiness?.ready ? 'ready' : 'has-gaps'}`}><strong>{currentReadiness?.ready ? '生成准备度已满足' : readinessGaps.length ? `准备度缺口 ${readinessGaps.length}` : readinessStateLabel(currentReadiness?.state)}</strong><button className="button secondary compact" disabled={pipelineChecking || !currentGenerationStage} onClick={() => performReadinessAction('validate')}>预检未保存输入</button><span>{readinessGaps.join(' · ') || `保存版 revision ${currentReadiness?.revision ?? planRevision} · ${readinessStateLabel(currentReadiness?.state)}`}</span>{currentReadiness?.next_actions.map(action => <button key={action.id} className="text-button" onClick={() => performReadinessAction(action.id)}>{action.label}</button>)}{draftReadiness && <div>未保存参数预检 · revision {draftReadiness.revision}：{draftReadiness.ready ? '可提交，仍需明确点击生成' : draftReadiness.blockers.map(blockerMessage).join(' · ') || readinessStateLabel(draftReadiness.state)}</div>}</div>}
            {!isProjectVideoMaterial && <div className="director-decision-strip" aria-label="当前片段审核门"><div><span>审核门</span><strong>{hasApprovalCheckpoint ? '已通过，可进入精细化' : hasGeneratedCheckpoint ? '先判断内容是否可用' : '生成前检查'}</strong></div><small>{kind === 'video' ? '低清样片只审画面、声音与动作' : '素材审核不会自动绑定到分镜'} · 当前：{currentReview}</small></div>}
          </div>
          <details className="director-advanced-details" hidden><summary>更多执行信息</summary>
          <aside className="director-inspector" aria-label="当前分镜详情"><div className="director-inspector-header"><div><span className="director-kicker">当前片段</span><h2>{assetLabel(current.id, kind)} <small>{currentReview}</small></h2></div><button className="icon-button" aria-label="刷新当前项目" onClick={() => { void refreshPlan(); }}><Radio size={16} /></button></div><div className="director-inspector-actions"><button className="button secondary compact" onClick={() => void checkPendingSubmission()}>核对提交回执</button><button className="button primary" disabled={!currentExecutable || !currentGenerationStage || Boolean(taskState && !['succeeded', 'failed', 'stopped', 'needs_reconcile'].includes(taskState)) || !current.id} onClick={() => void startCurrentTask()}><Play size={14} />{activeTaskStates.includes(taskState) ? taskStateLabel(taskState) : current.ready ? '重做当前片段' : '启动当前片段'}</button>{['preparing', 'submitting', 'queued', 'scheduler_waiting', 'running', 'stop_requested', 'stopping'].includes(taskState) && <button className="button secondary" onClick={stopCurrentTask}><Square size={14} />停止</button>}{['stopped', 'needs_reconcile'].includes(taskState) && <button className="button secondary" onClick={resumeCurrentTask}><Play size={14} />恢复</button>}<button className="button adopt" onClick={adoptCurrent} disabled={!current.ready || (isAssemblyAsset && assemblyIsStale) || (variant === 'B' && currentCandidateBIsFinish && !hasApprovalCheckpoint)}><Check size={14} />{variant === 'B' && currentCandidateBIsFinish ? '采用精细版' : isAssemblyAsset ? '通过全片审核' : kind === 'video' ? '采用样片' : '采用素材候选'}</button></div><div className="director-task-status"><span className={taskState === 'succeeded' ? 'task-success' : taskState === 'failed' ? 'task-failed' : ''} />任务状态 <strong>{taskState ? taskStateLabel(taskState) : current.ready ? '已有结果' : '尚未提交'}</strong>{currentTask?.queue && <span>{currentTask.queue.reason?.message || currentTask.queue.state}{currentTask.queue.position != null && ` · 队列位置 ${currentTask.queue.position}`}</span>}{taskId && <small>{taskId}</small>}</div>{!currentExecutable && !isAssemblyAsset && <p className="director-next-step">尚未登记制作方案。先添加制作步骤与素材，再开始生成。</p>}{taskId && ['stopped', 'failed', 'needs_reconcile'].includes(taskState) && <div className="director-panel-actions"><button className="button secondary compact" onClick={() => void resumeCurrentTask()}>{taskState === 'needs_reconcile' ? '核对原任务结果' : '恢复原任务'}</button>{taskState === 'needs_reconcile' && currentTask?.payload?.kind !== 'speech' && <button className="button secondary compact" onClick={() => void resolveMissingExecution(taskId)}>确认执行已结束</button>}</div>}<div className="director-inspector-tabs" role="tablist" aria-label="片段档案栏目">{[['intent', '导演意图'], ['materials', '画面与音频'], ['params', '生成参数'], ['history', '历史版本']].map(([id, label]) => <button key={id} role="tab" aria-selected={inspectorTab === id} className={inspectorTab === id ? 'active' : ''} onClick={() => setInspectorTab(id as typeof inspectorTab)}>{label}</button>)}</div>{inspectorTab === 'intent' && <div className="director-inspector-body">{currentPlanSegment?.visual_review?.reason && <section><h3>持久化视觉审核</h3><p className="director-intent">{currentPlanSegment.visual_review.reason}</p></section>}<section><h3>导演意图</h3><p className="director-intent">{current.intent || '暂无导演意图。'}</p></section><section><label htmlFor={`director-prompt-${current.id}`}>提示词</label><textarea id={`director-prompt-${current.id}`} rows={6} value={currentPrompt} placeholder="描述画面、动作、表情和镜头意图。" onChange={event => { updateDraft(current.id, { prompt: event.target.value }); updatePipelineValue('prompt', event.target.value); }} /></section><section><label htmlFor={`director-note-${current.id}`}>审核意见</label><textarea id={`director-note-${current.id}`} rows={4} value={currentNote} placeholder="记录为什么保留、采用或需要重做。" onChange={event => updateDraft(current.id, { note: event.target.value })} /></section><div className="director-intent-meta"><span>{currentPlanSegment?.shot_size ?? '素材类型'}</span><span>{currentPlanSegment?.camera ?? current.specs}</span><span>{currentPlanSegment?.wardrobe ?? '版本档案'}</span></div></div>}{inspectorTab === 'materials' && <div className="director-inspector-body"><section><h3>首尾帧</h3><div className="director-frame-pair">{current.image ? <figure><img src={current.image} alt={`${current.id} 首帧`} /><figcaption>首帧参考</figcaption></figure> : <div className="director-frame-empty">暂无首帧</div>}{current.lastImage ? <figure><img src={current.lastImage} alt={`${current.id} 尾帧`} /><figcaption>尾帧目标</figcaption></figure> : <div className="director-frame-empty">暂无尾帧</div>}</div></section><section><h3>音频素材</h3>{currentAudioSource ? <audio src={currentAudioSource} controls preload="metadata" /> : <p className="director-muted">当前片段没有独立音频文件，使用项目完整母版时间轴。</p>}</section><section><h3>工作流与来源</h3><p className="director-mono">{current.workflow || '尚未登记'}</p>{workflowEntriesForAsset(current).length > 0 && <div className="director-workflow-links">{workflowEntriesForAsset(current).map(entry => <a key={`${entry.id}-${entry.path}`} href={workflowDownloadUrl(entry.path)} download={entry.path.split(/[\\/]/).pop()}><ArrowDownToLine size={13} />{entry.comfyui_import === 'ui' ? 'UI 工作流' : 'API 配方'}</a>)}</div>}</section></div>}{inspectorTab === 'params' && <div className="director-inspector-body"><section><h3>当前规格</h3><dl className="director-fact-list"><div><dt>时长</dt><dd>{range(current)}</dd></div><div><dt>规格</dt><dd>{current.specs || '尚未登记'}</dd></div><div><dt>来源</dt><dd>{current.origin || '尚未登记'}</dd></div></dl></section><section><h3>执行入口</h3><p className="director-muted">节点、脚本和模型仍由 ComfyUI 执行，工作台只保存本段的参数档案和任务回执。</p><button className="button secondary pipeline-drawer-button" onClick={() => setPipelineOpen(true)}><Workflow size={14} />打开当前片段 Pipeline</button></section></div>}{inspectorTab === 'history' && <div className="director-inspector-body"><section><h3>历史版本</h3><div className="director-history-row"><span className="history-version active">A</span><div><strong>{candidateA ? '当前候选已存在' : 'A 版本待生成'}</strong><small>{current.origin || '尚未登记来源'}</small></div></div><div className="director-history-row"><span className="history-version">B</span><div><strong>{candidateB ? currentCandidateBIsRework ? '重做样片 · 待审核' : `精细化候选 · ${currentFinishReview}` : '尚未生成 B 版本'}</strong><small>{candidateB ? currentCandidateBIsRework ? '与原样片同规格，只判断内容是否可用' : '样片采用后才能采用精细版' : '重做时可保存为新版本'}</small></div></div></section><section><h3>持久化审核</h3><p className="director-muted">{currentSampleRecord ? `${currentReview} · revision ${currentSampleRecord.revision} · ${currentSampleRecord.source}` : '尚无审核 revision；请使用下方审核与版本档案提交决定。'}</p></section><section><h3>任务尝试</h3>{currentAttempts.length ? currentAttempts.slice(0, 4).map(task => <div className="director-history-row" key={task.id}><span className="history-version">{taskStateLabel(task.status).slice(0, 2)}</span><div><strong>{taskStateLabel(task.status)}</strong><small>{task.id}</small></div></div>) : <p className="director-mono">尚无当前片段任务</p>}</section></div>}</aside>
          </details>
        </section>

        </>}
        {workspacePage !== 'creative' && (workspacePage === 'delivery' || advancedOpen) && <section className="director-operations" aria-label="当前片段生产控制">
          <div className="director-operations-head"><div><span className="director-kicker">当前片段控制</span><h2>{workspacePage === 'delivery' ? '批次与交付' : `${current.id} · 分镜详情`}</h2></div><span>{currentExecutable ? '执行入口已登记' : isAssemblyAsset ? '完整版仅使用装配入口' : '请先在「制作步骤」登记制作方案'}</span></div>
          <div className="director-operations-grid">
            {currentPlanSegment && <ReferenceOrigin segment={currentPlanSegment} onOpen={analysisId => { if (project) setReferenceTarget({ projectId: project.id, analysisId }); setWorkspaceMode('reverse'); }} />}
            {currentPlanSegment && project && <CreativeInspectionPanel key={`${project.id}:${currentPlanSegment.id}`} projectId={project.id} segmentId={currentPlanSegment.id} revision={planRevision} onPlanChanged={() => { void refreshPlan(); }} />}
            {currentPlanSegment && <details className="director-operation-panel"><summary className="director-panel-title"><div><h3>分镜计划</h3><small>修改后，相关阶段需要重新确认</small></div><span>{currentPlanSegment.dependencies?.length ? `依赖 ${currentPlanSegment.dependencies.join(', ')}` : '无上游依赖'}</span></summary><div className="director-segment-edit-grid"><label>时长（秒）<input type="number" min="0.1" step="0.01" value={segmentEdit.duration_seconds} onChange={event => setSegmentEdit(previous => ({ ...previous, duration_seconds: event.target.value }))} /></label><label>场景<input value={segmentEdit.location} onChange={event => setSegmentEdit(previous => ({ ...previous, location: event.target.value }))} /></label><label>景别<input value={segmentEdit.shot_size} onChange={event => setSegmentEdit(previous => ({ ...previous, shot_size: event.target.value }))} /></label><label>镜头<input value={segmentEdit.camera} onChange={event => setSegmentEdit(previous => ({ ...previous, camera: event.target.value }))} /></label><label>造型<input value={segmentEdit.wardrobe} onChange={event => setSegmentEdit(previous => ({ ...previous, wardrobe: event.target.value }))} /></label><label className="wide">动作表演<textarea rows={2} value={segmentEdit.performance} onChange={event => setSegmentEdit(previous => ({ ...previous, performance: event.target.value }))} /></label><label className="wide">提示词<textarea rows={4} value={currentPrompt} onChange={event => { updateDraft(current.id, { prompt: event.target.value }); setSegmentEdit(previous => ({ ...previous, prompt: event.target.value })); }} /></label><label className="wide">工作流引用<input value={segmentEdit.workflow} placeholder="workflows/...json" onChange={event => setSegmentEdit(previous => ({ ...previous, workflow: event.target.value }))} /></label><MaterialSelect excludedValues={diagnosticPaths} label="首帧" kind="image" value={segmentEdit.first_frame} options={materialOptions} onChange={value => setSegmentEdit(previous => ({ ...previous, first_frame: value }))} onBrowse={() => setMaterialBridgeOpen(true)} /><MaterialSelect excludedValues={diagnosticPaths} label="尾帧" kind="image" value={segmentEdit.last_frame} options={materialOptions} onChange={value => setSegmentEdit(previous => ({ ...previous, last_frame: value }))} onBrowse={() => setMaterialBridgeOpen(true)} /><MaterialSelect excludedValues={diagnosticPaths} label="表演引导音频" kind="audio" value={segmentEdit.audio_guide} options={materialOptions} onChange={value => setSegmentEdit(previous => ({ ...previous, audio_guide: value }))} onBrowse={() => setMaterialBridgeOpen(true)} /><MaterialSelect excludedValues={diagnosticPaths} label="交付音频" kind="audio" value={segmentEdit.delivery_master} options={materialOptions} onChange={value => setSegmentEdit(previous => ({ ...previous, delivery_master: value }))} onBrowse={() => setMaterialBridgeOpen(true)} /><label className="wide">依赖片段<input value={segmentEdit.dependencies} placeholder="例如 S01, S02" onChange={event => setSegmentEdit(previous => ({ ...previous, dependencies: event.target.value }))} /></label></div><div className="director-panel-actions"><button className="button primary compact" onClick={() => void saveCurrentSegment()}>保存分镜 revision</button><button className="button secondary compact" onClick={() => void operateCurrentSegment('copy')}>复制</button><button className="button secondary compact" disabled={index <= 0} onClick={() => void operateCurrentSegment('move', { before_id: planSegments[index - 1]?.id })}>前移</button><button className="button secondary compact" disabled={index < 0 || index >= planSegments.length - 1} onClick={() => void operateCurrentSegment('move', { before_id: planSegments[index + 2]?.id ?? '__end__' })}>后移</button><button className="button secondary compact" onClick={() => void operateCurrentSegment('split', { split_at_seconds: currentPlanSegment.duration_seconds / 2 })}>中点拆分</button><button className="button secondary compact" disabled={index >= planSegments.length - 1} onClick={() => void operateCurrentSegment('merge', { target_id: planSegments[index + 1]?.id })}>合并下一段</button></div>{index >= 0 && index < planSegments.length - 1 && <div className="director-panel-actions"><label>连续合并至<select aria-label="连续合并至" value={mergeEnd} onChange={event => setMergeEnd(event.target.value)}><option value="">选择末段</option>{planSegments.slice(index + 1).map(segment => <option key={segment.id} value={segment.id}>{segment.id} · {segment.location}</option>)}</select></label><button className="button secondary compact" disabled={!mergeEnd || !planSegments.slice(index + 1).some(segment => segment.id === mergeEnd)} onClick={() => { if (window.confirm(`将 ${currentPlanSegment.id} 至 ${mergeEnd} 的连续分镜合并。已有候选将失效，来源历史会保留。确认合并？`)) void operateCurrentSegment('merge_range', { target_id: mergeEnd }); }}>合并所选范围</button><small>已有候选会失效，保留来源历史。</small></div>}</details>}

            {currentPlanSegment && <button type="button" className="director-shot-delete" onClick={() => setDeleteShotOpen(true)}>删除当前分镜</button>}
            {reviewConflictPanel}
            <details className="director-operation-panel" hidden><summary className="director-panel-title"><div><h3>审核与版本档案</h3><small>保存审核结论，保留历史版本</small></div><span>{currentSampleRecord ? `${kind === 'video' ? '样片' : '素材候选'} revision ${currentSampleRecord.revision}` : kind === 'video' ? '尚无样片审核档案' : '尚无素材审核档案'}</span></summary><label className="director-review-note">审核意见<textarea rows={3} value={currentNote} placeholder="记录画面、声音、动作或节奏问题" onChange={event => updateDraft(current.id, { note: event.target.value })} /></label><div className="director-panel-actions"><button className="button secondary compact" disabled={stateSaving || !current.ready} onClick={() => void persistReview(isAssemblyAsset ? 'final' : 'sample', 'pending_review')}>保存意见</button><button className="button secondary compact" disabled={stateSaving || !current.ready} onClick={() => void persistReview(isAssemblyAsset ? 'final' : 'sample', 'changes_requested')}>退回修改</button><button className="button adopt compact" disabled={stateSaving || !current.ready || (isAssemblyAsset && assemblyIsStale)} onClick={() => void adoptCurrent()}><Check size={13} />{isAssemblyAsset ? '通过全片审核' : '采用当前候选'}</button></div><div className="director-review-facts"><span>当前采用：{currentSampleRecord?.adopted_variant ?? '无'}</span><span>精细版：{currentFinishRecord ? `${currentFinishReview} · r${currentFinishRecord.revision}` : '无档案'}</span><span>历史尝试：{currentAttempts.length}</span></div></details>

            {(pipelineOpen || inspectorTab === 'params') && activePipelineStage && <article className="director-operation-panel director-pipeline-controls">
              <div className="director-panel-title"><div><h3>{activePipelineStage.title} · 实际输入</h3><small>{activePipelineStage.execution?.label ?? activePipelineStage.backend}</small></div><button className="button secondary compact" onClick={() => void validatePipelineStage(activePipelineStage)} disabled={pipelineChecking}>{pipelineChecking ? '预检中…' : '预检参数'}</button></div>
              <div className="director-segment-edit-grid">{(activePipelineStage.inputSpecs ?? []).map(input => {
                const value = pipelineValues[input.id] ?? input.example ?? (input.control === 'multi-select' ? [] : '');
                const values = Array.isArray(value) ? value : [];
                return <label key={input.id} className={input.control === 'text' || input.control === 'freeform' ? 'wide' : ''}><span>{input.label}{input.required ? ' *' : ''}</span>{(input.control === 'select' || input.control === 'boolean') && <select value={String(value)} onChange={event => updatePipelineValue(input.id, event.target.value)}>{(input.options ?? (input.control === 'boolean' ? ['enable', 'disable'] : [])).map(option => <option key={option}>{option}</option>)}</select>}{input.control === 'number' && <input type="number" min={input.min} max={input.max} step={input.step} value={String(value)} onChange={event => updatePipelineValue(input.id, event.target.value)} />}{input.control === 'asset' && <select value={String(value)} onChange={event => updatePipelineValue(input.id, event.target.value)}><option value="">选择已登记{input.kind}</option>{pipelineAssetOptions(input).map(option => <option key={option.value} value={option.value}>{option.label}</option>)}</select>}{input.control === 'multi-select' && <span className="director-check-list">{(input.options ?? []).map(option => <label key={option}><input type="checkbox" checked={values.includes(option)} onChange={event => updatePipelineValue(input.id, event.target.checked ? [...values, option] : values.filter(item => item !== option))} />{option}</label>)}</span>}{(!input.control || input.control === 'text' || input.control === 'freeform' || input.control === 'time-range') && <textarea rows={2} value={String(value)} onChange={event => updatePipelineValue(input.id, event.target.value)} />}</label>;
              })}</div>
              {pipelineValidation?.stage_id === activePipelineStage.id && <div className="director-validation"><strong>预检通过 · {pipelineValidation.binding_count ?? 0} 项会写入 API 图</strong><code>{pipelineValidation.args.join(' ') || 'ComfyUI 节点绑定已确认'}</code></div>}
            </article>}

            <article className="director-operation-panel director-delivery-panel"><div className="director-panel-title"><div><h3>批次与装配</h3><small>停止保留已完成片段；恢复从原冻结快照的新尝试开始</small></div><span>{batchTasks.length ? `${batchTasks.filter(task => task.status === 'succeeded').length}/${batchTasks.length}` : '未运行批次'}</span></div><div className="director-panel-actions"><button className="button secondary compact" disabled={batchActive} onClick={startBatch}><Play size={13} />启动可执行片段批次</button>{batchActive && <button className="button secondary compact" onClick={stopBatch}><Square size={13} />停止批次</button>}{batchId && !batchActive && batchTasks.some(task => task.status !== 'succeeded') && <button className="button secondary compact" onClick={resumeBatch}><Play size={13} />恢复批次</button>}<button className="button primary compact" disabled={assemblySubmitting || (!hasPendingAssembly && (!assemblyReady || activeTaskStates.includes(assemblyTaskState)))} onClick={() => void startAssembly()}><Layers3 size={13} />{hasPendingAssembly ? '沿用装配凭据重试' : assemblyTaskState === 'succeeded' ? '重新装配' : '启动装配'}</button>{hasPendingAssembly && <button className="button secondary compact" disabled={assemblySubmitting} onClick={() => void startAssembly(true)}>核对装配提交</button>}{activeTaskStates.includes(assemblyTaskState) && <button className="button secondary compact" onClick={stopAssembly}><Square size={13} />停止装配</button>}{!hasPendingAssembly && ['stopped', 'failed', 'needs_reconcile'].includes(assemblyTaskState) && <button className="button secondary compact" onClick={resumeAssembly}><Play size={13} />{assemblyTaskState === 'needs_reconcile' ? '核对装配结果' : '恢复装配'}</button>}</div><div className="director-review-facts"><span>已有视频 {planSegments.filter(segment => Boolean(segment.video?.path)).length}/{planSegments.length}</span><span>装配使用各分镜的当前版本</span><span>装配任务：{assemblyIsStale ? '历史成片 · 需重新装配' : assemblyTaskState ? taskStateLabel(assemblyTaskState) : '无'}</span></div></article>

            <details className="director-operation-panel director-execution-record"><summary className="director-panel-title"><div><h3>本次实际执行档案</h3><small>查看实际使用的参数与执行记录</small></div><span>{currentTask ? `${currentTask.status} · ${currentTask.id.slice(0, 8)}` : '尚无任务'}</span></summary>{currentTask?.payload?.execution_snapshot ? <dl><div><dt>实际步骤</dt><dd>{currentTask.payload.execution_snapshot.pipeline_stage_id || '旧任务 · 未登记步骤'} · 已写入 {currentTask.payload.execution_snapshot.applied_bindings?.length ?? 0} 项节点输入</dd></div><div><dt>实际提示词</dt><dd>{currentTask.payload.execution_snapshot.prompt || '工作流未声明提示词覆盖'}</dd></div><div><dt>API 图</dt><dd><a href={workflowDownloadUrl(currentTask.payload.execution_snapshot.api_graph ?? '')}>{currentTask.payload.execution_snapshot.api_graph}</a></dd></div><div><dt>来源图</dt><dd>{currentTask.payload.execution_snapshot.source_workflow}</dd></div><div><dt>参数</dt><dd><code>{JSON.stringify(currentTask.payload.execution_snapshot.parameters ?? {})}</code></dd></div><div><dt>素材</dt><dd><code>{JSON.stringify(currentTask.payload.execution_snapshot.materials ?? {})}</code></dd></div></dl> : <p className="director-muted">提交后，这里会显示真正执行的提示词、素材、参数和冻结 API 图。</p>}{currentTask?.error && <p className="director-error">{currentTask.error}</p>}</details>
          </div>
        </section>}
        </>}
      </div>
      {workspaceMode === 'production' && workspacePage === 'shots' && advancedOpen && pipelineOpen && <aside className="director-pipeline-drawer" aria-label="高级制作参数"><div className="director-drawer-head"><div><span className="director-kicker">当前分镜</span><h2>高级制作参数</h2><p>仅在需要调试执行方案时使用。</p></div><button className="icon-button" aria-label="关闭高级参数" onClick={() => setPipelineOpen(false)}><X size={17} /></button></div><div className="director-pipeline-list">{currentPipeline.map(stage => <button key={stage.id} className={`director-pipeline-step ${stage.id === activePipelineStage?.id ? 'active' : ''}`} onClick={() => setPipelineStageId(stage.id)}><span>{String(stage.order).padStart(2, '0')}</span><div><strong>{stage.title}</strong><small>{stage.purpose}</small></div><em>{stage.status}</em></button>)}</div></aside>}
    </main>
      {materialBridgeOpen && <MaterialBridge projectId={project?.id ?? ''} initialMode={materialBridgeDevice ? 'device' : 'server'} onClose={() => { setMaterialBridgeOpen(false); setMaterialBridgeDevice(false); }} onRegistered={count => { void refreshRegisteredMaterials(count); }} />}
      {creativeConflictPanel}
      {batchId && <div className="director-batch-note" role="note">批次参数语义：按每个分镜自己的计划与源工作流冻结；当前片段 Inspector 的修改只影响单段重做。需要批量改参时，请先逐段确认后再提交。</div>}
      <ConnectAgent />
      {privateMode && <CreationAssistant projectId={project?.id} onPresentation={presentAssistantEvent} onResult={result => void openAssistantResult(result)} />}
      {privateMode && <>{agentStep && <div className="director-readiness" role="status" aria-label="Agent 页面进度">{agentStep}</div>}<AgentPageBridge onAction={presentAgentAction} /></>}
      {notice && <div className="director-toast" role="status"><Check size={16} /><span>{notice}</span></div>}
      {deleteDirectory && <DeleteDirectoryDialog target={deleteDirectory} onClose={() => setDeleteDirectory(null)} onDeleted={() => { void afterDirectoryDeleted(); }} />}
      {deleteShotOpen && currentPlanSegment && <div className="director-delete-backdrop" role="presentation" onMouseDown={event => { if (event.target === event.currentTarget) setDeleteShotOpen(false); }}><section className="director-delete-dialog" role="dialog" aria-modal="true" aria-label="删除分镜"><h2>删除分镜 {currentPlanSegment.id}？</h2><p>会从本作品的制作计划移除这一段，并重排后续时间轴。生成记录与可能被其他分镜共用的素材仍保留。</p><div className="director-delete-actions"><button type="button" className="button secondary compact" onClick={() => setDeleteShotOpen(false)}>取消</button><button type="button" className="button danger compact" disabled={deletingShot} onClick={() => void deleteCurrentShot()}>{deletingShot ? '删除中…' : '删除分镜'}</button></div></section></div>}
    <dialog ref={dialogRef} className="lightbox" onCancel={() => setLightbox(false)}><div><span>{current.id} · {current.title}</span><button className="icon-button" onClick={() => setLightbox(false)} aria-label="关闭大图"><X size={22} /></button></div>{lightbox && <img src={current.image} alt={current.title} />}</dialog>
  </div>;
  /* Legacy multi-page render retained temporarily while the single-screen
     director workspace is validated. */
  /*
  return <div className="workbench">
    <aside className="project-sidebar">
      <div className="brand"><span className="brand-mark"><Clapperboard size={21} /></span><div>片场<span>DIRECTOR WORKBENCH</span></div></div>
      <div className="sidebar-label">工作空间 <span>LOCAL</span></div>
      <div className="series-label"><Layers3 size={16} /> {project?.series ?? '工作空间'}</div>
      <button className="project-active" onClick={() => setProjectMenuOpen(open => !open)}><span className="project-dot" />{project?.title ?? '正在读取项目'}<ChevronRight size={14} /></button>
      <div className="project-note">{project?.tag ?? '项目清单驱动'}</div>
      {projectMenuOpen && <div className="project-switcher"><div className="project-switcher-actions"><button className="button secondary compact" onClick={() => { void refreshProjects(); setProjectMenuOpen(true); }}>刷新项目</button><button className="button primary compact" onClick={() => setCreateOpen(open => !open)}>新建空白作品</button></div>{projectList.map(item => <button key={item.id} className={`project-switcher-item ${item.selected ? 'selected' : ''}`} onClick={() => void selectProject(item.id)}>{item.title}<small>{item.series ?? item.id}</small></button>)}{createOpen && <div className="project-create-form"><label>系列名称<input value={newSeries} onChange={event => setNewSeries(event.target.value)} placeholder="例如：新系列" /></label><label>作品名称<input value={newTitle} onChange={event => setNewTitle(event.target.value)} placeholder="例如：第一集" /></label><label>项目 ID（可选）<input value={newProjectId} onChange={event => setNewProjectId(event.target.value)} placeholder="自动生成" /></label><button className="button primary compact" onClick={() => void createProject()}>创建并进入工作台</button></div>}<div className="project-import-form"><strong>导入现有工作目录</strong><small>点击“选择工作目录”会唤起系统文件夹选择框；选定后保留原有相对目录。</small><input ref={folderInputRef} className="visually-hidden-file" type="file" multiple {...({ webkitdirectory: '' } as unknown as InputHTMLAttributes<HTMLInputElement>)} onChange={event => setImportFiles(Array.from(event.target.files ?? []))} /><button className="button secondary compact file-picker-button" onClick={() => folderInputRef.current?.click()}><ArrowUpRight size={14} />选择工作目录</button><span>{importFiles.length ? `已选择 ${importFiles.length} 个文件` : '尚未选择文件夹'}</span><button className="button primary compact" disabled={!importFiles.length} onClick={() => void uploadSelectedFiles()}>上传并解析资源</button><label>本机高级入口<input value={importPath} onChange={event => setImportPath(event.target.value)} placeholder="D:\\Comfy-Desktop\\..." /></label><button className="button secondary compact" onClick={() => void importWorkspace()}>按路径解析</button></div><button className="button secondary compact" onClick={() => void exportProjectBundle()}>导出当前项目清单</button>{importReport && <small>最近导入：{String((importReport.counts as Record<string, unknown> | undefined)?.video ?? 0)} 视频 · {String((importReport.counts as Record<string, unknown> | undefined)?.audio ?? 0)} 音频 · {String((importReport.counts as Record<string, unknown> | undefined)?.image ?? 0)} 图片</small>}</div>}
      <div className="sidebar-divider" />
      <div className="sidebar-label">工作台</div>
      <nav className="page-nav" aria-label="工作台页面">
        <button className={`page-nav-item ${page === 'overview' ? 'active' : ''}`} onClick={() => setPage('overview')}><LayoutDashboard size={16} />总览</button>
        <button className={`page-nav-item ${page === 'pipeline' ? 'active' : ''}`} onClick={() => setPage('pipeline')}><Workflow size={16} />制作流程</button>
        <button className={`page-nav-item ${page === 'review' ? 'active' : ''}`} onClick={() => setPage('review')}><ClipboardCheck size={16} />片段审核</button>
      </nav>
      <div className="sidebar-divider" />
      <div className="sidebar-label">作品内容</div>
      {(['video', 'audio', 'image'] as Kind[]).map(type => {
        const Icon = kindIcons[type];
        const count = type === 'video' ? planVideoAssets.length : library.filter(asset => asset.kind === type).length;
        return <button key={type} className={`side-item ${kind === type ? 'active' : ''}`} onClick={() => switchKind(type)}><Icon size={17} />{type === 'video' ? '表演分镜' : type === 'audio' ? '声音与配乐' : '造型与关键帧'}<span>{String(count).padStart(2, '0')}</span></button>;
      })}
      <div className="sidebar-bottom"><div className="connection"><span className={comfyOnline ? 'online' : ''} /> {comfyOnline ? `ComfyUI 已连接${comfyInfo.version ? ` · ${comfyInfo.version}` : ''}` : 'ComfyUI 尚未接入'}</div><p>工作台整理与展示<br />ComfyUI 执行生成</p><button className="text-button" onClick={() => setResourceOpen(!resourceOpen)}><Radio size={15} /> 查看连接与资源</button></div>
    </aside>

    <main data-page={page}>
      <header className="topbar"><div>{project?.series ?? '工作空间'} <ChevronRight size={13} /><span>{project?.title ?? '项目读取中'}</span></div><span className="prototype-label">项目配置驱动 · ComfyUI 封装层</span><button className="icon-button" aria-label="连接与资源状态" onClick={() => setResourceOpen(!resourceOpen)}><Settings2 size={18} /></button></header>
      <nav className="mobile-page-nav" aria-label="工作台页面"><button className={page === 'overview' ? 'active' : ''} onClick={() => setPage('overview')}>总览</button><button className={page === 'pipeline' ? 'active' : ''} onClick={() => setPage('pipeline')}>制作流程</button><button className={page === 'review' ? 'active' : ''} onClick={() => setPage('review')}>片段审核</button></nav>
      {resourceOpen && <section className="resource-panel"><div><Radio size={18} /><strong>连接与资源</strong></div><p>{comfyOnline ? `已连接 ComfyUI ${comfyInfo.version ?? ''} · 显存空闲约 ${comfyInfo.gpu_free_mib ?? '?'} MiB · 队列运行 ${comfyInfo.queue_running ?? 0} · 等待 ${comfyInfo.queue_pending ?? 0}。` : '尚未连接 ComfyUI，未读取实时显存、内存或队列数据。本页面不会加载模型。连续播放只读取已有素材；有效任务可先排队，服务器会在所需资源恢复后执行。'}</p><button className="icon-button" aria-label="关闭资源面板" onClick={() => setResourceOpen(false)}><X size={17} /></button></section>}
        {pipeline.find(stage => stage.id === pipelineStageId)?.inputSpecs?.length ? (() => { const stage = pipeline.find(item => item.id === pipelineStageId)!; return <section className="pipeline-inputs" aria-label={`${stage.title}输入控件`}><div className="pipeline-inputs-head"><div><span className="eyebrow">可填写参数 · {stage.title}</span><p>控件由步骤契约声明：固定选项不会变成自由文本，数值参数保留可编辑范围。</p></div><span className="pipeline-inputs-source">{stage.execution?.label ?? stage.backend}</span></div><div className="pipeline-input-grid">{stage.inputSpecs!.map(input => { const assetKind = assetKindForInput(input.kind); const value = pipelineValues[input.id] ?? input.example ?? (input.control === 'multi-select' ? [] : ''); const values = Array.isArray(value) ? value : []; return <label className="pipeline-input" key={input.id}><span>{input.label}{input.required && <small>必需</small>}</span>{(input.control === 'select' || input.control === 'boolean') && <select value={String(value)} onChange={event => updatePipelineValue(input.id, event.target.value)}>{(input.options ?? (input.control === 'boolean' ? ['enable', 'disable'] : [])).map(option => <option key={option} value={option}>{option}</option>)}</select>}{input.control === 'multi-select' && <div className="pipeline-multi-options">{(input.options ?? []).map(option => <span className="pipeline-multi-option" key={option}><input type="checkbox" checked={values.includes(option)} aria-label={option} onChange={event => updatePipelineValue(input.id, event.target.checked ? [...values, option] : values.filter(selected => selected !== option))} /><span>{option}</span></span>)}</div>}{input.control === 'number' && <input type="number" value={String(value)} min={input.min} max={input.max} step={input.step} onChange={event => updatePipelineValue(input.id, event.target.value)} />}{input.control === 'asset' && assetKind && <select value={String(value)} onChange={event => updatePipelineValue(input.id, event.target.value)}><option value="">选择已登记{input.kind}…</option>{baseAssets.filter(asset => asset.kind === assetKind).map(asset => <option key={asset.id} value={asset.id}>{asset.id} · {asset.title}</option>)}</select>}{(input.control === 'text' || input.control === 'freeform') && <textarea rows={2} value={String(value)} placeholder={input.example ?? (input.allowEmpty ? '可留空' : '输入本步骤需要的文本')} onChange={event => updatePipelineValue(input.id, event.target.value)} />}{input.control === 'time-range' && <input type="text" value={String(value)} placeholder={input.example ?? '00:00.000–00:00.000'} onChange={event => updatePipelineValue(input.id, event.target.value)} />}<em>{input.flag ? `${input.flag} · ` : ''}{input.kind} · {input.description}{input.control === 'multi-select' ? ' 可多选' : ''}</em></label>; })}</div></section>; })() : null}
       {pipeline.find(stage => stage.id === pipelineStageId) && (() => { const stage = pipeline.find(item => item.id === pipelineStageId)!; const validation = pipelineValidation?.stage_id === stage.id ? pipelineValidation : null; return <section className="pipeline-command-preview" aria-label={`${stage.title}参数预检`}><div><span className="eyebrow">执行前预检</span><p>先按 JSON 契约检查必填项、单选范围、多选范围和数值范围；通过后才会生成传给脚本或适配器的参数数组。</p></div><button className="button secondary compact" onClick={() => validatePipelineStage(stage)} disabled={pipelineChecking}>{pipelineChecking ? '检查中…' : '检查参数'}</button>{validation && <div className="pipeline-validation-result"><span className="pipeline-validation-label">参数数组</span><code>{validation.args.length ? validation.args.join('  ') : '（没有可选参数）'}</code><details><summary>查看 JSON 归一化结果</summary><pre>{JSON.stringify(validation.values, null, 2)}</pre></details></div>}</section>; })()}
          <div className="content">
           {page === 'pipeline' && <section className="page-intro" aria-label="制作流程页面说明"><div><span className="eyebrow">工作台 / 制作流程</span><h1>制作流程</h1><p>先确认每一步的输入和输出，再打开具体步骤填写参数。节点与脚本仍由 ComfyUI 和本地适配器执行。</p></div><div className="page-intro-actions"><span>{pipeline.length} 个步骤</span><button className="button secondary" onClick={() => setStageFormOpen(open => !open)}><Plus size={15} />新增步骤</button></div></section>}
           {page === 'review' && <section className="page-intro" aria-label="片段审核页面说明"><div><span className="eyebrow">工作台 / 片段审核</span><h1>片段审核</h1><p>按视频、音频和图片分别查看结果。每个片段都保留提示词、首尾帧、工作流和审核意见。</p></div><div className="page-intro-actions"><span>{library.length} 个素材</span><button className="button secondary" onClick={() => setPage('overview')}><LayoutDashboard size={15} />回到总览</button></div></section>}
           {page === 'segment' && <section className="segment-detail-page" aria-label="片段详情"><div className="segment-detail-head"><div><span className="eyebrow">片段详情 · 审核页面</span><h2>{current.title}</h2><p>{current.intent || '这段素材还没有填写表演意图。'}</p></div><button className="button secondary" onClick={() => setPage('review')}><ChevronLeft size={15} />返回片段审核</button></div><div className="segment-detail-meta"><div><span>时间范围</span><strong>{range(current)}</strong></div><div><span>规格</span><strong>{current.specs || '尚未登记'}</strong></div><div><span>生成来源</span><strong>{current.origin || '尚未登记'}</strong></div></div><div className="segment-detail-columns"><div><label htmlFor={`segment-prompt-${current.id}`}>提示词草稿</label><textarea id={`segment-prompt-${current.id}`} rows={8} value={drafts[current.id]?.prompt ?? current.prompt} placeholder="记录这段画面、动作、表情和镜头意图。" onChange={e => updateDraft(current.id, { prompt: e.target.value })} /></div><div><label htmlFor={`segment-note-${current.id}`}>审核意见</label><textarea id={`segment-note-${current.id}`} rows={8} value={drafts[current.id]?.note ?? ''} placeholder="记录需要重做或保留的地方。" onChange={e => updateDraft(current.id, { note: e.target.value })} /><div className="segment-detail-source"><span>工作流与历史参数</span><p>{current.workflow || '尚未登记'}</p></div></div></div>{(current.image || current.lastImage) && <div className="segment-detail-frames">{current.image && <button onClick={() => { setKind('image'); setPage('review'); }}><img src={current.image} alt={`${current.title}首帧`} /><span>首帧参考</span></button>}{current.lastImage && <button onClick={() => { setKind('image'); setPage('review'); }}><img src={current.lastImage} alt={`${current.title}尾帧`} /><span>尾帧目标</span></button>}</div>}<div className="workflow-downloads segment-detail-workflows"><span>ComfyUI 工作流</span>{workflowEntriesForAsset(current).length ? workflowEntriesForAsset(current).map(entry => <a className="button secondary compact" key={`${entry.id}-${entry.path}`} href={workflowDownloadUrl(entry.path)} download={entry.path.split(/[\\/]/).pop()} title={entry.equivalence ?? (entry.comfyui_import === 'ui' ? '可导入 ComfyUI 界面查看' : '可提交到 ComfyUI API')}><ArrowDownToLine size={13} />{entry.comfyui_import === 'ui' ? '下载 UI 图' : '下载 API 图'}</a>) : <small>项目尚未登记可下载的 JSON 工作流</small>}</div></section>}
          <section className="creation-hub" aria-label="项目创作入口"><div><span className="eyebrow">从这里开始</span><h2>把想法登记成可执行项目</h2><p>先导入素材，再登记制作步骤和分段计划。真正的模型加载、渲染和输出仍由 ComfyUI 完成。</p></div><div className="creation-hub-actions"><button className="button secondary compact" onClick={() => setStageFormOpen(open => !open)}><Plus size={14} />制作步骤</button><button className="button secondary compact" onClick={() => setSegmentFormOpen(open => !open)}><Plus size={14} />分段计划</button></div></section>
          {stageFormOpen && <section className="creation-panel" aria-label="登记制作步骤"><div className="creation-panel-heading"><div><span className="eyebrow">新建步骤</span><p>先登记这一步让审核者准备什么、会产出什么；执行引用可稍后补齐。</p></div><button className="icon-button" aria-label="关闭新建步骤" onClick={() => setStageFormOpen(false)}><X size={16} /></button></div><div className="creation-grid"><label>步骤名称<input value={stageTitle} onChange={event => setStageTitle(event.target.value)} placeholder="例如：人声混音" /></label><label>执行方式<select value={stageBackend} onChange={event => setStageBackend(event.target.value)}><option>混合流程</option><option>ComfyUI 工作流</option><option>本地脚本</option></select></label><label className="creation-wide">用途说明<input value={stagePurpose} onChange={event => setStagePurpose(event.target.value)} placeholder="审核者能理解的这一步目的" /></label><label>输入（逗号分隔）<input value={stageInputs} onChange={event => setStageInputs(event.target.value)} placeholder="首帧, 引导音频, 提示词" /></label><label>输出（逗号分隔）<input value={stageOutputs} onChange={event => setStageOutputs(event.target.value)} placeholder="候选视频" /></label><label className="creation-wide">工作流或脚本引用（可选）<input value={stageWorkflow} onChange={event => setStageWorkflow(event.target.value)} placeholder="例如：workflows/api/motion.json 或 tools/render.py" /></label></div><div className="creation-actions"><span>步骤登记后，可在卡片中继续填写参数契约。</span><button className="button primary compact" onClick={() => void createPipelineStage()}>保存步骤</button></div></section>}
          {segmentFormOpen && <section className="creation-panel" aria-label="登记分段计划"><div className="creation-panel-heading"><div><span className="eyebrow">新建分段</span><p>用审核者看得懂的字段描述镜头；首尾帧、提示词和工作流可以后补。</p></div><button className="icon-button" aria-label="关闭新建分段" onClick={() => setSegmentFormOpen(false)}><X size={16} /></button></div><div className="creation-grid"><label>片段编号（可选）<input value={segmentDraft.segment_id} onChange={event => setSegmentDraft(previous => ({ ...previous, segment_id: event.target.value }))} placeholder="留空则自动编号" /></label><label>时长（秒）<input type="number" min="0.1" step="0.1" value={segmentDraft.duration_seconds} onChange={event => setSegmentDraft(previous => ({ ...previous, duration_seconds: event.target.value }))} /></label><label>场景<input value={segmentDraft.location} onChange={event => setSegmentDraft(previous => ({ ...previous, location: event.target.value }))} placeholder="例如：阳光庭院" /></label><label>景别<input value={segmentDraft.shot_size} onChange={event => setSegmentDraft(previous => ({ ...previous, shot_size: event.target.value }))} /></label><label>镜头运动<input value={segmentDraft.camera} onChange={event => setSegmentDraft(previous => ({ ...previous, camera: event.target.value }))} /></label><label>服装 / 造型<input value={segmentDraft.wardrobe} onChange={event => setSegmentDraft(previous => ({ ...previous, wardrobe: event.target.value }))} /></label><label className="creation-wide">表演与动作<input value={segmentDraft.performance} onChange={event => setSegmentDraft(previous => ({ ...previous, performance: event.target.value }))} placeholder="例如：唱到呼音时抬爪并眨眼" /></label><label className="creation-wide">提示词<input value={segmentDraft.prompt} onChange={event => setSegmentDraft(previous => ({ ...previous, prompt: event.target.value }))} placeholder="描述这一段的画面、表情、动作和镜头" /></label>{advancedOpen && <label>工作流引用（可选）<input value={segmentDraft.workflow} onChange={event => setSegmentDraft(previous => ({ ...previous, workflow: event.target.value }))} placeholder="workflows/api/...json" /></label><label>首帧路径（可选）<input value={segmentDraft.first_frame} onChange={event => setSegmentDraft(previous => ({ ...previous, first_frame: event.target.value }))} placeholder="从已登记图片中选择或填路径" /></label><label>尾帧路径（可选）<input value={segmentDraft.last_frame} onChange={event => setSegmentDraft(previous => ({ ...previous, last_frame: event.target.value }))} placeholder="从已登记图片中选择或填路径" /></label></div><div className="creation-actions"><span>{planSegments.length ? `当前已有 ${planSegments.length} 段，新增片段会自动接在末尾。` : '这是空白项目的第一段计划。'}</span><button className="button primary compact" onClick={() => void createPlanSegment()}>保存分段</button></div></section>}
         {pipeline.find(stage => stage.id === pipelineStageId)?.inputSpecs?.some(input => input.control === 'asset') && (() => { const stage = pipeline.find(item => item.id === pipelineStageId)!; return <section className="pipeline-asset-register-panel" aria-label="登记步骤素材"><div><span className="eyebrow">素材登记</span><p>直接从本机选择文件，工作台会把它登记到当前作品目录，并自动加入可复用资源。</p></div><div className="pipeline-asset-register-grid">{stage.inputSpecs!.filter(input => input.control === 'asset' && assetKindForInput(input.kind)).map(input => <label key={`register-${input.id}`}><span>{input.label}</span><input type="file" accept={assetKindForInput(input.kind) === 'audio' ? 'audio/*' : assetKindForInput(input.kind) === 'image' ? 'image/*' : 'video/*'} onChange={event => { const file = event.target.files?.[0]; if (file) void registerPipelineAsset(input.id, file); }} /><small>登记后可在下方输入控件中复用</small></label>)}</div></section>; })()}
        <section className="project-heading"><div><div className="eyebrow">{project?.eyebrow ?? '导演工作台 / 当前项目'}</div><h1>{project?.title ?? '项目读取中'}<span className="title-tag">{project?.tag ?? '项目'}</span></h1><p>{project?.subtitle ?? '读取项目清单后显示作品说明。'}</p></div><div className="heading-actions"><button className="button secondary" onClick={exportPlan}><ArrowDownToLine size={15} />导出审阅计划</button><button className="button secondary" onClick={exportProjectBundle}><ArrowDownToLine size={15} />导出项目清单</button>{['preparing', 'submitting', 'queued', 'scheduler_waiting', 'running', 'stop_requested', 'stopping'].includes(taskState) && <button className="button secondary" onClick={stopCurrentTask}><Square size={14} />停止当前任务</button>}{['stopped', 'needs_reconcile'].includes(taskState) && <button className="button secondary" onClick={resumeCurrentTask}><Play size={14} />恢复当前片段</button>}<button className="button primary" disabled={!currentExecutable || Boolean(taskState && !['succeeded', 'failed', 'stopped', 'needs_reconcile'].includes(taskState)) || !current.id} title={!currentExecutable ? '请先登记该片段的 ComfyUI 工作流或执行引用' : '提交有效请求；所需资源暂不可用时等待排队'} onClick={() => void startCurrentTask()}>{taskState === 'preparing' || taskState === 'submitting' || (taskState === 'queued' || taskState === 'running') ? <Radio size={15} /> : <Play size={15} />}{taskState ? `${project?.default_task_asset_id ?? current.id} · ${taskState}` : !currentExecutable ? '先登记工作流' : `提交 ${project?.default_task_asset_id ?? current.id} 排队`}</button></div></section>
        {page === 'overview' && <div className="overview-actions" aria-label="项目页面入口"><span>从总览进入具体工作</span><button className="button secondary" onClick={() => setPage('pipeline')}><Workflow size={15} />查看制作流程</button><button className="button secondary" onClick={() => setPage('review')}><ClipboardCheck size={15} />进入片段审核</button></div>}
        <div className="scope-banner"><span className="status-dot" /><span>{assemblyIsStale ? '历史成片 · 需重新装配' : planAssembly ? '完整版候选已装配' : `${library.length} 个已登记素材 · 等待项目计划`}</span>{assemblyAsset && <button className="text-button" onClick={() => choose(assemblyAsset)}><Film size={13} />打开完整版候选</button>}<span>{assemblyIsStale ? '旧版本可预览，不能作为当前计划采用' : planAssembly ? '待导演审核 · 不代表已采用' : '可从侧栏导入或切换工作目录'}</span></div>

        <section className="pipeline-section" aria-label="制作步骤"><div className="section-header pipeline-header"><div><h2>制作步骤 <span>PIPELINE</span></h2><p>每一步只关心输入、输出和审核结果；节点或脚本由后台按步骤契约执行。</p></div><span className="pipeline-caption">{pipeline.length} 个步骤 · 可逐步调试</span></div><div className="pipeline-grid">{pipeline.map(stage => <article className={`pipeline-card ${pipelineStageId === stage.id ? 'pipeline-card-selected' : ''}`} key={stage.id}><div className="pipeline-card-top"><span className="pipeline-order">{String(stage.order).padStart(2, '0')}</span><div><h3>{stage.title}</h3><p>{stage.purpose}</p></div><span className={`pipeline-status pipeline-${stage.status === '已接入' ? 'ready' : stage.status === '可调试' ? 'debug' : 'planned'}`}>{stage.status}</span></div><div className="pipeline-io"><div><span>输入</span>{(stage.inputSpecs ?? stage.inputs.map((label, index) => ({ id: `input-${index}`, label, kind: '参数' as const, required: true, description: '' }))).map(input => <em key={input.id}>{input.label}{input.required ? ' · 必需' : ''}</em>)}</div><div className="pipeline-arrow">→</div><div><span>输出</span>{(stage.outputSpecs ?? stage.outputs.map((label, index) => ({ id: `output-${index}`, label, kind: '参数' as const, description: '', state: '待生成' as const }))).map(output => <em key={output.id}>{output.label}</em>)}</div></div><div className="pipeline-meta"><span>{stage.execution?.label ?? stage.backend}</span><span>{stage.recipe}</span></div><div className="pipeline-card-actions"><span>{stage.execution?.state ?? stage.status}</span><button className="text-button" onClick={() => setPipelineStageId(stage.id)} aria-label={`查看${stage.title}步骤档案`}>查看步骤档案 <ArrowUpRight size={13} /></button></div><details><summary>查看调试说明</summary><p>{stage.note}</p></details></article>)}</div>{pipeline.find(stage => stage.id === pipelineStageId) && (() => { const stage = pipeline.find(item => item.id === pipelineStageId)!; return <article className="pipeline-inspector" aria-label={`${stage.title}步骤档案`}><div className="pipeline-inspector-head"><div><span className="eyebrow">步骤档案 · {String(stage.order).padStart(2, '0')}</span><h3>{stage.title}</h3><p>{stage.purpose}</p></div><button className="icon-button" onClick={() => setPipelineStageId('')} aria-label="关闭步骤档案"><X size={17} /></button></div><div className="pipeline-inspector-grid"><div><div className="pipeline-inspector-label">审核者需要准备的输入</div>{(stage.inputSpecs ?? []).map(input => <div className="contract-row" key={input.id}><span className="contract-kind">{input.kind}</span><div><strong>{input.label}{input.required && <small>必需</small>}</strong><p>{input.description}{input.example && ` 示例：${input.example}`}</p></div></div>)}</div><div><div className="pipeline-inspector-label">这一步会登记的输出</div>{(stage.outputSpecs ?? []).map(output => <div className="contract-row" key={output.id}><span className={`contract-state contract-${output.state === '已存在' ? 'ready' : output.state === '待接入' ? 'planned' : 'pending'}`}>{output.state}</span><div><strong>{output.label}</strong><p>{output.kind} · {output.description}</p></div></div>)}</div></div><div className="pipeline-inspector-foot"><span>执行来源：{stage.execution?.label ?? stage.backend} · {stage.execution?.references?.join(' / ') ?? stage.recipe}</span><button className="button secondary compact" onClick={() => exportStageContract(stage)}><ArrowDownToLine size={14} />导出步骤契约</button></div></article>; })()}</section>

        <section className="preview-grid" aria-label="多媒体预览区">
          <div className="preview-panel">
            <div className="panel-toolbar"><span><span className="live-dot" />{labels[kind]}预览 <em>/ {current.id}</em></span><span>{current.ready ? '已有素材' : '计划占位'}{kind !== 'image' && <span className="muted"> · {range(current)}</span>}</span></div>
            <div className={`stage stage-${kind} ${!current.ready ? 'stage-empty' : ''}`}>
              {current.ready && kind === 'video' && <video key={source} ref={attachMedia} src={source} poster={current.image} {...mediaProps} aria-label={`${current.id} 视频预览`} />}
              {current.ready && kind === 'image' && <><img src={current.image} alt={current.title} /><button className="image-zoom icon-button" aria-label="放大当前图片" onClick={() => setLightbox(true)}><Maximize2 size={17} /></button></>}
              {current.ready && kind === 'audio' && <div className="audio-preview"><div className={`record ${playing ? 'record-playing' : ''}`}><div><Music2 size={30} /></div></div><span className="eyebrow">项目音频预览</span><h2>{current.title}</h2><p>{variant === 'A' ? '版本 A · 当前选定' : '版本 B · 对照试听'}</p><div className="audio-timeline">{baseAssets.filter(a => a.kind === 'audio' && a.start !== undefined).map(a => <button key={a.id} className={a.id === current.id ? 'selected' : ''} style={{ flex: (a.end! - a.start!) }} onClick={() => choose(a)} aria-label={`试听 ${a.id} ${a.title}`}>{a.id}<span>{a.end! - a.start!}s</span></button>)}</div><audio key={source} ref={attachMedia} src={source} {...mediaProps} aria-label={`${current.id} 音频预览`} /><small>母版时间轴 {range(current)} · 当前片段 {clock(position)} / {clock(duration)}</small></div>}
              {!current.ready && <div className="empty-state"><Clapperboard size={32} /><h2>这一段，还在构思中</h2><p>{current.intent}</p><span>没有生成结果 · 不会自动跳过此段</span></div>}
              {current.ready && kind === 'video' && <span className="stage-badge">实际生成画面 · v3-{variant} 配音</span>}
            </div>
            <div className="transport"><div className="transport-buttons"><button className="icon-button" aria-label="上一段" disabled={index === 0} onClick={() => choose(list[index - 1])}><ChevronLeft size={19} /></button>{kind === 'image' ? <span className="image-counter">{index + 1} / {list.length}</span> : <button className="play-button" aria-label={playing ? '暂停预览' : '播放当前片段'} disabled={playbackDisabled} title={current.ready && kind !== 'image' && mediaStatus === 'loading' ? '素材加载中…' : undefined} onClick={togglePlay}>{playing ? <Pause size={16} /> : <Play size={16} />}</button>}<button className="icon-button" aria-label="下一段" disabled={index === list.length - 1} onClick={() => choose(list[index + 1])}><ChevronRight size={19} /></button><span className="transport-title">{current.title}</span></div>{kind !== 'image' ? <label className="toggle"><input type="checkbox" checked={continuous} onChange={e => setContinuous(e.target.checked)} /><span />连续预览</label> : <span className="muted">逐张查看 · 保留各自提示词</span>}</div>
          </div>

          <aside className="inspector"><div className="inspector-heading"><span className="eyebrow">当前素材</span><span className="asset-code">{current.id}</span></div><h2>{current.title}</h2><p className="intent">{current.intent}</p><dl><div><dt>素材规格</dt><dd>{current.specs}</dd></div><div><dt>生成 / 来源</dt><dd>{current.origin}</dd></div></dl>{current.sources?.B && <div className="version-field"><label htmlFor="voice-version">试听版本</label><select id="voice-version" value={variant} onChange={e => { autoplayRef.current = false; setVariant(e.target.value as Variant); }}><option value="A">版本 A · 当前选定</option><option value="B">版本 B · 对照试听</option></select><small>{kind === 'video' ? '不同声音版本共用同一画面。' : '不同版本按同一时间轴试听。'}</small></div>}{current.lastImage && <div className="frame-pair"><button onClick={() => { switchKind('image'); setSelected(current.id); }}><img src={current.image} alt={`${current.title}首帧`} /><span>首帧 / 参考 <ArrowUpRight size={12} /></span></button><button onClick={() => { switchKind('image'); setSelected(current.id); }}><img src={current.lastImage} alt={`${current.title}尾帧`} /><span>尾帧 / 目标 <ArrowUpRight size={12} /></span></button></div>}<div className="inspector-bottom"><span className="small-label">审核标记 · 本浏览器保存</span><select aria-label="当前素材审核标记" value={drafts[current.id]?.review ?? '待审核'} onChange={e => updateDraft(current.id, { review: e.target.value })}><option>待审核</option><option>已采用</option><option>需调整</option></select></div></aside>
        </section>

        <section className="asset-section" aria-label="分段素材">
          <div className="section-header"><div><h2>分段素材 <span>ASSETS</span></h2><p>从准备到结果，每一段都有自己的制作档案。</p></div><button className="text-button" onClick={() => setExpanded(list.every(a => expanded.includes(a.id)) ? expanded.filter(id => !list.some(a => a.id === id)) : [...new Set([...expanded, ...list.map(a => a.id)])])}><Layers3 size={15} />{list.every(a => expanded.includes(a.id)) ? '收起当前全部' : '展开当前全部'}</button></div>
          <div className="asset-tabs" role="tablist" aria-label="素材类型">{(['video', 'audio', 'image'] as Kind[]).map(type => { const Icon = kindIcons[type]; const count = type === 'video' ? planVideoAssets.length : library.filter(asset => asset.kind === type).length; return <button key={type} role="tab" aria-selected={kind === type} className={kind === type ? 'selected' : ''} onClick={() => switchKind(type)}><Icon size={16} />{labels[type]}<span>{count}</span></button>; })}<span>{kind === 'video' ? `${library.filter(asset => asset.kind === 'video').length} 个视频资源` : kind === 'audio' ? `${library.filter(asset => asset.kind === 'audio').length} 个音频资源` : `${library.filter(asset => asset.kind === 'image').length} 个图片资源`}</span></div>
          <div className="asset-cards">{list.map(asset => {
            const Icon = kindIcons[asset.kind]; const open = expanded.includes(asset.id);
            return <article key={asset.id} className={`asset-card ${asset.id === current.id ? 'current' : ''}`}><button className={`card-preview ${!asset.ready ? 'unready' : ''} ${asset.kind === 'audio' ? 'sound-card' : ''}`} onClick={() => choose(asset)} aria-label={`查看 ${asset.id} ${asset.title}`}>{asset.image ? <img src={asset.image} alt={asset.title} /> : <div className="card-placeholder"><Icon size={26} /><span>{asset.ready ? '已有资源' : '待准备'}</span></div>}<span className="card-code">{asset.id}</span>{asset.start !== undefined && <span className="duration">{asset.end! - asset.start}s</span>}{asset.id === current.id && <span className="current-label">预览中</span>}</button><div className="card-info"><div><h3>{asset.title}</h3><span className={`asset-state ${!asset.ready ? 'pending' : ''}`}>{asset.ready ? drafts[asset.id]?.review ?? '待审核' : '待准备'}</span></div><p>{range(asset)}<span>{asset.subtitle}</span></p><button className="detail-toggle" aria-expanded={open} onClick={() => setExpanded(ids => open ? ids.filter(id => id !== asset.id) : [...ids, asset.id])}>制作档案<ChevronDown size={15} className={open ? 'rotated' : ''} /></button></div>{open && <div className="card-details"><label htmlFor={`prompt-${asset.id}`}>提示词草稿{drafts[asset.id]?.prompt !== undefined && <span>已编辑</span>}</label><textarea id={`prompt-${asset.id}`} rows={5} value={drafts[asset.id]?.prompt ?? asset.prompt} placeholder="输入这一段的提示词或下一版意图…" onChange={e => updateDraft(asset.id, { prompt: e.target.value })} /><label htmlFor={`note-${asset.id}`}>审核意见</label><textarea id={`note-${asset.id}`} rows={2} value={drafts[asset.id]?.note ?? ''} placeholder="记录需要调整的动作、画面或声音。" onChange={e => updateDraft(asset.id, { note: e.target.value })} /><details><summary>工作流与历史参数</summary><p>{asset.workflow}</p>{asset.prompt && <><p>历史输入提示词：</p><p className="historical-prompt">{asset.prompt}</p></>}<div className="workflow-downloads"><span>ComfyUI 工作流</span>{workflowEntriesForAsset(asset).length ? workflowEntriesForAsset(asset).map(entry => <a className="button secondary compact" key={`${entry.id}-${entry.path}`} href={workflowDownloadUrl(entry.path)} download={entry.path.split(/[\\/]/).pop()} title={entry.equivalence ?? (entry.comfyui_import === 'ui' ? '可导入 ComfyUI 界面查看' : '可提交到 ComfyUI API')}><ArrowDownToLine size={13} />{entry.comfyui_import === 'ui' ? '下载 UI 图' : '下载 API 图'}</a>) : <small>项目尚未登记可下载的 JSON 工作流</small>}</div></details><div className="detail-actions"><span>{asset.ready ? '保留已有结果，新版本另存' : '素材准备完成后才能提交'}</span><button className="icon-button" disabled aria-label={`生成 ${asset.id}`} title="由当前项目工作流执行"><Play size={14} /></button></div></div>}</article>;
          })}</div>
        </section>
        <section className="assembly-note"><span className="assembly-icon"><Layers3 size={19} /></span><div><strong>{kind === 'image' ? '多图作品，逐张审阅' : assemblyIsStale ? '历史成片 · 需重新装配' : assemblyAsset ? '完整版候选已由 ComfyUI 装配' : '先连续预览，再生成成品'}</strong><p>{kind === 'image' ? '每张图保留独立提示词、参考素材和版本，不强制转换为视频。' : assemblyAsset ? `${planAssembly?.notes ?? '完整候选保留片段顺序、剪辑点和工作流回执。'} 当前仍需人工检查音画、黑帧和段间连续性。` : '连续预览不会生成新文件。正式音视频拼接将提交给 ComfyUI；缺失的片段会明确提示。'}</p></div>{kind === 'image' ? <button disabled className="button secondary"><Plus size={15} />批量生成待接入</button> : <div className="assembly-actions"><div className="heading-actions"><button className="button secondary" disabled={Boolean(batchId && batchTasks.some(task => ['batch_waiting', 'preparing', 'submitting', 'queued', 'scheduler_waiting', 'running', 'stop_requested', 'stopping'].includes(task.status)))} onClick={startBatch}><Play size={14} />启动全片段批次</button>{batchId && batchTasks.some(task => ['batch_waiting', 'preparing', 'submitting', 'queued', 'scheduler_waiting', 'running', 'stop_requested', 'stopping'].includes(task.status)) && <button className="button secondary" onClick={stopBatch}><Square size={14} />停止批次</button>}{batchId && batchTasks.length > 0 && batchTasks.every(task => ['stopped', 'failed', 'succeeded'].includes(task.status)) && batchTasks.some(task => task.status !== 'succeeded') && <button className="button secondary" onClick={resumeBatch}><Play size={14} />恢复批次</button>}{['preparing', 'submitting', 'queued', 'scheduler_waiting', 'running', 'stop_requested', 'stopping'].includes(assemblyTaskState) && <button className="button secondary" onClick={stopAssembly}><Square size={14} />停止装配</button>}{['stopped', 'failed', 'needs_reconcile'].includes(assemblyTaskState) && <button className="button secondary" onClick={resumeAssembly}><Play size={14} />恢复装配</button>}{assemblyTaskState === 'needs_reconcile' && <button className="button secondary compact" onClick={() => void resolveMissingExecution(assemblyTaskId)}>确认装配执行已结束</button>}<button className="button secondary" disabled={resourceReleaseBlocked || ['preparing', 'submitting', 'queued', 'scheduler_waiting', 'running', 'stop_requested', 'stopping'].includes(assemblyTaskState)} onClick={() => void startAssembly()}><Plus size={15} />{assemblyTaskState === 'succeeded' ? '重新装配' : assemblyTaskState ? `装配 · ${assemblyTaskState}` : '启动完整版装配'}</button></div>{batchTasks.length > 0 && <div className="batch-progress"><span>{batchTasks.filter(task => task.status === 'succeeded').length}/{batchTasks.length} 段已完成</span><span>{batchTasks.find(task => ['preparing', 'submitting', 'queued', 'scheduler_waiting', 'running', 'stop_requested', 'stopping'].includes(task.status))?.asset_id ?? (batchTasks.every(task => task.status === 'succeeded') ? '批次完成' : '批次待处理')}</span></div>}</div>}</section>
        <footer><span><span className={`status-dot ${storageError ? 'error' : ''}`} />{storageError ? '本地保存失败，请导出审阅计划备份' : '草稿自动保存于本浏览器'}</span><span>{comfyOnline ? `ComfyUI 已连接 · ${comfyInfo.gpu_free_mib ?? '?'} MiB 空闲` : '模型与渲染由 ComfyUI 完成'} <ArrowUpRight size={12} /></span></footer>
      </div>
    </main>
    {notice && <div className="toast" role="status"><Check size={16} /><span>{notice}</span><button className="icon-button" onClick={() => setNotice('')} aria-label="关闭提示"><X size={15} /></button></div>}
    <dialog ref={dialogRef} className="lightbox" onCancel={() => setLightbox(false)}><div><span>{current.id} · {current.title}</span><button className="icon-button" onClick={() => setLightbox(false)} aria-label="关闭大图"><X size={22} /></button></div>{lightbox && <img src={current.image} alt={current.title} />}</dialog>
  </div>;
  */
}
