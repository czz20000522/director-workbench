import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { CSSProperties, MouseEvent as ReactMouseEvent } from 'react';
import { ArrowDownToLine, Check, ChevronLeft, ChevronRight, Film, FolderOpen, Image as ImageIcon, Layers3, Maximize2, Music2, Pause, Play, Radio, Sparkles, Square, Workflow, X } from 'lucide-react';
import { assets as fallbackAssets, labels, pipelineStages as defaultPipelineStages, range, type Asset, type Kind, type PipelineInput, type PipelineStage, type Variant } from './data';
// Split out of the initial bundle: mounted only in its own workspace mode.
const MaterialBridge = lazy(() => import('./MaterialBridge'));
import MaterialSelect from './MaterialSelect';
const SpeechPanel = lazy(() => import('./SpeechPanel'));
const AudioEditPanel = lazy(() => import('./AudioEditPanel'));
const MediaToolsPanel = lazy(() => import('./MediaToolsPanel'));
const AssemblyEditPanel = lazy(() => import('./AssemblyEditPanel'));
const EnhancementPanel = lazy(() => import('./EnhancementPanel'));
const KeyframePanel = lazy(() => import('./KeyframePanel'));
import ReferenceOrigin from './ReferenceOrigin';
import CreativeInspectionPanel, { ReferenceDifferenceSummary } from './CreativeInspectionPanel';
import { diagnosticMaterialPaths, isCreativeMaterial, materialPathKey, projectMaterialOptions } from './materialOptions';
const ProductionPresetPicker = lazy(() => import('./ProductionPresetPicker'));
import { selectVideoGenerationStage } from './generationStage';
// Split out of the initial bundle: mounted only in its own workspace mode.
const ReverseWorkbench = lazy(() => import('./ReverseWorkbench'));
import CreationRoutes, { creationRoutes } from './CreationRoutes';
// Split out of the initial bundle: mounted only in its own workspace mode.
const ScriptWorkbench = lazy(() => import('./ScriptWorkbench'));
import WorkbenchSidebar from './components/WorkbenchSidebar';
import WorkbenchTopbar from './components/WorkbenchTopbar';
import WorkbenchOverlays from './components/WorkbenchOverlays';
import ProjectActionsPopover from './components/ProjectActionsPopover';
import ResourceStatusPanel from './components/ResourceStatusPanel';
// Split out of the initial bundle: mounted only in its own workspace mode.
const ArchiveBrowser = lazy(() => import('./ArchiveBrowser'));
const ShotPromptEditor = lazy(() => import('./ShotPromptEditor'));
const FirstUseGuide = lazy(() => import('./FirstUseGuide'));
import TaskQueueStatus from './TaskQueueStatus';
import { blockerMessage, readinessStateLabel, type ReadinessItem } from './readiness';
import { assetLabel, assetTitle, type TreeKind } from './shotLabels';
import './quick-create.css';
import ResolutionPicker from './ResolutionPicker';
const CandidateComparison = lazy(() => import('./CandidateComparison'));
import { downloadName, mediaUrl, workflowUrl } from './api/client';
import { useComfyStatus, useDrafts, usePlan, usePlayback, useProjectSession, useProjectTasks, useSidebarWidth } from './app/hooks';

import type { BatchTask, CheckpointRecord, CreativeConflict, CreativeDraft, CreativeProfile, PipelineValidation, ProductionStep, ProductionStepState, ProjectManifest, ProjectState, QuickAspect, ReusableTemplate, ReviewRecord, SegmentDraft, SegmentEdit, ShotGenerationSettings, ShotVersion, TaskRecord, WorkflowCatalogEntry } from './app/types';
import { ASPECT_LABEL, QUICK_SERIES, adultVideoProfileNote, blankCreativeDraft, candidateOutputFacts, checkpointLabel, hasCreativeDraftContent, loadCreativeDraft, localSystemTime, measureStillAspect, normalizeCreativeDraft, normalizeLiveTaskState, normalizeManifestAsset, outputMethodLabel, outputResolutionLabel, readNewSegmentDraft, reviewLabel, sameCreativeDraft, taskStateLabel, taskStateLabels, versionMaterial } from './app/helpers';

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
  const { sidebarWidth, resizeSidebar, beginSidebarResize } = useSidebarWidth();
  const [pendingTreeItem, setPendingTreeItem] = useState<{ projectId: string; kind: TreeKind; id: string } | null>(null);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [quickMode, setQuickMode] = useState(true);
  const [quickCategory, setQuickCategory] = useState<'general' | 'adult'>('general');
  const [adultProfile, setAdultProfile] = useState<Extract<CreativeProfile, 'adult-realism' | 'adult-anime'>>('adult-realism');
  const [quickTemplate, setQuickTemplate] = useState<'image' | 'text-video' | 'video' | 'enhance' | null>(null);
  const [enhancementSource, setEnhancementSource] = useState('');
  const [reusableTemplates, setReusableTemplates] = useState<ReusableTemplate[]>([]);
  const [quickBusy, setQuickBusy] = useState(false);
  const quickWorking = useRef(false);
  const [quickError, setQuickError] = useState('');
  const refreshProjectsRef = useRef<(() => Promise<Array<{ id: string; title: string; series?: string; selected?: boolean }>>) | null>(null);
  const selectProjectRef = useRef<((projectId: string) => Promise<void>) | null>(null);
  const [referenceTarget, setReferenceTarget] = useState<{ projectId: string; analysisId: string } | null>(null);
  const [workspaceMode, setWorkspaceMode] = useState<'production' | 'reverse'>('production');
  const [workspacePage, setWorkspacePage] = useState<'creative' | 'script' | 'shots' | 'delivery' | 'enhance'>('shots');
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
  const [continuous, setContinuous] = useState(false);
  const [notice, setNotice] = useState('');
  const pageInteraction = useRef(0);
  useEffect(() => {
    if (!project?.id) { setReusableTemplates([]); return; }
    const controller = new AbortController();
    fetch(`/api/projects/${encodeURIComponent(project.id)}/reusable-templates`, { signal: controller.signal })
      .then(response => response.ok ? response.json() : Promise.reject(new Error('template catalog')))
      .then(data => { if (Array.isArray(data.templates)) setReusableTemplates(data.templates as ReusableTemplate[]); })
      .catch(reason => { if (reason?.name !== 'AbortError') setReusableTemplates([]); });
    return () => controller.abort();
  }, [project?.id]);

  useEffect(() => {
    const interacted = (event: Event) => {
      if (!event.isTrusted) return;
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
  const submittingShot = useRef(false);
  const regeneratingShot = useRef(false);
  const refreshProjectStateRef = useRef<(() => Promise<void>) | null>(null);
  const refreshPlanRef = useRef<(() => Promise<void>) | null>(null);
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
  const [selectedBatchAssetIds, setSelectedBatchAssetIds] = useState<string[]>([]);
  const [pipeline, setPipeline] = useState<PipelineStage[]>(defaultPipelineStages);
  const [pipelineStageId, setPipelineStageId] = useState('');
  const [pipelineOpen, setPipelineOpen] = useState(false);
  // `<details open>` 是受控属性。派生默认值一旦变化（切换快速/完整模式、切换生成方式、切到别的分镜），
  // React 就会重写 open，把用户手动的展开/收起顶掉。这里记住用户自己的选择，让它优先于派生默认值；
  // 只影响本会话，刷新后回到当前模式的默认状态。
  const [expandedPanels, setExpandedPanels] = useState<Record<string, boolean>>({});
  const panelOpen = (key: string, fallback: boolean) => expandedPanels[key] ?? fallback;
  const rememberPanel = (key: string) => (event: ReactMouseEvent<HTMLElement>) => {
    const node = event.currentTarget.parentElement;
    if (!(node instanceof HTMLDetailsElement)) return;
    // summary 的默认行为也会自己翻转 open，可能与 React 的同步刷新互相抵消；
    // 先取消默认行为，再由 React 独占写入，展开状态就只有一个来源。
    event.preventDefault();
    setExpandedPanels(current => ({ ...current, [key]: !node.open }));
  };
  const [inspectorTab, setInspectorTab] = useState<'intent' | 'materials' | 'params' | 'history'>('intent');
  const [shotEditTab, setShotEditTab] = useState<'materials' | 'prompt' | 'history'>('materials');
  const [shotVersions, setShotVersions] = useState<ShotVersion[]>([]);
  const [shotVersionsTotal, setShotVersionsTotal] = useState(0);
  const [shotVersionsOffset, setShotVersionsOffset] = useState(0);
  const [shotVersionsError, setShotVersionsError] = useState('');
  const [shotVersionsOwner, setShotVersionsOwner] = useState('');
  const [shotVersionsLoading, setShotVersionsLoading] = useState(false);
  const [restoringVersion, setRestoringVersion] = useState('');
  const [generationEdit, setGenerationEdit] = useState<ShotGenerationSettings>({ mode: 'auto', aspect_ratio: '16:9', sound_mode: 'performance_reference', seed: 42, speed_profile: 'standard', creative_profile: 'general' });
  const [generationDraft, setGenerationDraft] = useState<ShotGenerationSettings>({ mode: 'auto', aspect_ratio: '16:9', sound_mode: 'performance_reference', seed: 42, speed_profile: 'standard', creative_profile: 'general' });
  // 当前开始画面实测到的画幅。只用于对齐默认值并给出事实提示，null 表示没测出来（保持现有默认）。
  const [firstFrameAspect, setFirstFrameAspect] = useState<QuickAspect | null>(null);
  const [pipelineValues, setPipelineValues] = useState<Record<string, string | number | boolean | string[]>>({});
  const [pipelineValidation, setPipelineValidation] = useState<PipelineValidation | null>(null);
  const [draftReadiness, setDraftReadiness] = useState<ReadinessItem | null>(null);
  const { drafts, loadDraftsForProject, updateDraft, clearDraft, resetDrafts } = useDrafts(project?.id, {
    onStorageError: setStorageError,
    onPromptChange: () => setDraftReadiness(null),
  });
  const { comfyOnline, comfyInfo } = useComfyStatus();
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
  // 画幅是默认值而不是锁定值：快速预览只在 16:9 下可用，所以离开 16:9 一律回到标准方案
  // （与规格选择器同一条规则）。
  function adoptVideoAspect(aspect: QuickAspect) {
    setGenerationDraft(previous => previous.aspect_ratio === aspect ? previous : { ...previous, aspect_ratio: aspect, speed_profile: aspect === '16:9' ? previous.speed_profile : 'standard' });
  }
  // 开始画面可能来自本机草稿缓存、素材库、上传，也可能从刚生成的图片接手，所以统一在这里对齐画幅：
  // H3 图像转视频对首帧只做缩放铺满、不裁切，画幅与首帧不一致就会把画面压扁。对齐只在开始画面变化时发生，
  // 用户随后手动选的画幅不会被回写；读不到尺寸时保持现状，不猜。
  useEffect(() => {
    const path = quickMode && quickTemplate === 'video' ? segmentDraft.first_frame : '';
    if (!path) { setFirstFrameAspect(null); return; }
    let cancelled = false;
    void measureStillAspect(path).then(measured => {
      if (cancelled) return;
      setFirstFrameAspect(measured);
      if (measured) adoptVideoAspect(measured);
    });
    return () => { cancelled = true; };
  }, [quickMode, quickTemplate, segmentDraft.first_frame]);
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
  const { loading: projectLoading } = useProjectSession({
    privateMode,
    onProjectLoaded: data => {
      setProject(data);
      setSelected('');
      setPipelineStageId('');
      loadDraftsForProject(String(data.id));
      setCreativeDraft(loadCreativeDraft(`director-workbench:${String(data.id)}:creative:v1`));
      setCreativeConflict(null);
      void refreshProjectsRef.current?.().catch(() => {});
    },
    onOffline: () => {
      setNotice(privateMode ? '正在读取你的作品…' : '项目清单暂时不可用，当前仅显示离线占位。');
      if (!privateMode) return;
      const request = refreshProjectsRef.current?.();
      if (!request) return;
      void request.then(async items => {
        const last = localStorage.getItem('director-workbench:last-project');
        const initial = items.find(item => item.id === last) ?? items[0];
        if (initial) await selectProjectRef.current?.(initial.id);
        else setNotice('还没有作品。可以使用上方“新建作品”开始创作。');
      }).catch(() => setNotice('作品列表读取失败，请刷新重试。'));
    },
  });
  const [reviewConflict, setReviewConflict] = useState<{ projectId: string; assetId: string; stage: ReviewRecord['stage']; state: ProjectState } | null>(null);
  const [projectState, setProjectState] = useState<ProjectState | null>(null);
  const [stateSaving, setStateSaving] = useState(false);
  const [segmentEdit, setSegmentEdit] = useState<SegmentEdit>({ duration_seconds: '', location: '', shot_size: '', camera: '', wardrobe: '', performance: '', prompt: '', workflow: '', first_frame: '', last_frame: '', audio_guide: '', delivery_master: '', dependencies: '' });
  useEffect(() => { setDraftReadiness(null); }, [project?.id, selected, segmentEdit]);
  const [mergeEnd, setMergeEnd] = useState('');
  useEffect(() => { setMergeEnd(''); }, [project?.id, selected]);
  const creatingSegment = useRef(false);
  const { planSegments, setPlanSegments, planRevision, setPlanRevision, planAssembly, setPlanAssembly, refreshPlan } = usePlan({
    projectId: project?.id,
    privateMode,
    onRefreshState: () => { void refreshProjectStateRef.current?.().catch(() => { /* the plan remains usable while state is offline */ }); },
  });
  refreshPlanRef.current = refreshPlan;
  const creativeStorageKey = `director-workbench:${project?.id ?? 'loading'}:creative:v1`;
  const baseAssets = useMemo(() => project ? (Array.isArray(project.assets) ? project.assets.filter(isCreativeMaterial).map(normalizeManifestAsset) : []) : privateMode ? [] : fallbackAssets, [privateMode, project]);
  const diagnosticPaths = useMemo(() => diagnosticMaterialPaths(project), [project]);
  const isDiagnosticPath = (path?: string | null) => !!path && diagnosticPaths.some(item => materialPathKey(item) === materialPathKey(path));
  const emptyAsset: Asset = { id: 'empty-project', kind, title: '尚未登记素材', subtitle: '先从上方登记资源或创建分段', ready: false, intent: '这个项目还没有可预览的素材。先选择文件、登记制作步骤或添加分段计划。', prompt: '', origin: '空白项目', specs: '', workflow: '待登记' };
  const mediaRef = useRef<HTMLMediaElement | null>(null);
  const [mediaElement, setMediaElement] = useState<HTMLMediaElement | null>(null);
  const attachMedia = useCallback((node: HTMLMediaElement | null) => {
    mediaRef.current = node;
    setMediaElement(node);
  }, []);
  const autoplayRef = useRef(false);
  const advancing = useRef(false);
  const dialogRef = useRef<HTMLDialogElement>(null);
  const planVideoAssets: Asset[] = useMemo(() => planSegments.map(segment => {
    const base = baseAssets.find(asset => asset.id === segment.id);
    const firstFrame = mediaUrl(segment.keyframes?.first);
    const videoSource = mediaUrl(segment.video?.path);
    const reworkSource = mediaUrl(segment.video?.rework_candidate?.path);
    const finishSource = mediaUrl(segment.video?.finish_review?.path);
    return {
      id: segment.id, kind: 'video', title: `${segment.id} · ${segment.location}`, subtitle: `${segment.shot_size} · ${segment.camera} · ${segment.wardrobe}`,
      start: segment.start_seconds, end: segment.end_seconds, ready: Boolean(videoSource) || Boolean(base?.ready), image: firstFrame ?? base?.image, lastImage: mediaUrl(segment.keyframes?.last),
      sources: videoSource ? { A: videoSource, ...((reworkSource ?? finishSource) ? { B: reworkSource ?? finishSource } : {}) } : base?.sources, intent: segment.performance, prompt: segment.prompt ?? base?.prompt ?? '', origin: segment.video?.path ? `ComfyUI history 成功回执 · ${segment.comfyui_task_id ?? '任务 ID 未登记'}${segment.video.rework_candidate?.prompt_id ? ` · 重做样片 ${segment.video.rework_candidate.prompt_id}` : segment.video.finish_review?.prompt_id ? ` · 精细化候选 ${segment.video.finish_review.prompt_id}` : ''}` : (segment.keyframes?.first ? '生成前分段计划 · 已登记首帧' : '分镜草案 · 待准备本作关键帧'), specs: `${segment.duration_seconds} 秒 · ${segment.status}${segment.video?.rework_candidate?.specs ? ` · B ${segment.video.rework_candidate.specs}` : segment.video?.finish_review?.specs ? ` · B ${segment.video.finish_review.specs}` : ''}`, workflow: [segment.workflow ?? base?.workflow ?? '待登记', segment.video?.rework_candidate?.workflow ? `重做 ${segment.video.rework_candidate.workflow}` : '', segment.video?.rework_candidate?.ui_workflow ? `画布 ${segment.video.rework_candidate.ui_workflow}` : '', segment.video?.finish_review?.workflow ? `精细化 ${segment.video.finish_review.workflow}` : '', segment.keyframes?.first ? `首帧 ${segment.keyframes.first}` : '', segment.keyframes?.last ? `尾帧 ${segment.keyframes.last}` : '', segment.audio?.delivery_master ? `交付音频 ${segment.audio.delivery_master}` : '', segment.audio?.guide ? `引导音频 ${segment.audio.guide}` : '', segment.comfyui_task_id ? `ComfyUI 任务 ${segment.comfyui_task_id}` : ''].filter(Boolean).join(' · '),
    };
  }), [baseAssets, planSegments]);
  const planAudioAssets: Asset[] = useMemo(() => planSegments.flatMap(segment => {
    const audioPath = segment.audio?.delivery_master ?? segment.audio?.guide;
    const audioSource = mediaUrl(audioPath);
    if (!audioSource) return [];
    return [{
      id: segment.id, kind: 'audio', title: `${segment.id} · ${segment.audio?.delivery_master ? '交付音频' : '表演引导'}`,
      subtitle: `${segment.duration_seconds} 秒 · ${segment.audio?.delivery_master ? '已登记交付母版' : '参考演唱切片'}`,
      start: 0, end: segment.duration_seconds, ready: true, sources: { A: audioSource }, intent: segment.performance,
      prompt: segment.prompt ?? '', origin: audioPath ?? '', specs: '44.1 kHz · 分段音频', workflow: segment.workflow ?? '待登记',
    }];
  }), [planSegments]);
  const planImageAssets: Asset[] = useMemo(() => planSegments.flatMap(segment => {
    const image = mediaUrl(segment.keyframes?.first);
    if (!image || isDiagnosticPath(segment.keyframes?.first)) return [];
    return [{
      id: segment.id, kind: 'image', title: `${segment.id} · 首帧`, subtitle: `${segment.location} · ${segment.shot_size}`,
      ready: true, image, lastImage: mediaUrl(segment.keyframes?.last), intent: segment.performance,
      prompt: segment.prompt ?? '', origin: segment.keyframes?.first ?? '', specs: '首帧候选', workflow: segment.workflow ?? '待登记',
    }];
  }), [diagnosticPaths, planSegments]);
  const assembly = planAssembly;
  const assemblyIsStale = assembly?.status === 'stale';
  const assemblySource = mediaUrl(assembly?.output);
  const assemblyAsset: Asset | null = assembly && assemblySource ? {
    id: project?.assembly_asset_id || 'assembly', kind: 'video', title: `${project?.title ?? '项目'} · ${assemblyIsStale ? '历史成片 · 需重新装配' : '当前成片'}`,
    subtitle: `${assembly.duration_seconds?.toFixed(2) ?? '—'} 秒 · ${assemblyIsStale ? '对应旧计划' : '已装配'}`,
    start: 0, end: assembly.duration_seconds ?? assembly.target_duration_seconds ?? 0,
    ready: true, sources: { A: assemblySource }, image: mediaUrl(planSegments[0]?.keyframes?.first),
    intent: assemblyIsStale ? '这是旧计划的历史成片，仍可预览；当前计划已变化，请重新装配。' : '当前作品的成片。请检查音画后决定是否继续修改分镜。',
    prompt: '', origin: assembly.prompt_id ? `装配任务回执 · ${assembly.prompt_id}` : '装配结果 · 未登记任务回执',
    specs: `${assembly.duration_seconds?.toFixed(2) ?? '—'} 秒 · 分辨率与编码待核验`,
    workflow: assembly.workflow ?? '待登记',
  } : null;
  const derivedMaterialAssets = useMemo(() => [...planAudioAssets, ...planImageAssets], [planAudioAssets, planImageAssets]);
  const remainingBaseAssets = useMemo(() => baseAssets.filter(asset => !planVideoAssets.some(shot => asset.kind === 'video' && shot.id === asset.id) && !derivedMaterialAssets.some(derived => derived.kind === asset.kind && derived.id === asset.id)), [baseAssets, derivedMaterialAssets, planVideoAssets]);
  const library = useMemo(() => [...planVideoAssets, ...(assemblyAsset ? [assemblyAsset] : []), ...derivedMaterialAssets, ...remainingBaseAssets], [assemblyAsset, derivedMaterialAssets, planVideoAssets, remainingBaseAssets]);
  const list = useMemo(() => kind === 'video' && assemblyAsset?.id === selected
    ? [assemblyAsset]
    : library.filter(asset => asset.kind === kind && (kind !== 'video' || asset.id !== assemblyAsset?.id)), [assemblyAsset, kind, library, selected]);
  const current = list.find(asset => asset.id === selected) ?? list[0] ?? emptyAsset;
  const taskModel = useProjectTasks({
    projectId: project?.id,
    assemblyAssetId: project?.assembly_asset_id,
    currentAssetId: current.id,
    batchId,
    onTaskSucceeded: () => { void refreshPlanRef.current?.().catch(() => { /* a later stream update can retry the plan refresh */ }); },
  });
  const { taskState, setTaskState, taskId, setTaskId, currentTask, setCurrentTask, projectTasks, setProjectTasks, assemblyTaskState, setAssemblyTaskState, assemblyTaskId, setAssemblyTaskId } = taskModel;
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
  // Plan segments can expose an alternate B candidate (rework or AI finish).
  // Keep the selected candidate consistent across the player, review action,
  // and download link so a newly registered B result is actually inspectable.
  const source = current.sources?.[variant] ?? current.sources?.A;
  const mediaKey = JSON.stringify([project?.id, current.id, source]);
  const index = list.findIndex(asset => asset.id === current.id);
  const start = kind === 'audio' ? current.start ?? 0 : 0;
  const duration = (current.end ?? 0) - (current.start ?? 0);
  const activeTaskStates = ['preparing', 'submitting', 'queued', 'scheduler_waiting', 'running', 'stop_requested', 'stopping', 'batch_waiting', 'needs_reconcile'];
  const batchActive = batchTasks.some(task => activeTaskStates.includes(task.status));
  const currentPlanSegment = planSegments.find(segment => segment.id === current.id);
  const viewContext = useRef({ projectId: project?.id, assetId: current.id, source });
  viewContext.current = { projectId: project?.id, assetId: current.id, source };
  const savedReadiness = projectState?.readiness;
  const currentReadiness = savedReadiness?.segments.find(segment => segment.asset_id === current.id);
  const isAssemblyAsset = Boolean(project?.assembly_asset_id && current.id === project.assembly_asset_id);
  const isProjectVideoMaterial = kind === 'video' && !currentPlanSegment && !isAssemblyAsset && current.id !== 'empty-project';
  const focusList = useMemo(() => kind === 'video' ? (isProjectVideoMaterial || isAssemblyAsset ? [current] : planVideoAssets) : list, [current, isAssemblyAsset, isProjectVideoMaterial, kind, list, planVideoAssets]);
  const focusIndex = focusList.findIndex(asset => asset.id === current.id);
  const { playing, mediaStatus, playbackDisabled, togglePlay, mediaProps } = usePlayback({
    mediaKey, start, duration, kind, currentEnd: current.end, mediaElement, mediaRef, autoplayRef, advancing,
    ready: current.ready, continuous, focusList, focusIndex,
    onAdvance: asset => choose(asset as Asset, true), onNotice: setNotice,
  });
  const editableMaterial = !currentPlanSegment && !isAssemblyAsset ? project?.assets?.find(asset => asset.id === current.id && asset.kind === kind) : undefined;
  useEffect(() => { setMaterialTitle(editableMaterial ? String(editableMaterial.title ?? '') : ''); }, [project?.id, current.id, editableMaterial?.title]);
  const currentTaskTemplate = project?.task_templates?.[current.id];
  const canReviewCurrent = kind === 'video' && !isProjectVideoMaterial && (current.ready || taskState === 'succeeded');
  const materialCandidateRef = current.sources?.[variant] ?? (kind === 'image' && variant === 'A' ? current.image : undefined);
  const canReviewAsset = canReviewCurrent || ((kind === 'image' || kind === 'audio') && current.ready && Boolean(materialCandidateRef));
  const currentExecutable = kind === 'video' && !isAssemblyAsset && Boolean(currentReadiness?.stage_id);
  const timelineAssets = useMemo(() => kind === 'video' ? planVideoAssets : list, [kind, list, planVideoAssets]);
  const currentAudioPath = currentPlanSegment?.audio?.delivery_master ?? currentPlanSegment?.audio?.guide;
  const currentAudioSource = currentAudioPath
    ? mediaUrl(currentAudioPath)
    : kind === 'audio' ? source : undefined;
  const currentSampleRecord = projectState?.reviews[current.id]?.sample;
  const currentFinishRecord = projectState?.reviews[current.id]?.finish;
  const currentFinalRecord = projectState?.reviews[current.id]?.final;
  const currentAttempts = projectTasks.filter(task => task.asset_id === current.id);
  const finishTaskId = currentPlanSegment?.video?.finish_review?.task_id ?? currentPlanSegment?.video?.finish_review?.prompt_id;
  const selectedCandidateTask = variant === 'B' && finishTaskId
    ? projectTasks.find(task => task.id === finishTaskId) ?? currentTask
    : currentTask;
  const currentOutputFacts = candidateOutputFacts(selectedCandidateTask, currentPlanSegment, variant);
  const generationHistory = currentPlanSegment?.generation_history ?? [];
  const currentVersionNumber = currentPlanSegment?.current_version_task_id
    ? Math.max(1, generationHistory.findIndex(item => item.task_id === currentPlanSegment.current_version_task_id) + 1)
    : current.ready ? Math.max(1, generationHistory.length) : 0;
  const currentReview = isProjectVideoMaterial ? '参考素材' : current.ready ? reviewLabel((isAssemblyAsset ? currentFinalRecord : currentSampleRecord)?.status) : '待准备';
  const currentFinishReview = reviewLabel(currentFinishRecord?.status);
  const currentPrompt = drafts[current.id]?.prompt ?? currentPlanSegment?.prompt ?? currentTaskTemplate?.prompt ?? current.prompt;
  const currentNote = drafts[current.id]?.note ?? (isAssemblyAsset ? currentFinalRecord?.note : currentSampleRecord?.note) ?? currentPlanSegment?.visual_review?.reason ?? '';
  const currentPipeline = useMemo(() => pipeline.filter(stage => isAssemblyAsset
    ? stage.id === 'assembly'
    : kind === 'audio'
      ? stage.id.includes('audio') || stage.id.includes('voice')
      : kind === 'image'
        ? stage.id.includes('frame') || stage.outputs.some(output => output.includes('图') || output.includes('帧'))
        : !stage.id.includes('assembly') && !stage.id.includes('audio-master')), [isAssemblyAsset, kind, pipeline]);
  const currentGenerationStage = kind === 'video' && !isAssemblyAsset && !isProjectVideoMaterial
    ? currentPipeline.find(stage => stage.id === currentReadiness?.stage_id) ?? selectVideoGenerationStage(currentPipeline, currentPlanSegment?.workflow || currentTaskTemplate?.workflow || undefined, project?.production_preset?.workflow) : undefined;
  const activePipelineStage = currentPipeline.find(stage => stage.id === pipelineStageId) ?? currentGenerationStage ?? currentPipeline[0];
  const durationInput = currentGenerationStage?.inputSpecs?.find(input => input.id === 'duration');
  const isH3Stage = Boolean(currentGenerationStage?.recipe.startsWith('H3 '));
  const autoCreation = currentPlanSegment ? currentPlanSegment.generation?.mode === 'auto' : project?.default_creation_mode === 'auto';
  useEffect(() => { setGenerationEdit({ mode: 'advanced', aspect_ratio: '16:9', sound_mode: 'performance_reference', seed: 42, speed_profile: 'standard', creative_profile: 'general', ...(currentPlanSegment?.generation ?? {}) }); }, [project?.id, currentPlanSegment?.id, currentPlanSegment?.generation?.mode, currentPlanSegment?.generation?.aspect_ratio, currentPlanSegment?.generation?.sound_mode, currentPlanSegment?.generation?.seed, currentPlanSegment?.generation?.speed_profile, currentPlanSegment?.generation?.creative_profile, currentPlanSegment?.generation?.output_resolution]);
  const isTextGeneration = currentGenerationStage?.generation_mode === 'text_to_video';
  const supportsH3Duration = isH3Stage && durationInput?.min === 4 && durationInput?.max === 15;
  const readinessGaps = currentReadiness?.blockers.map(blockerMessage) ?? ['正在读取服务器准备度…'];
  const candidateA = current.sources?.A ?? source;
  const candidateB = current.sources?.B;
  const currentCandidateBIsRework = Boolean(currentPlanSegment?.video?.rework_candidate?.path);
  const currentCandidateBIsFinish = Boolean(!currentCandidateBIsRework && currentPlanSegment?.video?.finish_review?.path);
  const selectedCandidateLabel = variant === 'B'
    ? currentCandidateBIsFinish ? 'B · AI 高清候选' : 'B · 重做样片'
    : 'A · 当前样片';
  useEffect(() => {
    if (!current.sources?.B && variant === 'B') setVariant('A');
  }, [current.id, current.sources?.B, variant]);
  const taskIsActive = activeTaskStates.includes(taskState);
  const hasPlanCheckpoint = Boolean(currentPlanSegment) || planSegments.length > 0;
  const hasMaterialCheckpoint = Boolean(current.image || current.lastImage || currentAudioSource || (current.workflow && current.workflow !== '待登记'));
  const hasGeneratedCheckpoint = Boolean(canReviewCurrent);
  const hasApprovalCheckpoint = currentSampleRecord?.status === 'approved';
  const hasFinishCheckpoint = currentFinishRecord?.status === 'approved';
  const finishRejected = currentPlanSegment?.video?.finish_review?.status?.startsWith('rejected') ?? false;
  const assemblyReady = savedReadiness?.assembly.ready === true;
  const savedPlanCurrent = savedReadiness?.revision === planRevision;
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
    try { localStorage.setItem(creativeStorageKey, JSON.stringify(creativeDraft)); setStorageError(false); }
    catch { setStorageError(true); }
  }, [creativeDraft, creativeStorageKey, project]);

  useEffect(() => {
    void refreshPlan().catch(() => { /* the static pilot remains available while the plan is offline */ });
    void refreshProjectState().catch(() => { /* local drafts remain available while the archive is offline */ });
    setBatchId(''); setBatchTasks([]);
  }, [project?.id]);

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
  useEffect(() => { setShotVersionsOffset(0); setShotVersions([]); setShotVersionsTotal(0); setShotVersionsError(''); setShotVersionsOwner(''); }, [project?.id, current.id, planRevision]);
  useEffect(() => {
    const projectId = project?.id;
    const segmentId = currentPlanSegment?.id;
    if (!projectId || !segmentId || kind !== 'video') return;
    let cancelled = false;
    setShotVersionsLoading(true);
    fetch(`/api/projects/${encodeURIComponent(projectId)}/segments/${encodeURIComponent(segmentId)}/versions?limit=20&offset=${shotVersionsOffset}`)
      .then(async response => { const data = await response.json(); if (!response.ok) throw new Error(data.detail?.message || data.detail || '历史记录读取失败'); return data as { items: ShotVersion[]; total: number }; })
      .then(data => { if (cancelled || activeProjectId.current !== projectId) return; setShotVersions(previous => shotVersionsOffset ? [...previous, ...data.items] : data.items); setShotVersionsTotal(data.total); setShotVersionsOwner(`${projectId}:${segmentId}`); setShotVersionsError(''); })
      .catch(error => { if (!cancelled) setShotVersionsError(error instanceof Error ? error.message : '历史记录读取失败'); })
      .finally(() => { if (!cancelled) setShotVersionsLoading(false); });
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
  }, [currentPlanSegment?.keyframes?.first, currentPlanSegment?.keyframes?.last, currentPlanSegment?.audio?.guide, currentPlanSegment?.duration_seconds, currentPlanSegment?.generation?.seed, currentPlanSegment?.generation?.output_resolution]);

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
    if (lightbox) dialogRef.current?.showModal();
    else dialogRef.current?.close();
  }, [lightbox]);

  function choose(asset: Asset, auto = false) {
    mediaRef.current?.pause();
    autoplayRef.current = auto;
    setKind(asset.kind);
    setSelected(asset.id);
    if (!asset.sources?.B) setVariant('A');
    setNotice('');
  }

  function openTreeItem(itemKind: TreeKind, id: string) {
    mediaRef.current?.pause();
    setKind(itemKind);
    setSelected(id);
    setWorkspaceMode('production');
    setWorkspacePage('shots');
    setSegmentFormOpen(false);
    setQuickTemplate(itemKind === 'image' ? 'image' : itemKind === 'video' ? 'video' : null);
    setNotice('');
  }

  function openTreeProject(projectId: string, item?: { kind: TreeKind; id: string }) {
    setArchive(null);
    if (item) setPendingTreeItem({ projectId, ...item });
    if (projectId !== project?.id) void selectProject(projectId);
    else if (item) openTreeItem(item.kind, item.id);
  }

  function switchKind(next: Kind) {
    mediaRef.current?.pause();
    autoplayRef.current = false;
    setKind(next);
    setSelected(next === 'video' ? (planSegments[0]?.id ?? baseAssets.find(asset => asset.kind === 'video')?.id ?? '') : next === 'audio' ? (baseAssets.find(asset => asset.kind === 'audio')?.id ?? '') : (baseAssets.find(asset => asset.kind === 'image')?.id ?? ''));
    setNotice('');
  }

  async function persistReview(stage: 'sample' | 'finish' | 'final', status: ReviewRecord['status'], adoptedVariant?: Variant) {
    if (!current.id || !project?.id) return;
    if (restoringVersion) { setNotice('正在恢复候选，请完成后再确认审核。'); return; }
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
      clearDraft(segmentId);
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
  refreshProjectStateRef.current = refreshProjectState;

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
    if (!project || !currentPlanSegment || submittingShot.current || regeneratingShot.current) return;
    if (activeTaskStates.includes(taskState)) { setNotice('当前分镜仍有任务在运行，请等待或先停止任务。'); return; }
    const pendingKey = `director-workbench:${project.id}:${current.id}:pending-submission:v1`;
    if (localStorage.getItem(pendingKey)) { setNotice('上一次提交结果尚未确认，请先核对回执，再重新生成。'); return; }
    regeneratingShot.current = true;
    try {
      if (!await saveCurrentSegment()) return;
      await startCurrentTask({
        prompt: currentPrompt,
        'first-frame': segmentEdit.first_frame,
        'last-frame': segmentEdit.last_frame,
        guide: segmentEdit.audio_guide,
        duration: Number(segmentEdit.duration_seconds),
      });
    } finally { regeneratingShot.current = false; }
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

  async function refreshProjects(): Promise<Array<{ id: string; title: string; series?: string; selected?: boolean }>> {
    const response = await fetch('/api/projects');
    if (!response.ok) throw new Error('项目列表读取失败');
    const data = await response.json();
    const items = Array.isArray(data.projects) ? data.projects : [];
    setProjectList(items);
    return items;
  }
  refreshProjectsRef.current = refreshProjects;

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
      setProject(data as ProjectManifest); setProjectState(null); setCreativeConflict(null); setProjectMenuOpen(false); setPlanSegments([]); setPlanAssembly(null); setTaskId(''); setTaskState(''); setCurrentTask(null); setProjectTasks([]); setAssemblyTaskId(''); setAssemblyTaskState(''); setBatchId(''); setBatchTasks([]); setSelected(kind === 'video' ? '' : nextAssets.find(asset => asset.kind === kind)?.id ?? ''); loadDraftsForProject(String(data.id)); setCreativeDraft(loadCreativeDraft(`director-workbench:${String(data.id)}:creative:v1`));
      setArchive(null);
      localStorage.setItem('director-workbench:last-project', data.id);
      setNotice(`已切换项目：${data.title ?? data.id}`);
      void refreshProjects();
      setSeriesRefreshKey(value => value + 1);
      return data as ProjectManifest;
    } catch (error) { if (reportFailure) throw error; setNotice(error instanceof Error ? error.message : '项目切换失败。'); }
  }
  selectProjectRef.current = async projectId => { await selectProject(projectId); };

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
      setProject(data.project as ProjectManifest); setProjectState(null); setCreativeConflict(null); setProjectMenuOpen(false); setCreateOpen(false); setPlanSegments([]); setPlanAssembly(null); setTaskId(''); setTaskState(''); setCurrentTask(null); setProjectTasks([]); setAssemblyTaskId(''); setAssemblyTaskState(''); setBatchId(''); setBatchTasks([]); setSelected(''); resetDrafts(); setCreativeDraft(initialCreative); setNewSeries(''); setNewTitle(''); setNewProjectId('');
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
      setProject(data.project as ProjectManifest); setImportFiles([]); setNotice(`已登记 ${data.uploaded?.length ?? 0} 个资源，工作流和媒体仍由 ComfyUI 负责执行。`); void refreshProjects();
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

  async function openQuickTemplate(template: 'image' | 'text-video' | 'video' | 'enhance', firstFrame = '', zone: 'general' | 'adult' = quickCategory) {
    if (quickWorking.current || projectLoading) return;
    quickWorking.current = true; setQuickBusy(true); setQuickError('');
    try {
      // 每个快速分区的创作入口各自对应一个系列：成人向内容必须留在「成人向创作」，
      // 普通创作也不应被写进该系列。只有当前作品不属于本分区的系列时才新建并切换；
      // 用户自建的其他作品在普通分区里仍是默认去向，清晰化等加工入口始终作用于当前作品。
      const series = QUICK_SERIES[zone];
      const previousSeries = project?.series ?? '';
      const inZoneSeries = template === 'enhance' || (zone === 'adult' ? previousSeries === series : previousSeries !== QUICK_SERIES.adult);
      let switchedTitle = '';
      if (!project || !inZoneSeries) {
        // Private workspaces assign their own IDs. Recover a lost response by
        // the unique draft title within the authenticated project catalog.
        const key = `director-workbench:quick-project-pending:${zone}:v1`;
        const pending = localStorage.getItem(key);
        const title = pending || `${zone === 'adult' ? '成人向试作' : '随手创作'} ${new Date().toLocaleString('zh-CN', { hour12: false })} · ${crypto.randomUUID().slice(0, 6)}`;
        if (!pending) localStorage.setItem(key, title);
        const response = pending
          ? await fetch('/api/projects')
          : await fetch('/api/projects/create', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ series, title, creation_mode: 'auto' }) });
        const data = await response.json();
        if (!response.ok) {
          if (!pending && response.status >= 400 && response.status < 500) localStorage.removeItem(key);
          throw new Error(data.detail?.message || data.detail || '草稿创建失败');
        }
        const matches = pending ? data.projects?.filter((item: { title: string; series: string }) => item.title === title && item.series === series) : [data.project];
        if (matches?.length !== 1 || !matches[0]?.id) throw new Error('尚未找回草稿创建回执，请在作品列表核对后继续；没有重复创建。');
        const created = matches[0];
        await selectProject(created.id, true, pageInteraction.current);
        localStorage.removeItem(key);
        switchedTitle = title;
      }
      setQuickCategory(zone); setQuickMode(true); setQuickTemplate(template); setArchive(null);
      // switchKind 会清空提示，所以切换说明必须放在每个分支的最后一句。
      const switchNotice = switchedTitle ? (previousSeries && previousSeries !== series
        ? `内容不会写进「${previousSeries}」作品；已新建并切换到「${series}」作品：${switchedTitle}`
        : `已新建「${series}」作品：${switchedTitle}`) : '';
      if (template === 'enhance') {
        setEnhancementSource(firstFrame); setWorkspaceMode('production'); setWorkspacePage('enhance');
        if (switchNotice) setNotice(switchNotice); return;
      }
      setWorkspaceMode('production'); setWorkspacePage('shots'); setAdvancedOpen(false);
      switchKind(template === 'image' ? 'image' : 'video');
      setSegmentFormOpen(template !== 'image');
      if (zone === 'adult') setGenerationDraft(previous => ({ ...previous, creative_profile: adultProfile, speed_profile: 'standard' }));
      if (firstFrame) setSegmentDraft(previous => ({ ...previous, first_frame: firstFrame }));
      if (switchNotice) setNotice(switchNotice);
    } catch (error) { setQuickError(error instanceof Error ? error.message : '暂时无法打开生成入口'); }
    finally { quickWorking.current = false; setQuickBusy(false); }
  }

  async function uploadQuickImage(file: File) {
    if (!project || quickWorking.current) return;
    const projectId = project.id;
    quickWorking.current = true; setQuickBusy(true); setQuickError('');
    try {
      const form = new FormData(); form.append('files', file, `assets/${file.name}`);
      const response = await fetch(`/api/projects/${encodeURIComponent(projectId)}/upload`, { method: 'POST', body: form });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail?.message || data.detail || '图片上传失败');
      if (activeProjectId.current !== projectId) return;
      const path = data.uploaded?.[0];
      if (!path) throw new Error('上传回执缺少素材路径，请从素材库选择已上传的图片。');
      setProject(data.project); setSegmentDraft(previous => ({ ...previous, first_frame: path }));
    } catch (error) { if (activeProjectId.current === projectId) setQuickError(error instanceof Error ? error.message : '图片上传失败'); }
    finally { quickWorking.current = false; setQuickBusy(false); }
  }

  async function createPlanSegment(generate = false) {
    if (creatingSegment.current) return;
    if (!project) { setNotice('请先创建或选择一个项目。'); return; }
    const duration = Number(segmentDraft.duration_seconds);
    if (generate && (!segmentDraft.prompt.trim() || quickTemplate === 'video' && !segmentDraft.first_frame)) { setQuickError('请填写完整提示词；图生视频还需要开始画面。'); return; }
    if (!Number.isFinite(duration) || duration <= 0) { setNotice('片段时长必须是大于 0 的数字。'); return; }
    creatingSegment.current = true;
    if (generate) { setQuickBusy(true); setQuickError(''); }
    try {
      const quickInputs = generate ? { first_frame: quickTemplate === 'text-video' ? '' : segmentDraft.first_frame, last_frame: '', audio_guide: '', delivery_master: '', script_scene_id: '', script_revision: '', workflow: '', segment_id: '' } : {};
      const response = await fetch(`/api/projects/${encodeURIComponent(project.id)}/plan/segments`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ...segmentDraft, ...quickInputs, workflow: generate || autoCreation && !advancedOpen ? undefined : segmentDraft.workflow, generation: generate || autoCreation && !advancedOpen ? generationDraft : undefined, script_revision: undefined, script_scene_id: generate ? undefined : segmentDraft.script_scene_id || undefined, expected_script_revision: !generate && segmentDraft.script_scene_id ? Number(segmentDraft.script_revision) : undefined, expected_revision: planRevision, segment_id: generate ? undefined : segmentDraft.segment_id || undefined, duration_seconds: duration }) });
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
      if (generate) await startCurrentTask({ prompt: segmentDraft.prompt, duration }, { assetId: data.segment.id, prompt: segmentDraft.prompt, automatic: true });
    } catch (error) { if (activeProjectId.current === project.id) { const message = error instanceof Error ? error.message : '分段登记失败。'; setNotice(message); if (generate) setQuickError(message); } }
    finally { creatingSegment.current = false; if (generate) setQuickBusy(false); }
  }

  async function exportProjectBundle() {
    if (!project) return;
    try {
      const response = await fetch(`/api/projects/${encodeURIComponent(project.id)}/export`);
      if (!response.ok) throw new Error('项目导出失败');
      const data = await response.json();
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob); const link = document.createElement('a'); link.href = url; link.download = `${project.id}-director-project.json`; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
      setNotice('已导出项目清单、系列交接信息、分段计划、工作流快照和任务回执；媒体文件仍保留在 ComfyUI 工作区。');
    } catch (error) { setNotice(error instanceof Error ? error.message : '项目导出失败。'); }
  }

  function updatePipelineValue(id: string, value: string | number | boolean | string[]) {
    setDraftReadiness(null);
    setPipelineValues(previous => ({ ...previous, [id]: value }));
    if (id === 'prompt' && typeof value === 'string') updateDraft(current.id, { prompt: value });
    setPipelineValidation(null);
  }

  async function validatePipelineStage(stage: PipelineStage | undefined, announce = true, overrides: Record<string, string | number | boolean | string[]> = {}, target?: { assetId: string; automatic: boolean }): Promise<PipelineValidation | null> {
    if (!project?.id) return null;
    setPipelineChecking(true);
    setPipelineValidation(null);
    setDraftReadiness(null);
    setPipelineError('');
    try {
      const automatic = target?.automatic ?? generationEdit.mode === 'auto';
      const allowed = new Set(automatic ? ['prompt', 'duration', 'seed'] : (stage?.inputSpecs ?? []).map(input => input.id));
      const values = Object.fromEntries(Object.entries({ ...(target ? {} : pipelineValues), ...overrides }).filter(([id]) => allowed.has(id)));
      const response = await fetch(`/api/projects/${encodeURIComponent(project.id)}/pipeline/${encodeURIComponent(automatic ? 'auto' : stage!.id)}/validate`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ asset_id: target?.assetId ?? current.id, values }),
      });
      const data = await response.json();
      if (activeProjectId.current !== project.id || viewContext.current.assetId !== (target?.assetId ?? current.id)) return null;
      if (data.readiness || data.detail?.readiness) setDraftReadiness(data.readiness ?? data.detail.readiness);
      if (!response.ok) {
        const reasons = (data.detail?.blockers ?? data.detail?.readiness?.blockers ?? []).map(blockerMessage);
        const message = data.detail?.message || (typeof data.detail === 'string' ? data.detail : '参数校验失败，请查看准备度缺口。');
        throw new Error([message, ...reasons].filter((value, index, items) => items.indexOf(value) === index).join(' · '));
      }
      setPipelineValidation(data as PipelineValidation);
      setPipelineValues(previous => ({ ...previous, ...(data.values as Record<string, string | number | boolean | string[]>) }));
      if (announce) setNotice(`“${stage?.title ?? '自动制作方案'}”参数已通过预检，${data.binding_count ?? 0} 项输入会写入冻结 API 图。`);
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


  async function startCurrentTask(overrides: Record<string, string | number | boolean | string[]> = {}, target?: { assetId: string; prompt: string; automatic: boolean }) {
    if (!project?.id || submittingShot.current) return;
    const assetId = target?.assetId ?? current.id;
    if (!assetId) { setNotice('请先明确选择一个分镜，再提交生成。'); return; }
    const prompt = target?.prompt ?? currentPrompt;
    const automatic = target?.automatic ?? generationEdit.mode === 'auto';
    const storageKey = `director-workbench:${project.id}:${assetId}:pending-submission:v1`;
    submittingShot.current = true;
    let uncertain = false;
    try {
      const pending = localStorage.getItem(storageKey);
      let body = pending;
      if (!body) {
        if (!currentGenerationStage && !automatic) { setNotice('当前片段尚未登记可执行的制作步骤。'); return; }
        const validation = await validatePipelineStage(currentGenerationStage, false, { prompt: prompt || '', ...overrides }, target);
        if (!validation) return;
        const contractResponse = await fetch('/openapi.json');
        if (!contractResponse.ok) throw new Error('无法核对后端生成契约，请稍后重试。');
        const contract = await contractResponse.json();
        if (!contract.components?.schemas?.StartRequest?.properties?.idempotency_key) throw new Error('请更新工作台后端以启用生成请求去重。');
        if (activeProjectId.current !== project.id) return;
        body = JSON.stringify({ idempotency_key: crypto.randomUUID(), asset_id: assetId, prompt: prompt || undefined, expected_revision: validation.revision, pipeline_stage_id: automatic ? 'auto' : currentGenerationStage!.id, pipeline_values: validation.values });
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
      if (activeProjectId.current !== project.id) return;
      setProjectTasks(previous => [data as TaskRecord, ...previous.filter(task => task.id !== data.id)]);
      if (viewContext.current.assetId === assetId) { setCurrentTask(data as TaskRecord); setTaskState(normalizeLiveTaskState(data.status ?? '')); setTaskId(data.id); }
      setNotice(pending ? '已核对上次提交回执，没有创建重复任务。' : '已收到生成任务回执。');
    } catch (error) {
      if (activeProjectId.current !== project.id) return;
      if (viewContext.current.assetId === assetId) setTaskState('');
      setNotice(uncertain ? '提交结果尚未确认。原请求已保留；再次点击生成会核对同一请求，不会使用新参数重复提交。' : error instanceof Error ? error.message : '提交准备失败。');
    } finally { submittingShot.current = false; }
  }

  async function startFinishTask() {
    if (!project?.id || !currentPlanSegment || !currentSampleRecord || currentSampleRecord.status !== 'approved' || submittingShot.current) return;
    const storageKey = `director-workbench:${project.id}:${currentPlanSegment.id}:pending-finish:v1`;
    submittingShot.current = true;
    let uncertain = false;
    try {
      const saved = localStorage.getItem(storageKey);
      const body = saved || JSON.stringify({
        idempotency_key: crypto.randomUUID(), asset_id: currentPlanSegment.id,
        expected_plan_revision: planRevision, expected_review_revision: currentSampleRecord.revision,
        model_name: 'RealESRGAN_x2plus.pth', color_method: 'uniform', color_strength: 0.2,
      });
      if (!saved) localStorage.setItem(storageKey, body);
      uncertain = true;
      setTaskState('preparing');
      setNotice(saved ? '正在核对上次高清处理回执，沿用原请求。' : '正在冻结采用样片并提交 AI 高清候选任务。');
      const response = await fetch(`/api/projects/${encodeURIComponent(project.id)}/finish-tasks`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body });
      const data = await response.json();
      if (!response.ok) {
        if (response.status >= 400 && response.status < 500 && data.detail?.code !== 'idempotency_key_conflict') { localStorage.removeItem(storageKey); uncertain = false; }
        throw new Error(data.detail?.message || data.detail || '高清处理提交失败');
      }
      localStorage.removeItem(storageKey); uncertain = false;
      setProjectTasks(previous => [data as TaskRecord, ...previous.filter(task => task.id !== data.id)]);
      setCurrentTask(data as TaskRecord); setTaskId(data.id ?? ''); setTaskState(normalizeLiveTaskState(data.status ?? ''));
      setNotice(saved ? '已核对高清处理回执，没有创建重复任务。' : '已提交 AI 高清候选，完成后会作为 B 版本出现在当前片段。');
    } catch (error) {
      setTaskState('');
      setNotice(uncertain ? '高清处理提交结果尚未确认，原请求已保留；再次点击会核对同一请求。' : error instanceof Error ? error.message : '高清处理提交失败');
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
        if (!savedPlanCurrent || restoringVersion) { setNotice('正在核对当前计划，请完成后再提交装配。'); return; }
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
    const assetIds = selectedBatchAssetIds.filter(assetId => planSegments.some(segment => segment.id === assetId));
    if (!assetIds.length) { setNotice('请先明确选择要加入批次的分镜。'); return; }
    try {
      const generationStage = selectVideoGenerationStage(pipeline, project.production_preset?.workflow);
      if (!generationStage) { setNotice('当前项目没有可批量执行的视频制作步骤。'); return; }
      const response = await fetch(`/api/projects/${encodeURIComponent(project.id)}/batches`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ asset_ids: assetIds, pipeline_stage_id: generationStage.id, pipeline_values_by_asset: {} }) });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || '批次提交失败');
      setBatchId(data.batch_id ?? '');
      setBatchTasks((data.task_ids ?? []).map((id: string, index: number) => ({ id, asset_id: assetIds[index], status: 'batch_waiting', sequence: index + 1, batch_id: data.batch_id })));
      setNotice(`已提交明确选择的 ${assetIds.length} 个片段；每段按自己的计划和源工作流冻结参数。`);
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
  const quickShell = quickMode && workspaceMode === 'production' && ['shots', 'delivery', 'enhance'].includes(workspacePage) && !archive;
  const quickActive = quickShell && workspacePage === 'shots';
  const quickDelivery = quickShell && workspacePage === 'delivery';
  const adoptedShotCount = planSegments.filter(segment => {
    const review = projectState?.reviews[segment.id]?.finish ?? projectState?.reviews[segment.id]?.sample;
    return review?.status === 'approved' && review.source === 'explicit-adoption' && Boolean(review.adoption_binding);
  }).length;
  const assemblyTask = projectTasks.find(task => task.id === assemblyTaskId);
  const finalAdopted = !assemblyIsStale && projectState?.reviews[project?.assembly_asset_id ?? 'MASTER']?.final?.status === 'approved';
  const nextPlanAsset = currentPlanSegment ? planVideoAssets[planSegments.findIndex(segment => segment.id === current.id) + 1] : undefined;
  function openQuickDelivery() {
    setArchive(null); setWorkspaceMode('production'); setWorkspacePage('delivery');
    void refreshPlan().catch(() => setNotice('当前计划读取失败，请刷新后再提交装配。'));
  }
  function openQuickShot(id: string) { const asset = planVideoAssets.find(item => item.id === id); if (asset) { choose(asset); setWorkspacePage('shots'); } }
  const nativeVideoSize = (aspect: ShotGenerationSettings['aspect_ratio'], textToVideo = false) => textToVideo
    ? aspect === '16:9' ? '1344 × 768' : aspect === '9:16' ? '768 × 1344' : '1024 × 1024'
    : aspect === '16:9' ? '864 × 480' : aspect === '9:16' ? '480 × 864' : '640 × 640';
  const currentDisplayTitle = quickActive && kind === 'image' ? current.title.replace(/^关键帧 · /, '图片 · ') : assetTitle(current.id, current.title, kind);
  const downloadSource = kind === 'image' ? current.image : source;
  const downloadLabel = downloadSource ? downloadName(downloadSource) : undefined;
  return <div className={`director-workbench ${quickShell ? 'director-quick' : ''}`} data-build="2026-10-16-auth-cache-refresh" style={{ '--sidebar-width': `${sidebarWidth}px` } as CSSProperties}>
    <a className="skip-link" href="#director-main-content">跳到主内容</a>
    <WorkbenchSidebar projects={projectList} archive={archive} projectId={project?.id} planSegments={planSegments} assets={project?.assets ?? []} selectedKind={kind} selectedId={current.id} privateMode={privateMode} refreshKey={seriesRefreshKey} onOpenProject={openTreeProject} onOpenItem={openTreeItem} onOpenArchive={value => { setArchive(value); setCreateOpen(false); setProjectMenuOpen(false); }} onCreateWork={series => { setNewSeries(series); setNewTitle(''); setCreateOpen(true); setProjectMenuOpen(false); }} onDeleteDirectory={setDeleteDirectory} onRefreshProjects={() => { void refreshProjects(); if (project?.id) void refreshPlan().catch(() => setNotice('分镜读取失败，请稍后重试。')); }} comfyOnline={comfyOnline} comfyVersion={comfyInfo.version} onOpenMaterialBridge={() => setMaterialBridgeOpen(true)} onToggleResources={() => setResourceOpen(open => !open)} sidebarWidth={sidebarWidth} onBeginResize={beginSidebarResize} onResize={resizeSidebar} />
    <main id="director-main-content" className="director-main" tabIndex={-1}>
      <WorkbenchTopbar project={project} archive={archive} privateMode={privateMode} quickShell={quickShell} quickCategory={quickCategory} workspaceMode={workspaceMode} workspacePage={workspacePage} comfyOnline={comfyOnline} logoutBusy={logoutBusy} sessionError={sessionError} onLogout={onLogout} onQuickGenerate={() => { setQuickMode(true); setArchive(null); setWorkspaceMode('production'); setWorkspacePage('shots'); }} onWorkspace={() => { setQuickMode(false); setWorkspaceMode('production'); setWorkspacePage('shots'); }} onNavigate={page => { setArchive(null); setWorkspaceMode('production'); setWorkspacePage(page); if (page === 'creative') setCreativeOpen(true); }} onOpenReverse={() => { setArchive(null); setWorkspaceMode('reverse'); }} onToggleCreate={() => { setCreateOpen(open => !open); setProjectMenuOpen(false); }} onToggleImport={() => { setProjectMenuOpen(open => !open); setCreateOpen(false); }} />
      {privateMode && !quickShell && <Suspense fallback={null}><FirstUseGuide projectId={project?.id} refreshKey={`${planRevision}:${projectState?.history.length ?? 0}:${projectTasks.length}`} onAction={openGuideAction} onOpenProject={id => void selectProject(id)} /></Suspense>}
      {(createOpen || projectMenuOpen) && <ProjectActionsPopover mode={createOpen ? 'create' : 'import'} newSeries={newSeries} newTitle={newTitle} newCreationMode={newCreationMode} newReferenceProject={newReferenceProject} projectList={projectList} creatingProject={creatingProject} importFiles={importFiles} hasProject={Boolean(project)} folderInputRef={folderInputRef} onSeriesChange={setNewSeries} onTitleChange={setNewTitle} onCreationModeChange={setNewCreationMode} onReferenceProjectChange={setNewReferenceProject} onImportFiles={setImportFiles} onCreate={() => void createProject()} onUpload={() => void uploadSelectedFiles()} onClose={() => { setCreateOpen(false); setProjectMenuOpen(false); }} />}
      {resourceOpen && <ResourceStatusPanel comfyOnline={comfyOnline} comfyInfo={comfyInfo} onClose={() => setResourceOpen(false)} />}
      <div className="director-content" data-workspace-page={workspacePage}>
        {quickActive && <section className="quick-start" aria-label="快速生成">
          <header><span className="director-kicker">从一个画面开始</span><h1>{quickCategory === 'adult' ? '成人向创作区' : '今天想生成什么？'}</h1><p>{quickCategory === 'adult' ? '仅面向虚构成年人和非露骨成人题材；真人质感、成熟肖像与亲密氛围会单独保存在“成人向创作”系列。' : '选择模板，放入素材和提示词。结果会保存在当前工作台中。'}</p></header>
          <div className="quick-zone-switch" role="group" aria-label="快速生成分区"><button type="button" className={quickCategory === 'general' ? 'selected' : ''} onClick={() => { setQuickCategory('general'); setGenerationDraft(previous => ({ ...previous, creative_profile: 'general' })); }}>普通创作</button><button type="button" className={quickCategory === 'adult' ? 'selected' : ''} onClick={() => { setQuickCategory('adult'); setGenerationDraft(previous => ({ ...previous, creative_profile: adultProfile, speed_profile: 'standard' })); }}>成人向创作</button></div>
          {quickCategory === 'general' && <div className="quick-templates">
            <button disabled={quickBusy || projectLoading} aria-pressed={quickTemplate === 'image'} onClick={() => void openQuickTemplate('image')}><ImageIcon size={24} /><strong>文生图</strong><span>描述画面，生成一张图片</span></button>
            <button disabled={quickBusy || projectLoading} aria-pressed={quickTemplate === 'text-video'} onClick={() => void openQuickTemplate('text-video')}><Play size={24} /><strong>文生视频</strong><span>描述画面与动作，直接生成视频</span></button>
            <button disabled={quickBusy || projectLoading} aria-pressed={quickTemplate === 'video'} onClick={() => void openQuickTemplate('video')}><Film size={24} /><strong>图生视频</strong><span>放入图片，让画面动起来</span></button>
            <button disabled={quickBusy || projectLoading} onClick={() => void openQuickTemplate('enhance')}><Maximize2 size={24} /><strong>图像／视频清晰化</strong><span>先生成，再用 AI 提高分辨率</span></button>
          </div>}
          {quickCategory === 'adult' && <section className="quick-adult-zone" aria-label="成人向创作设置"><div className="quick-adult-note"><strong>当前质感</strong><span>真人质感优先；动漫质感目前只在关键帧图像生效</span></div><label>成人向视觉配置<select aria-label="成人向视觉配置" value={adultProfile} onChange={event => { const profile = event.target.value as typeof adultProfile; setAdultProfile(profile); setGenerationDraft(previous => ({ ...previous, creative_profile: profile, speed_profile: 'standard' })); }}><option value="adult-realism">真人质感 · 图像 Anything2Real，视频 H3 Realism People</option><option value="adult-anime">动漫质感 · 仅关键帧图像 Anime Consistency</option></select></label><div className="quick-templates quick-templates-adult"><button disabled={quickBusy || projectLoading} aria-pressed={quickTemplate === 'image'} onClick={() => void openQuickTemplate('image', '', 'adult')}><ImageIcon size={24} /><strong>成人向文生图</strong><span>虚构成年人肖像、时尚与成熟氛围</span></button><button disabled={quickBusy || projectLoading} aria-pressed={quickTemplate === 'text-video'} onClick={() => void openQuickTemplate('text-video', '', 'adult')}><Play size={24} /><strong>成人向文生视频</strong><span>真人质感动作与镜头表达</span></button><button disabled={quickBusy || projectLoading} aria-pressed={quickTemplate === 'video'} onClick={() => void openQuickTemplate('video', '', 'adult')}><Film size={24} /><strong>成人向图生视频</strong><span>让已审核的虚构成年人画面动起来</span></button><button disabled={quickBusy || projectLoading} onClick={() => void openQuickTemplate('enhance', '', 'adult')}><Maximize2 size={24} /><strong>图像／视频清晰化</strong><span>提高当前作品分辨率，不经过成人向配方</span></button></div><p className="quick-adult-boundary">只接收虚构成年人和非露骨内容；不会处理真人换脸、未成年人或露骨性行为。</p></section>}
          {reusableTemplates.length > 0 && <details className="quick-reusable-details"><summary>高级：复用工作流模板 <span>选用模板后仍可单独进行清晰化</span></summary><div className="quick-reusable-templates" aria-label="可复用模板">{reusableTemplates.map(template => { const missing = template.preset?.missing_models?.length ?? 0; return <button key={template.id} className={quickTemplate === template.id ? 'selected' : ''} disabled={quickBusy || projectLoading} onClick={() => void openQuickTemplate(template.id)}><strong>{template.title}</strong><small>{template.description}</small>{template.preset?.title && <small className="quick-template-recipe">执行方案：{template.preset.title}</small>}<em>{missing ? `缺少 ${missing} 个模型 · 选用后仍需补齐` : template.installed ? '当前作品已配置' : '可直接复用默认方案'}</em></button>; })}</div></details>}
          {quickError && <p role="alert">{quickError}</p>}
          {quickBusy && <p role="status" aria-live="polite">正在准备，请稍候…</p>}
          {project && (quickTemplate === 'video' || quickTemplate === 'text-video') && segmentFormOpen && <fieldset className="quick-video-form" disabled={quickBusy}>
            <legend>{quickTemplate === 'text-video' ? '用文字生成视频' : '让图片动起来'}</legend>
            {quickTemplate === 'video' && <><MaterialSelect excludedValues={diagnosticPaths} label="开始画面" kind="image" value={segmentDraft.first_frame} options={materialOptions} onChange={value => setSegmentDraft(previous => ({ ...previous, first_frame: value }))} onBrowse={() => setMaterialBridgeOpen(true)} />
            <label>上传图片<input type="file" accept="image/png,image/jpeg,image/webp" onChange={event => { const file = event.target.files?.[0]; if (file) void uploadQuickImage(file); event.target.value = ''; }} /></label></>}
            <label className="quick-wide">提示词<textarea rows={4} value={segmentDraft.prompt} onChange={event => setSegmentDraft(previous => ({ ...previous, prompt: event.target.value }))} placeholder="描述主体如何运动、镜头如何移动，以及需要保持的内容" /></label>
            <label>时长<select aria-label="视频时长" value={segmentDraft.duration_seconds} onChange={event => setSegmentDraft(previous => ({ ...previous, duration_seconds: event.target.value }))}>{[4, 5, 8, 10, 15].map(n => <option key={n} value={n}>{n} 秒</option>)}</select></label>
            <label>生成方案<select aria-label="视频生成方案" value={generationDraft.speed_profile} onChange={event => setGenerationDraft(previous => ({ ...previous, speed_profile: event.target.value as ShotGenerationSettings['speed_profile'] }))}><option value="standard">标准方案 · 约 1MP / 原生配置</option><option value="fast">快速预览 · 已验证 8 步 + Kitchen（仅 16:9）</option></select></label>
            {quickCategory === 'adult' && <p className="quick-adult-profile">{adultVideoProfileNote(adultProfile, generationDraft.aspect_ratio, generationDraft.speed_profile)}</p>}
            <ResolutionPicker label="视频" aspect={generationDraft.aspect_ratio} resolution={generationDraft.output_resolution} nativeSize={nativeVideoSize(generationDraft.aspect_ratio, quickTemplate === 'text-video')} onChange={(aspect_ratio, output_resolution) => setGenerationDraft(previous => ({ ...previous, aspect_ratio, output_resolution, speed_profile: aspect_ratio === '16:9' ? previous.speed_profile : 'standard' }))} />
            {quickTemplate === 'video' && segmentDraft.first_frame && firstFrameAspect && firstFrameAspect !== generationDraft.aspect_ratio && <p className="quick-aspect-mismatch" role="status" aria-live="polite">
              开始画面是{ASPECT_LABEL[firstFrameAspect]}（{firstFrameAspect}），当前视频画幅是{ASPECT_LABEL[generationDraft.aspect_ratio]}（{generationDraft.aspect_ratio}）：H3 会把首帧直接拉伸铺满，不裁切，画面比例会失真。
              <button type="button" className="text-button" onClick={() => adoptVideoAspect(firstFrameAspect)}>改用{ASPECT_LABEL[firstFrameAspect]}（{firstFrameAspect}）</button>
            </p>}
            <div className="quick-wide"><button className="button primary" disabled={!segmentDraft.prompt.trim() || quickTemplate === 'video' && !segmentDraft.first_frame} onClick={() => void createPlanSegment(true)}><Play size={15} />生成视频</button><button className="text-button" onClick={() => setSegmentFormOpen(false)}>返回已有片段</button></div>
          </fieldset>}
        </section>}
        {archive ? <Suspense fallback={null}><ArchiveBrowser archive={archive} /></Suspense> : privateMode && !project ? quickActive ? null : <section className="director-no-project"><FolderOpen size={38} /><h1>从一部作品开始</h1><p>左侧会按系列整理你能访问的全部作品。新作品创建后，分镜、声音和图片都可以从目录中展开查看。</p><button className="button primary" onClick={() => setCreateOpen(true)}>新建第一部作品</button></section> : workspaceMode === 'reverse' ? <Suspense fallback={null}><ReverseWorkbench key={project?.id} initialAnalysisId={referenceTarget?.projectId === project?.id ? referenceTarget?.analysisId : undefined} projectId={project?.id ?? ''} onConverted={count => { void refreshPlan(); setWorkspaceMode('production'); setWorkspacePage('shots'); setNotice(count ? `已从参考拆解新增 ${count} 个分镜草案，请准备本作关键帧和制作方案。` : '已打开对应制作草案，原有分镜与修改均已保留。'); }} /></Suspense> : <>
        <header className="director-heading"><div><span className="director-kicker">{project?.eyebrow ?? '导演工作区'}</span><h1>{workspacePage === 'script' ? '剧本与场次' : workspacePage === 'creative' ? '创作设定' : workspacePage === 'delivery' ? '任务与成片' : '分镜制作'}</h1><p>{workspacePage === 'script' ? '先读故事，再把每场戏安排成镜头。' : workspacePage === 'creative' ? '确定这部作品的起点、角色与表达。' : workspacePage === 'delivery' ? '运行批次，检查并导出成片。' : '选择一段，修改素材与提示词，然后生成。'}</p></div><div className="director-heading-actions" hidden={workspacePage !== 'delivery'}><button className="button secondary" onClick={exportPlan}><ArrowDownToLine size={14} />导出审阅计划</button><button className="button secondary" onClick={exportProjectBundle}><ArrowDownToLine size={14} />导出项目清单</button></div></header>
        <div className="director-status-line"><span className="status-pill"><span className="status-pill-dot" />{planSegments.length ? `${planSegments.length} 个分镜已登记` : `${library.length} 个素材已登记`}</span><span className="status-pill status-pill-muted">{assemblyIsStale ? '历史成片 · 需重新装配' : hasAssemblyCheckpoint ? '当前成片已生成' : hasAssemblyArtifact ? '历史成片可预览' : '等待各分镜生成视频'}</span>{assemblyAsset && <button className="text-button" onClick={() => { choose(assemblyAsset); setWorkspacePage('shots'); }}><Film size={13} />打开历史成片</button>}<span className="director-status-spacer" /><span className="director-save-status">{storageError || segmentStorageError ? '本机草稿缓存失败' : '编辑后请点击保存，未保存修改仅留在本机'}</span></div>
        {workspacePage === 'delivery' && !quickDelivery && projectTasks.map(task => <TaskQueueStatus key={task.id} task={task} />)}
        {workspacePage === 'delivery' && !quickDelivery && savedReadiness && <div className={`director-readiness ${savedReadiness.assembly.ready ? 'ready' : 'has-gaps'}`} aria-label="装配准备度"><strong>装配准备度 · revision {savedReadiness.assembly.revision}</strong><span>{savedReadiness.assembly.blockers.map(blockerMessage).join(' · ') || (savedReadiness.assembly.ready ? '准备完成，可明确提交装配。' : readinessStateLabel(savedReadiness.assembly.state))}</span>{savedReadiness.assembly.next_actions.map(action => <button key={action.id} className="text-button" onClick={() => performReadinessAction(action.id, true)}>{action.label}</button>)}</div>}
        {quickDelivery && project && <section className="quick-delivery" aria-label="装配与取得文件">
          <header><div><h1>装配与取得文件</h1><p>核对各段和接点，明确提交装配，再查看和下载成片候选。</p></div><button type="button" className="button secondary" onClick={() => setWorkspacePage('shots')}>返回分镜</button></header>
          <section aria-label="分镜采用进度"><h2>已采用 {savedPlanCurrent ? adoptedShotCount : '…'} / {planSegments.length} 段</h2><p>装配保留原片及候选。未采用或版本变化的分镜，请先打开核对。</p>
            <ol className="quick-delivery-shots">{planSegments.map(segment => { const review = projectState?.reviews[segment.id]?.finish ?? projectState?.reviews[segment.id]?.sample; const adopted = review?.status === 'approved' && review.source === 'explicit-adoption' && Boolean(review.adoption_binding); return <li key={segment.id}><span>{assetLabel(segment.id)} · {adopted ? '已明确采用' : segment.video?.path ? '等待明确采用' : '等待生成'}</span><button type="button" className="text-button" onClick={() => openQuickShot(segment.id)}>打开{assetLabel(segment.id)}</button></li>; })}</ol>
          </section>
          <Suspense fallback={null}><AssemblyEditPanel projectId={project.id} planRevision={planRevision} assemblyPath={planAssembly?.output} onPlanChanged={() => refreshPlan()} onBrowse={() => setMaterialBridgeOpen(true)} onOpenShot={openQuickShot} /></Suspense>
          <section className="quick-delivery-submit" aria-label="提交成片装配"><h2>提交装配</h2>
            {!savedReadiness || !savedPlanCurrent ? <p>正在核对当前计划的准备度…</p> : <><p>{assemblyReady ? '装配输入已准备好。' : '请先处理以下项目：'}</p>{savedReadiness.assembly.blockers.length > 0 && <ul>{savedReadiness.assembly.blockers.map((blocker, index) => <li key={index}>{blocker.dependency && `${blocker.dependency}：`}{blockerMessage(blocker)}</li>)}</ul>}</>}
            {adoptedShotCount < planSegments.length && <p>还有 {planSegments.length - adoptedShotCount} 段未采用；上方可以直接返回对应分镜。</p>}
            <div className="quick-result-actions"><button type="button" className="button primary" disabled={assemblySubmitting || (!hasPendingAssembly && (!savedPlanCurrent || Boolean(restoringVersion) || !planSegments.length || adoptedShotCount !== planSegments.length || !assemblyReady || activeTaskStates.includes(assemblyTaskState)))} onClick={() => void startAssembly()}>{hasPendingAssembly ? '沿用装配凭据重试' : assemblyIsStale || assemblyTaskState === 'succeeded' ? '重新装配成片' : '装配成片'}</button>
              {hasPendingAssembly && <button type="button" className="button secondary" disabled={assemblySubmitting} onClick={() => void startAssembly(true)}>核对装配提交</button>}
              {activeTaskStates.includes(assemblyTaskState) && <button type="button" className="button secondary" onClick={() => void stopAssembly()}>停止装配</button>}
              {!hasPendingAssembly && ['stopped', 'failed', 'needs_reconcile'].includes(assemblyTaskState) && <button type="button" className="button secondary" onClick={() => void resumeAssembly()}>{assemblyTaskState === 'needs_reconcile' ? '核对装配结果' : '恢复装配'}</button>}
              <button type="button" className="text-button" onClick={() => { void refreshPlan().catch(() => setNotice('刷新失败，请稍后重试。')); }}>刷新装配状态</button>
            </div>
            {assemblyTaskState && <p role="status" aria-live="polite" aria-label="装配任务状态">装配任务：{taskStateLabel(assemblyTaskState)}</p>}{assemblyTask && <TaskQueueStatus task={assemblyTask} />}{assemblyTask?.error && <p role="alert">{assemblyTask.error}</p>}
          </section>
          {assemblySource && <section className="quick-delivery-result" aria-label="成片文件"><h2>{assemblyIsStale ? '历史成片 · 需重新装配' : finalAdopted ? '成片已采用' : '成片候选 · 等待审核'}</h2><p>{assemblyIsStale ? '分镜或装配输入已变化；此文件对应旧计划，重新装配后再确认采用。' : '先播放检查画面、声音和段间连续性，再明确通过全片审核。下载不会代替审核。'}</p>
            <video key={assemblySource} src={assemblySource} controls playsInline preload="metadata" aria-label="装配成片预览" />
            <div className="quick-result-actions"><a className="button primary" href={assemblySource} download={new URL(assemblySource, window.location.href).searchParams.get('path')?.split(/[\\/]/).pop() || true}>{assemblyIsStale ? '下载历史成片' : finalAdopted ? '下载已采用成片' : '下载成片候选'}</a><button type="button" className="button secondary" onClick={() => void openQuickTemplate('enhance', new URL(assemblySource!, window.location.href).searchParams.get('path') || '')}>清晰化成片</button><button type="button" className="button secondary" onClick={() => { if (assemblyAsset) { choose(assemblyAsset); setWorkspacePage('shots'); } }}>查看成片并审核</button></div>
          </section>}
        </section>}
        {workspacePage === 'delivery' && !quickDelivery && project && <AssemblyEditPanel projectId={project.id} planRevision={planRevision} assemblyPath={planAssembly?.output} onPlanChanged={() => refreshPlan()} onBrowse={() => setMaterialBridgeOpen(true)} onOpenShot={id => openTreeItem('video', id)} />}
        {workspacePage === 'delivery' && !quickDelivery && project && <Suspense fallback={null}><MediaToolsPanel projectId={project.id} assets={[...new Map([...baseAssets, ...planVideoAssets, ...(assemblyAsset ? [assemblyAsset] : [])].map(a => [a.id, a])).values()].filter(a => (a.kind === 'audio' || a.kind === 'video') && Boolean(a.sources?.A || a.sources?.B)).map(a => ({ id: a.id, title: a.title, kind: a.kind }))} onBrowse={() => setMaterialBridgeOpen(true)} onComplete={() => {
          const completedProjectId = project.id;
          void fetch(`/api/projects/${encodeURIComponent(completedProjectId)}`).then(r => { if (!r.ok) throw new Error(); return r.json() as Promise<ProjectManifest>; }).then(data => setProject(previous => previous?.id === completedProjectId ? data : previous)).catch(() => { if (activeProjectId.current === completedProjectId) setNotice('媒体处理已完成，素材刷新失败，请重新选择作品。'); });
        }} /></Suspense>}
        {workspacePage === 'script' && project && <Suspense fallback={null}><ScriptWorkbench key={project.id} projectId={project.id} shots={planSegments} request={fetch} onCreateShot={(sceneId, revision) => { setSegmentDraft(previous => ({ ...previous, script_scene_id: sceneId, script_revision: String(revision) })); setWorkspacePage('shots'); setSegmentFormOpen(true); setStageFormOpen(false); }} onSelectShot={id => openTreeItem('video', id)} /></Suspense>}
        {workspacePage === 'creative' && <CreationRoutes onScript={() => setWorkspacePage('script')} mode={creativeDraft.creation_mode} segmentCount={planSegments.length} onMode={mode => setCreativeDraft(previous => ({ ...previous, creation_mode: mode }))} onReference={() => setWorkspaceMode('reverse')} onSettings={() => setCreativeOpen(open => !open)} onSegment={() => { setWorkspacePage('shots'); setSegmentFormOpen(true); setStageFormOpen(false); }} onStage={() => { setWorkspacePage('shots'); setStageFormOpen(true); setSegmentFormOpen(false); }} />}
        {workspacePage === 'creative' && creativeOpen && <section className="director-creation-panel" aria-label="创作意图与世界观"><div className="director-form-heading"><div><span className="director-kicker">人工草稿 · 可随时退回修改</span><h2>创作意图与身份锚点</h2></div><span>{projectState?.creative ? `服务器档案 revision ${projectState.creative.revision} · ${projectState.creative.status === 'approved' ? '已确认' : '待确认'}` : '尚未提交服务器档案'}</span></div><div className="director-creation-grid"><label>创作意图<textarea rows={3} value={creativeDraft.intent} onChange={event => setCreativeDraft(previous => ({ ...previous, intent: event.target.value }))} placeholder="这部作品希望观众感受到什么？"></textarea></label><label>世界观 / 规则<textarea rows={3} value={creativeDraft.world} onChange={event => setCreativeDraft(previous => ({ ...previous, world: event.target.value }))} placeholder="时间、地点、叙事规则和不能违背的约束"></textarea></label><label>角色与身份锚点<input value={creativeDraft.character} onChange={event => setCreativeDraft(previous => ({ ...previous, character: event.target.value }))} placeholder="外形、服装、身份连续性"></input></label><label>声音与配音设定<input value={creativeDraft.voice} onChange={event => setCreativeDraft(previous => ({ ...previous, voice: event.target.value }))} placeholder="声线、情绪、演唱或对白规则"></input></label><label>参考视频备注（可选）<input value={creativeDraft.reference_video} onChange={event => setCreativeDraft(previous => ({ ...previous, reference_video: event.target.value }))} placeholder="描述想借鉴的参考；导入和分析请使用参考拆解入口"></input></label><label>参考系列（可选）<textarea rows={2} value={creativeDraft.reference_series} onChange={event => setCreativeDraft(previous => ({ ...previous, reference_series: event.target.value }))} placeholder="系列名称、代表作品、角色关系与风格规则" /></label><label>保留什么 / 改编什么<textarea rows={2} value={creativeDraft.adaptation} onChange={event => setCreativeDraft(previous => ({ ...previous, adaptation: event.target.value }))} placeholder="保留的结构、节奏与角色特征；本集的新内容及需要避免的重复" /></label><label>导演备注<textarea rows={2} value={creativeDraft.notes} onChange={event => setCreativeDraft(previous => ({ ...previous, notes: event.target.value }))} placeholder="需要人工逐项确认的风险或取舍"></textarea></label></div><div className="director-form-actions"><span>创作方式与设定一起保存；未提交修改仅缓存在当前浏览器。</span><div className="director-creation-actions"><button className="button secondary compact" disabled={stateSaving} onClick={() => void saveCreativeSettings('pending_review')}>保存版本</button><button className="button primary compact" disabled={stateSaving} onClick={() => void saveCreativeSettings('approved')}>保存并确认</button></div></div></section>}
        {workspacePage === 'enhance' && project && <Suspense fallback={null}><EnhancementPanel key={project.id} projectId={project.id} initialSource={enhancementSource} onBack={() => setWorkspacePage('shots')} /></Suspense>}
        {workspacePage === 'shots' && !quickActive && segmentFormOpen && autoCreation && !advancedOpen && <section className="director-creation-panel" aria-label="添加分镜"><h2>描述这一段画面</h2><label>画面与动作描述<textarea rows={3} value={segmentDraft.prompt} onChange={event => setSegmentDraft(previous => ({ ...previous, prompt: event.target.value }))} placeholder="例如：旧物店中，女孩拿起一只怀表，听见远处的钟声" /></label><div className="director-creation-grid"><label>剧情时长（秒）<input type="number" min="4" max="15" step="0.1" value={segmentDraft.duration_seconds} onChange={event => setSegmentDraft(previous => ({ ...previous, duration_seconds: event.target.value }))} /></label><label>目标画幅<select value={generationDraft.aspect_ratio} onChange={event => setGenerationDraft(previous => ({ ...previous, aspect_ratio: event.target.value as ShotGenerationSettings['aspect_ratio'] }))}><option value="16:9">横屏 · 1344×768</option><option value="1:1">方形 · 1024×1024</option><option value="9:16">竖屏 · 768×1344</option></select></label></div><p>默认5秒横屏样片；文字生成使用标准采样和约1.0MP原生画布。保存后可按需增加开始画面或表演声音，预检不生成。</p><button className="button primary" onClick={() => void createPlanSegment()}>保存分镜</button><button className="button secondary" onClick={() => setSegmentFormOpen(false)}>收起</button></section>}
        {workspacePage === 'shots' && !quickActive && segmentFormOpen && (!autoCreation || advancedOpen) && <section className="director-creation-panel" aria-label="添加分镜"><div className="director-form-heading"><div><span className="director-kicker">人工阶段 · 分镜 / 节拍</span><h2>添加分镜</h2>{segmentDraft.script_scene_id && <p>所属场次：{segmentDraft.script_scene_id} · 请单独设置这个镜头的时长。<button className="text-button" onClick={() => setSegmentDraft(previous => ({ ...previous, script_scene_id: '', script_revision: '' }))}>解除关联</button></p>}</div><button className="icon-button" aria-label="关闭添加分镜" onClick={() => setSegmentFormOpen(false)}><X size={16} /></button></div><div className="director-creation-grid"><label>片段编号<input value={segmentDraft.segment_id} onChange={event => setSegmentDraft(previous => ({ ...previous, segment_id: event.target.value }))} placeholder="留空则自动编号"></input></label><label>时长（秒）<input type="number" min="0.1" step="0.1" value={segmentDraft.duration_seconds} onChange={event => setSegmentDraft(previous => ({ ...previous, duration_seconds: event.target.value }))}></input></label><label>场景<input value={segmentDraft.location} onChange={event => setSegmentDraft(previous => ({ ...previous, location: event.target.value }))} placeholder="允许原创场景"></input></label><label>景别<input value={segmentDraft.shot_size} onChange={event => setSegmentDraft(previous => ({ ...previous, shot_size: event.target.value }))}></input></label><label>镜头运动<input value={segmentDraft.camera} onChange={event => setSegmentDraft(previous => ({ ...previous, camera: event.target.value }))}></input></label><label>服装 / 造型<input value={segmentDraft.wardrobe} onChange={event => setSegmentDraft(previous => ({ ...previous, wardrobe: event.target.value }))}></input></label><label className="director-creation-wide">动作与表演<input value={segmentDraft.performance} onChange={event => setSegmentDraft(previous => ({ ...previous, performance: event.target.value }))} placeholder="表情、口型、手势、节拍动作"></input></label><label className="director-creation-wide">提示词草稿<textarea rows={3} value={segmentDraft.prompt} onChange={event => setSegmentDraft(previous => ({ ...previous, prompt: event.target.value }))} placeholder="先写导演意图，再决定模型和工作流"></textarea></label>{advancedOpen && <label>工作流引用（可选）<input value={segmentDraft.workflow} onChange={event => setSegmentDraft(previous => ({ ...previous, workflow: event.target.value }))} placeholder="ComfyUI JSON 路径"></input></label>}<MaterialSelect excludedValues={diagnosticPaths} label="首帧" kind="image" value={segmentDraft.first_frame} options={materialOptions} onChange={value => setSegmentDraft(previous => ({ ...previous, first_frame: value }))} onBrowse={() => setMaterialBridgeOpen(true)} /><MaterialSelect excludedValues={diagnosticPaths} label="尾帧" kind="image" value={segmentDraft.last_frame} options={materialOptions} onChange={value => setSegmentDraft(previous => ({ ...previous, last_frame: value }))} onBrowse={() => setMaterialBridgeOpen(true)} /><MaterialSelect excludedValues={diagnosticPaths} label="表演引导音频" kind="audio" value={segmentDraft.audio_guide} options={materialOptions} onChange={value => setSegmentDraft(previous => ({ ...previous, audio_guide: value }))} onBrowse={() => setMaterialBridgeOpen(true)} /><MaterialSelect excludedValues={diagnosticPaths} label="交付音频" kind="audio" value={segmentDraft.delivery_master} options={materialOptions} onChange={value => setSegmentDraft(previous => ({ ...previous, delivery_master: value }))} onBrowse={() => setMaterialBridgeOpen(true)} /></div><div className="director-form-actions"><span>保存后可直接在当前分镜下方修改和审核。</span><button className="button primary compact" onClick={() => void createPlanSegment()}>保存分镜计划</button></div></section>}
        {workspacePage === 'shots' && stageFormOpen && <section className="director-creation-panel" aria-label="添加制作步骤"><div className="director-form-heading"><div><span className="director-kicker">执行契约 · 可人工覆盖</span><h2>添加声音、图像或视频步骤</h2></div><button className="icon-button" aria-label="关闭添加步骤" onClick={() => setStageFormOpen(false)}><X size={16} /></button></div><div className="director-creation-grid"><label>步骤名称<input value={stageTitle} onChange={event => setStageTitle(event.target.value)} placeholder="例如：角色配音或动作设计"></input></label><label>执行方式<select value={stageBackend} onChange={event => setStageBackend(event.target.value)}><option>混合流程</option><option>ComfyUI 工作流</option><option>本地脚本</option></select></label><label className="director-creation-wide">这一步的目的<input value={stagePurpose} onChange={event => setStagePurpose(event.target.value)} placeholder="导演能判断输入和输出是否成立"></input></label><label>输入（逗号分隔）<input value={stageInputs} onChange={event => setStageInputs(event.target.value)} placeholder="角色设定, 音频, 提示词"></input></label><label>输出（逗号分隔）<input value={stageOutputs} onChange={event => setStageOutputs(event.target.value)} placeholder="候选素材"></input></label><label className="director-creation-wide">工作流或脚本引用（可选）<input value={stageWorkflow} onChange={event => setStageWorkflow(event.target.value)} placeholder="先保存契约，执行引用可后补"></input></label></div><div className="director-form-actions"><span>步骤登记后可在高级操作中查看。</span><button className="button primary compact" onClick={() => void createPipelineStage()}>保存制作步骤</button></div></section>}
        {workspacePage === 'shots' && project && advancedOpen && !planSegments.length && <Suspense fallback={null}><ProductionPresetPicker key={project.id} projectId={project.id} installed={project.production_preset?.id} onInstalled={refreshPresetSelection} /></Suspense>}
        {workspacePage === 'shots' && <>
        <div className="director-shot-toolbar"><div className="director-kind-tabs" role="group" aria-label="素材类型">{((quickActive ? ['video', 'image'] : ['video', 'audio', 'image']) as Kind[]).map(type => <button key={type} aria-pressed={kind === type} onClick={() => switchKind(type)}>{type === 'video' ? '分镜' : type === 'audio' ? '声音' : quickActive ? '图片' : '关键帧'} <span>{type === 'video' ? planVideoAssets.length : library.filter(asset => asset.kind === type).length}</span></button>)}</div><div className="director-creation-actions"><button className="button secondary compact" onClick={() => quickActive ? void openQuickTemplate('video') : setSegmentFormOpen(open => !open)}>+ 添加分镜</button>{advancedOpen && currentPlanSegment && generationEdit.mode !== 'auto' && <button className="button secondary compact" onClick={() => setGenerationEdit(previous => ({ ...previous, mode: 'auto' }))}>切换为自动创作（保存后生效）</button>}{advancedOpen && <button className="button secondary compact" onClick={() => setStageFormOpen(open => !open)}>高级制作步骤</button>}</div></div>
        {kind === 'audio' && project?.id && <Suspense fallback={null}><SpeechPanel key={project.id} projectId={project.id} options={materialOptions} onBrowse={() => setMaterialBridgeOpen(true)} onComplete={() => {
          const completedProjectId = project.id;
          void fetch(`/api/projects/${encodeURIComponent(completedProjectId)}`).then(response => {
            if (!response.ok) throw new Error('声音素材刷新失败');
            return response.json() as Promise<ProjectManifest>;
          }).then(data => setProject(previous => previous?.id === completedProjectId ? data : previous)).catch(() => {
            if (activeProjectId.current === completedProjectId) setNotice('声音已完成，素材刷新失败，请重新选择作品。');
          });
        }} /></Suspense>}
        {kind === 'audio' && project?.id && <Suspense fallback={null}><AudioEditPanel projectId={project.id}
          audioAssets={(project.assets ?? []).filter(asset => asset.kind === 'audio' && typeof asset.id === 'string').map(asset => ({ id: String(asset.id), title: String(asset.title ?? asset.id) }))}
          onBrowse={() => setMaterialBridgeOpen(true)} onComplete={() => {
            const completedProjectId = project.id;
            void fetch(`/api/projects/${encodeURIComponent(completedProjectId)}`).then(response => {
              if (!response.ok) throw new Error('音频候选刷新失败');
              return response.json() as Promise<ProjectManifest>;
            }).then(data => setProject(previous => previous?.id === completedProjectId ? data : previous)).catch(() => {
              if (activeProjectId.current === completedProjectId) setNotice('编辑已完成，素材刷新失败，请重新选择作品。');
            });
          }} /></Suspense>}
        {kind === 'image' && project?.id && <Suspense fallback={null}><KeyframePanel key={project.id} projectId={project.id} simple={quickActive} initialMode={quickActive ? 'text' : undefined} creativeProfile={quickCategory === 'adult' ? adultProfile : 'general'} candidateTaskId={typeof editableMaterial?.keyframe_task_id === 'string' ? editableMaterial.keyframe_task_id : undefined} options={materialOptions} onBrowse={() => setMaterialBridgeOpen(true)} onComplete={completedTaskId => {
          const completedProjectId = project.id;
          void fetch(`/api/projects/${encodeURIComponent(completedProjectId)}`).then(response => {
            if (!response.ok) throw new Error('关键帧素材刷新失败');
            return response.json() as Promise<ProjectManifest>;
          }).then(data => { if (activeProjectId.current !== completedProjectId) return; setProject(data); setSelected(`keyframe-${completedTaskId}`); }).catch(() => {
            if (activeProjectId.current === completedProjectId) setNotice('关键帧已完成，素材刷新失败，请重新选择作品。');
          });
        }} /></Suspense>}
        <section className="director-board" aria-label="分镜工作区" hidden={quickActive && !timelineAssets.length && !currentPlanSegment}>
          <div className="director-board-main">
            <div className="director-timeline-header"><div><h2>分镜时间线</h2></div><div className="director-timeline-tools"><span>{timelineCountLabel}</span><button className="button secondary compact" onClick={() => setPipelineOpen(open => !open)}><Workflow size={14} />{pipelineOpen ? '收起制作流程' : '制作流程'}</button></div></div>
            <details className="director-progress-details"><summary>阶段检查与确认</summary><div className="director-stage-rail" aria-label="当前片段生产阶段">{productionRail.map((step, stepIndex) => <div key={step.id} className={`director-stage-step ${step.state}`}><span className="director-stage-marker">{step.state === 'done' ? '✓' : String(stepIndex + 1).padStart(2, '0')}</span><div><strong>{step.label}</strong><small>{step.detail}</small>{['intent', 'character', 'scene', 'performance', 'storyboard', 'materials'].includes(step.id) && step.state !== 'waiting' ? <button className="director-stage-action" onClick={() => void saveCheckpoint(step.id, step.state === 'done' ? 'changes_requested' : 'approved')}>{step.state === 'done' ? '退回修改' : '人工确认'}</button> : <em>{step.mode === 'auto' ? '执行器阶段' : '等待上游'}</em>}</div></div>)}</div></details>
            <div className="director-timeline" role="list" aria-label="分镜片段列表">{timelineAssets.map((asset, assetIndex) => <button key={asset.id} role="listitem" className={`director-shot ${asset.id === current.id ? 'selected' : ''} ${!asset.ready ? 'pending' : ''}`} onClick={() => choose(asset)} aria-label={`选择 ${asset.id} ${asset.title}`}><div className="director-shot-thumb">{asset.image ? <img src={asset.image} alt="" /> : asset.kind === 'video' && asset.sources?.A ? <video src={asset.sources.A + "#t=0.1"} muted playsInline preload="metadata" aria-hidden="true" /> : <span>{asset.kind === 'audio' ? <Music2 size={20} /> : asset.kind === 'image' ? <ImageIcon size={20} /> : <Film size={20} />}</span>}<b>{asset.id === (assemblyAsset?.id ?? '') ? 'MASTER' : quickActive && kind === 'image' ? `图片 ${assetIndex + 1}` : asset.id}</b><em>{asset.start !== undefined ? `${(asset.end! - asset.start).toFixed(asset.end! - asset.start! < 2 ? 1 : 0)}s` : '参考'}</em></div><strong>{asset.title.replace(`${asset.id} · `, '')}</strong><small>{assetIndex === 0 && asset.id === assemblyAsset?.id ? '完整候选' : asset.subtitle.split(' · ')[0]}</small></button>)}</div>
            <div className="director-focus-heading"><strong>{currentDisplayTitle}</strong><span>{isProjectVideoMaterial ? '项目素材' : focusIndex >= 0 ? `${focusIndex + 1} / ${focusList.length}` : '尚无分镜'}</span></div>
            <div className={`director-focus-strip ${focusList.length <= 1 ? 'single' : focusIndex <= 0 ? 'no-previous' : focusIndex >= focusList.length - 1 ? 'no-next' : ''}`}>
              <button type="button" className="director-neighbor director-neighbor-previous" disabled={focusIndex <= 0} onClick={() => choose(focusList[focusIndex - 1])} aria-label="查看上一段">{focusList[focusIndex - 1]?.image ? <img src={focusList[focusIndex - 1].image} alt="" /> : focusList[focusIndex - 1]?.sources?.A ? <video src={focusList[focusIndex - 1].sources?.A + "#t=0.1"} muted playsInline preload="metadata" aria-hidden="true" /> : <Film size={24} />}<small>上一段</small><strong>{focusList[focusIndex - 1] ? kind === 'image' ? `图片 ${focusIndex}` : assetTitle(focusList[focusIndex - 1].id, focusList[focusIndex - 1].title, kind) : '没有上一段'}</strong></button>
             <section className="director-preview-area" aria-label="当前片段预览"><div className="director-preview-head"><div><span className="director-kicker">{isAssemblyAsset ? '完整成片' : isProjectVideoMaterial ? '项目视频素材' : `当前${kind === 'video' ? '分镜' : kind === 'audio' ? '音频' : '图片'}`}</span><h2>{currentDisplayTitle}</h2></div><div className="director-preview-meta"><span>{isProjectVideoMaterial ? '已登记素材' : current.ready ? '当前版本' : current.image ? '已准备首帧' : '待准备'}</span>{kind !== 'image' && <span>{range(current)}</span>}{current.sources?.B && <label className="candidate-picker">候选<select aria-label="候选版本" value={variant} onChange={event => { autoplayRef.current = false; setVariant(event.target.value as Variant); }}><option value="A">A · 当前样片</option><option value="B">{currentCandidateBIsFinish ? 'B · AI 高清候选' : 'B · 重做样片'}</option></select></label>}</div></div><div className="director-preview-stage"><div className={`director-media director-media-${kind}`}>{current.ready && kind === 'video' && <video key={source} ref={attachMedia} src={source} poster={current.image} {...mediaProps} aria-label={`${current.id} 视频预览`} />}{current.ready && kind === 'image' && <><img src={current.image} alt={current.title} /><button className="image-zoom icon-button" aria-label="放大当前图片" onClick={() => setLightbox(true)}><Maximize2 size={17} /></button></>}{current.ready && kind === 'audio' && <div className="director-audio-stage"><div className={`record ${playing ? 'record-playing' : ''}`}><div><Music2 size={30} /></div></div><span className="director-kicker">音频片段</span><h2>{currentDisplayTitle}</h2><p>当前音频</p><audio key={source} ref={attachMedia} src={source} {...mediaProps} aria-label={`${current.id} 音频预览`} /></div>}{!current.ready && current.image && <img className="director-unrendered-frame" src={current.image} alt={`${currentDisplayTitle}的首帧`} />}{!current.ready && <div className={`director-empty ${current.image ? 'has-first-frame' : ''}`}><strong>{current.image ? '首帧已准备，视频还没生成' : `这一段还没有${kind === 'audio' ? '音频' : kind === 'image' ? '图片' : '视频'}`}</strong><p>{kind === 'video' ? `已准备：${[current.image ? '首帧' : '', currentAudioSource ? '音频' : '', currentPrompt ? '提示词' : ''].filter(Boolean).join('、') || '可在下方填写提示词和素材'}。生成后会自动显示在这里。` : '可在下方准备素材并生成。'}</p></div>}</div>{current.ready && <span className="director-stage-label">{isProjectVideoMaterial ? '参考视频' : kind === 'video' ? '当前视频' : kind === 'audio' ? '当前音频' : '当前图片'}</span>}</div><div className={`director-transport ${quickActive && kind === 'image' ? 'quick-image-transport' : ''}`}><button className="icon-button" aria-label="上一段" disabled={focusIndex <= 0} onClick={() => choose(focusList[focusIndex - 1])}><ChevronLeft size={18} /></button><button className="director-play-button" aria-label={playing ? '暂停当前片段' : '播放当前片段'} disabled={playbackDisabled} title={current.ready && kind !== 'image' && mediaStatus === 'loading' ? '素材加载中…' : undefined} onClick={togglePlay}>{playing ? <Pause size={16} /> : <Play size={16} />}</button><button className="icon-button" aria-label="下一段" disabled={focusIndex < 0 || focusIndex >= focusList.length - 1} onClick={() => choose(focusList[focusIndex + 1])}><ChevronRight size={18} /></button><span>{current.ready && kind !== 'image' && mediaStatus === 'loading' ? '素材加载中…' : currentDisplayTitle}</span><label className="director-continuous"><input type="checkbox" checked={continuous} onChange={event => setContinuous(event.target.checked)} /><span />连续播放</label></div></section>
              <button type="button" className="director-neighbor director-neighbor-next" disabled={focusIndex < 0 || focusIndex >= focusList.length - 1} onClick={() => choose(focusList[focusIndex + 1])} aria-label="查看下一段">{focusList[focusIndex + 1]?.image ? <img src={focusList[focusIndex + 1].image} alt="" /> : focusList[focusIndex + 1]?.sources?.A ? <video src={focusList[focusIndex + 1].sources?.A + "#t=0.1"} muted playsInline preload="metadata" aria-hidden="true" /> : <Film size={24} />}<small>下一段</small><strong>{focusList[focusIndex + 1] ? kind === 'image' ? `图片 ${focusIndex + 2}` : assetTitle(focusList[focusIndex + 1].id, focusList[focusIndex + 1].title, kind) : '没有下一段'}</strong></button>
            </div>
            {quickActive && current.ready && <div className="quick-result-actions" aria-label="使用生成结果">
              {(kind === 'image' || kind === 'video') && downloadSource && <button className="button secondary" onClick={() => void openQuickTemplate('enhance', new URL(downloadSource, window.location.href).searchParams.get('path') || '')}>清晰化这个结果</button>}
                {kind === 'image' && <button className="button primary" onClick={() => void openQuickTemplate('video', String(editableMaterial?.image_path ?? current.sources?.A ?? ''))}>让这张图动起来</button>}
              {canReviewAsset && <button className="button adopt" disabled={stateSaving || Boolean(restoringVersion) || isAssemblyAsset && assemblyIsStale} onClick={() => void adoptCurrent()}><Check size={14} />{isAssemblyAsset ? '通过全片审核' : variant === 'B' && currentCandidateBIsFinish ? '采用精细版' : '采用当前候选'}</button>}
              <a className="button secondary" href={downloadSource} download={downloadLabel || true}>下载{isAssemblyAsset ? assemblyIsStale ? '历史成片' : '成片候选' : kind === 'image' ? '图片' : kind === 'audio' ? '音频' : '视频'}</a>
              <span>{currentReview}</span>
            </div>}
            {quickActive && kind === 'video' && currentPlanSegment && <section className="quick-continuation" aria-label="采用后继续"><div><strong>{hasApprovalCheckpoint ? '这一段已采用' : '先核对并采用这一段'}</strong><p>{hasApprovalCheckpoint ? '继续制作下一段，或核对整部作品并装配。' : '采用后可继续下一段；原作品和其他候选都会保留。'}</p></div><div className="quick-result-actions">
              {nextPlanAsset ? <button type="button" className="button primary" disabled={!hasApprovalCheckpoint || stateSaving || Boolean(restoringVersion)} onClick={() => openQuickShot(nextPlanAsset.id)}>继续下一段</button> : <><button type="button" className="button secondary" disabled={!hasApprovalCheckpoint || stateSaving || Boolean(restoringVersion)} onClick={() => void openQuickTemplate('text-video')}>添加下一段（文字）</button><button type="button" className="button secondary" disabled={!hasApprovalCheckpoint || stateSaving || Boolean(restoringVersion)} onClick={() => void openQuickTemplate('video')}>添加下一段（图片）</button></>}
              <button type="button" className="button secondary" onClick={openQuickDelivery}>检查并装配成片</button>
            </div></section>}
            {quickActive && isAssemblyAsset && <button type="button" className="button secondary" onClick={openQuickDelivery}>返回装配与下载</button>}
            {quickActive && current.ready && !isAssemblyAsset && (kind === 'image' || kind === 'video') && <section className="quick-candidate-meta" aria-label="当前候选信息">
              <div className="quick-candidate-meta-heading"><strong>{selectedCandidateLabel}{currentVersionNumber ? ` · v${currentVersionNumber}` : ''}</strong><span>{variant === 'B' && currentCandidateBIsFinish ? currentFinishReview : currentReview}</span></div>
              <dl>
                <div><dt>输出规格</dt><dd>{outputResolutionLabel(currentOutputFacts.resolution)}{currentOutputFacts.width && currentOutputFacts.height ? ` · ${currentOutputFacts.width} × ${currentOutputFacts.height}` : ''}</dd></div>
                <div><dt>处理方式</dt><dd>{outputMethodLabel(currentOutputFacts.method)}</dd></div>
                <div><dt>输入快照</dt><dd>提示词、素材与生成参数已冻结</dd></div>
              </dl>
              {currentOutputFacts.nativeOutput && <p>原生文件已保留，可在任务回执中核对；当前下载的是{outputMethodLabel(currentOutputFacts.method)}结果。</p>}
            </section>}
            {quickActive && kind === 'video' && currentPlanSegment && shotVersionsOwner === `${project?.id}:${current.id}` && shotVersions.length > 1 && <Suspense fallback={null}><CandidateComparison
              key={`${project?.id}:${current.id}`}
              busy={Boolean(restoringVersion)} loading={shotVersionsLoading} error={shotVersionsError}
              hasMore={shotVersions.length < shotVersionsTotal} onLoadMore={() => setShotVersionsOffset(shotVersions.length)}
              onRestore={id => { const version = shotVersions.find(item => item.task_id === id); if (version) void restoreShotVersion(version); }}
              candidates={shotVersions.map((version, index) => ({
                id: version.task_id, label: `v${shotVersionsTotal - index}`, mediaUrl: mediaUrl(version.video_path) ?? '',
                prompt: String(version.snapshot?.prompt ?? (version.snapshot?.execution_snapshot as Record<string, unknown> | undefined)?.prompt ?? '未登记提示词'),
                generatedAt: localSystemTime(version.generated_at),
                output: [outputResolutionLabel(version.processing?.resolution), version.processing?.width && version.processing?.height ? `${version.processing.width} × ${version.processing.height}` : '尺寸未登记', outputMethodLabel(version.processing?.method)].join(' · '),
                current: version.is_current, incomplete: version.snapshot_complete === false,
              }))}
            /></Suspense>}
            <section className="director-edit-sheet" aria-label="当前分镜修改" hidden={quickActive && kind === 'image'}>
              <div className="director-edit-heading"><div><span className="director-kicker">只编辑当前这一段</span><h2>{currentPlanSegment ? `${assetLabel(current.id, kind)}的内容` : isAssemblyAsset ? '完整成片' : editableMaterial ? '素材档案' : '请先添加分镜'}</h2>{currentPlanSegment?.script_scene_id && <><button className="text-button" onClick={() => setWorkspacePage('script')}>所属场次 {currentPlanSegment.script_scene_id} · 查看剧本</button><button className="text-button" disabled={scriptLinkSaving} onClick={() => void unlinkScriptScene()}>解除场次关联</button></>}</div><span>{isProjectVideoMaterial ? '素材已登记 · 未生成分镜' : taskState ? taskStateLabel(taskState) : current.ready ? '当前版本已生成' : current.image ? '已有首帧 · 视频待生成' : '视频待生成'}</span></div>
              {currentPlanSegment ? <>
                <div className="shot-edit-tabs" role="tablist" aria-label="分镜编辑栏目"><button type="button" role="tab" aria-selected={shotEditTab === 'materials'} onClick={() => setShotEditTab('materials')}>画面与声音</button><button type="button" role="tab" aria-selected={shotEditTab === 'prompt'} onClick={() => setShotEditTab('prompt')}>提示词 <span>{currentPrompt.length.toLocaleString('zh-CN')} 字</span></button><button type="button" role="tab" aria-selected={shotEditTab === 'history'} onClick={() => setShotEditTab('history')}>历史记录 <span>{shotVersionsTotal}</span></button></div>
                {shotEditTab === 'materials' && <div className="director-edit-grid" role="tabpanel">
                  {generationEdit.mode === 'auto' && <><label className="director-edit-wide">画面与动作描述<textarea rows={3} value={currentPrompt} onChange={event => { updateDraft(current.id, { prompt: event.target.value }); setSegmentEdit(previous => ({ ...previous, prompt: event.target.value })); }} placeholder="描述你想看到的画面、动作和声音" /></label><label className="director-edit-wide">生成方案<select aria-label="当前分镜生成方案" value={generationEdit.speed_profile} onChange={event => setGenerationEdit(previous => ({ ...previous, speed_profile: event.target.value as ShotGenerationSettings['speed_profile'] }))}><option value="standard">标准方案 · 原生配置</option><option value="fast">快速预览 · 已验证 8 步 + Kitchen（仅 16:9、无结束帧／表演音频）</option></select></label><ResolutionPicker label="视频" aspect={generationEdit.aspect_ratio} resolution={generationEdit.output_resolution} nativeSize={nativeVideoSize(generationEdit.aspect_ratio, isTextGeneration)} onChange={(aspect_ratio, output_resolution) => setGenerationEdit(previous => ({ ...previous, aspect_ratio, output_resolution, speed_profile: aspect_ratio === '16:9' ? previous.speed_profile : 'standard' }))} /></>}
                  {!autoCreation && <label className="director-edit-wide">导演意图<textarea rows={2} value={segmentEdit.performance} onChange={event => setSegmentEdit(previous => ({ ...previous, performance: event.target.value }))} placeholder="这一段要表达什么、人物怎样行动？" /></label>}
                  {quickActive && generationEdit.mode === 'auto' && <MaterialSelect excludedValues={diagnosticPaths} label="开始画面" kind="image" value={segmentEdit.first_frame} options={materialOptions} onChange={value => setSegmentEdit(previous => ({ ...previous, first_frame: value }))} onBrowse={() => setMaterialBridgeOpen(true)} />}
                  <details className="director-edit-more director-edit-wide" open={panelOpen('shot-controls', !autoCreation)}><summary onClick={rememberPanel('shot-controls')}>画面与声音控制（可选）</summary><div className="director-edit-grid">
                  {isTextGeneration && generationEdit.mode !== 'auto' ? <p className="director-edit-wide">当前方案仅用文字生成，无需首尾帧或表演音频。要控制起始画面，请明确选择图生方案；成片音频可在后期设置。</p> : <>{!(quickActive && generationEdit.mode === 'auto') && <MaterialSelect excludedValues={diagnosticPaths} label={generationEdit.mode === 'auto' ? '开始画面（可选）' : '首帧'} kind="image" value={segmentEdit.first_frame} options={materialOptions} onChange={value => setSegmentEdit(previous => ({ ...previous, first_frame: value }))} onBrowse={() => setMaterialBridgeOpen(true)} />}
                  <MaterialSelect excludedValues={diagnosticPaths} label={generationEdit.mode === 'auto' ? '结束画面（可选）' : '尾帧'} kind="image" value={segmentEdit.last_frame} options={materialOptions} onChange={value => setSegmentEdit(previous => ({ ...previous, last_frame: value }))} onBrowse={() => setMaterialBridgeOpen(true)} />
                  <MaterialSelect excludedValues={diagnosticPaths} label={generationEdit.mode === 'auto' ? '用于表演的声音（可选）' : '表演音频'} kind="audio" value={segmentEdit.audio_guide} options={materialOptions} onChange={value => setSegmentEdit(previous => ({ ...previous, audio_guide: value }))} onBrowse={() => setMaterialBridgeOpen(true)} /></>}
                  {generationEdit.mode === 'auto' && segmentEdit.audio_guide && <label>声音含义<select value={generationEdit.sound_mode} onChange={event => setGenerationEdit(previous => ({ ...previous, sound_mode: event.target.value as ShotGenerationSettings['sound_mode'] }))}><option value="performance_reference">表演参考 · 生成声音可能变化</option><option value="locked_dialogue">严格使用已有对白 · 待小样验证</option></select></label>}
                  <MaterialSelect excludedValues={diagnosticPaths} label="成片音频" kind="audio" value={segmentEdit.delivery_master} options={materialOptions} onChange={value => setSegmentEdit(previous => ({ ...previous, delivery_master: value }))} onBrowse={() => setMaterialBridgeOpen(true)} /></div></details>
                  <details className="director-edit-more director-edit-wide"><summary>场景、时长与镜头设置</summary><div className="director-segment-edit-grid"><label>时长（秒）<input type="number" min="0.1" step="0.01" value={segmentEdit.duration_seconds} onChange={event => setSegmentEdit(previous => ({ ...previous, duration_seconds: event.target.value }))} /></label><label>场景<input value={segmentEdit.location} onChange={event => setSegmentEdit(previous => ({ ...previous, location: event.target.value }))} /></label><label>景别<input value={segmentEdit.shot_size} onChange={event => setSegmentEdit(previous => ({ ...previous, shot_size: event.target.value }))} /></label><label>镜头<input value={segmentEdit.camera} onChange={event => setSegmentEdit(previous => ({ ...previous, camera: event.target.value }))} /></label><label>造型<input value={segmentEdit.wardrobe} onChange={event => setSegmentEdit(previous => ({ ...previous, wardrobe: event.target.value }))} /></label></div></details>
                </div>}
                {shotEditTab === 'prompt' && <div role="tabpanel"><Suspense fallback={null}><ShotPromptEditor key={currentPlanSegment.id} value={currentPrompt} savedValue={currentPlanSegment.prompt ?? ''} onChange={value => { updateDraft(current.id, { prompt: value }); setSegmentEdit(previous => ({ ...previous, prompt: value })); updatePipelineValue('prompt', value); }} /></Suspense></div>}
                {shotEditTab === 'history' && <div className="shot-version-list" role="tabpanel"><p>按生成时间查看候选及完整输入。新结果会显示为当前版本，采用仍需你确认；可以恢复旧版本后继续修改。</p>{shotVersionsError && <p role="alert">{shotVersionsError}</p>}{shotVersions.length ? shotVersions.map((version, versionIndex) => <article key={version.task_id} className={`shot-version ${version.is_current ? 'current' : ''}`}><div className="shot-version-preview"><video src={mediaUrl(version.video_path)} preload="metadata" controls aria-label={`${assetLabel(current.id, kind)}历史版本${versionIndex + 1}`} /></div><div className="shot-version-info"><strong>{version.is_current ? '当前版本' : `第 ${shotVersionsTotal - versionIndex} 次生成`}</strong><time dateTime={version.generated_at}>生成于 {localSystemTime(version.generated_at)}</time>{version.restored_at && <small>最近恢复：{localSystemTime(version.restored_at)}</small>}<small>{[version.snapshot?.location ? String(version.snapshot.location) : '场景未登记', version.snapshot?.duration_seconds ? String(version.snapshot.duration_seconds) + ' 秒' : '时长未登记', `${outputResolutionLabel(version.processing?.resolution)}${version.processing?.width && version.processing?.height ? ` · ${version.processing.width} × ${version.processing.height}` : ''}`, outputMethodLabel(version.processing?.method)].join(' · ')}{version.snapshot_complete === false ? ' · 旧任务信息不完整' : ''}</small><details><summary>查看当时的提示词与素材</summary><p>{String(version.snapshot?.prompt ?? (version.snapshot?.execution_snapshot as Record<string, unknown> | undefined)?.prompt ?? '未登记提示词')}</p><div className="shot-version-materials"><span>首帧{versionMaterial(version, 'first') ? <img src={mediaUrl(versionMaterial(version, 'first'))} alt="历史首帧" /> : '未登记'}</span><span>尾帧{versionMaterial(version, 'last') ? <img src={mediaUrl(versionMaterial(version, 'last'))} alt="历史尾帧" /> : '未登记'}</span><span>表演音频{versionMaterial(version, 'guide') ? <audio src={mediaUrl(versionMaterial(version, 'guide'))} controls preload="none" /> : '未登记'}</span></div></details>{!version.is_current && <button type="button" className="button secondary compact" disabled={Boolean(restoringVersion)} onClick={() => void restoreShotVersion(version)}>{restoringVersion === version.task_id ? '恢复中…' : '恢复为当前'}</button>}</div></article>) : !shotVersionsError && <p>还没有成功生成的历史版本。</p>}{shotVersions.length < shotVersionsTotal && <button type="button" className="button secondary compact" onClick={() => setShotVersionsOffset(shotVersions.length)}>查看更多历史</button>}</div>}
                <div className="director-edit-actions">
                  <button className="button secondary" onClick={() => void saveCurrentSegment()}>保存修改</button>
                  <button className="button primary" disabled={activeTaskStates.includes(taskState)} onClick={() => currentGenerationStage || generationEdit.mode === 'auto' ? void saveAndRegenerate() : openGenerationSettings()}><Play size={15} />{!currentGenerationStage && generationEdit.mode !== 'auto' ? '先选择生成方式' : current.ready ? '保存并重新生成' : '保存并生成这一段'}</button>
                  {currentGenerationStage && advancedOpen && <span className="director-muted" aria-label="当前生成方案">{currentGenerationStage.title}</span>}
                  <button className="button secondary" onClick={() => void checkPendingSubmission()}>核对上次提交</button>
                  {activeTaskStates.includes(taskState) && <button className="button secondary" onClick={() => void stopCurrentTask()}><Square size={14} />停止任务</button>}
                  <button className="button secondary" onClick={() => setAdvancedOpen(open => !open)}>{advancedOpen ? '收起更多操作' : '更多操作'}</button>
                </div>
                {supportsH3Duration && !quickActive && <section className="director-edit-wide director-operation-panel" aria-label="H3 镜头时长">
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
            {quickActive && currentPlanSegment && currentTask?.error && <p role="alert">{currentTask.error}</p>}
            {quickActive && currentPlanSegment && taskState === 'needs_reconcile' && <button className="button secondary" onClick={() => void resumeCurrentTask()}>核对原任务结果</button>}
            {quickActive && currentPlanSegment && ['failed', 'stopped'].includes(taskState) && <button className="button secondary" onClick={() => void resumeCurrentTask()}>按原快照重试任务</button>}
            {currentPlanSegment && pipelineError && <div className="director-readiness has-gaps" role="alert"><strong>预检未通过</strong><span>{pipelineError}</span><span>请处理上述原因后重新预检；本次未提交生成。</span></div>}
            {currentPlanSegment && <div className={`director-readiness ${currentReadiness?.ready ? 'ready' : 'has-gaps'}`}><strong>{currentReadiness?.ready ? '生成准备度已满足' : readinessGaps.length ? `准备度缺口 ${readinessGaps.length}` : readinessStateLabel(currentReadiness?.state)}</strong><button className="button secondary compact" disabled={pipelineChecking || !currentGenerationStage} onClick={() => performReadinessAction('validate')}>预检未保存输入</button><span>{readinessGaps.join(' · ') || `保存版 revision ${currentReadiness?.revision ?? planRevision} · ${readinessStateLabel(currentReadiness?.state)}`}</span>{currentReadiness?.next_actions.map(action => <button key={action.id} className="text-button" onClick={() => performReadinessAction(action.id)}>{action.label}</button>)}{draftReadiness && <div>未保存参数预检 · revision {draftReadiness.revision}：{draftReadiness.ready ? '可提交，仍需明确点击生成' : draftReadiness.blockers.map(blockerMessage).join(' · ') || readinessStateLabel(draftReadiness.state)}</div>}</div>}
            {!isProjectVideoMaterial && <div className="director-decision-strip" aria-label="当前片段审核门"><div><span>审核门</span><strong>{hasApprovalCheckpoint ? '已通过，可进入精细化' : hasGeneratedCheckpoint ? '先判断内容是否可用' : '生成前检查'}</strong></div><small>{kind === 'video' ? '低清样片只审画面、声音与动作' : '素材审核不会自动绑定到分镜'} · 当前：{currentReview}</small></div>}
          </div>
          <details className="director-advanced-details" hidden><summary>更多执行信息</summary>
          <aside className="director-inspector" aria-label="当前分镜详情"><div className="director-inspector-header"><div><span className="director-kicker">当前片段</span><h2>{assetLabel(current.id, kind)} <small>{currentReview}</small></h2></div><button className="icon-button" aria-label="刷新当前项目" onClick={() => { void refreshPlan(); }}><Radio size={16} /></button></div><div className="director-inspector-actions"><button className="button secondary compact" onClick={() => void checkPendingSubmission()}>核对提交回执</button><button className="button primary" disabled={!currentExecutable || !currentGenerationStage || Boolean(taskState && !['succeeded', 'failed', 'stopped', 'needs_reconcile'].includes(taskState)) || !current.id} onClick={() => void startCurrentTask()}><Play size={14} />{activeTaskStates.includes(taskState) ? taskStateLabel(taskState) : current.ready ? '重做当前片段' : '启动当前片段'}</button>{['preparing', 'submitting', 'queued', 'scheduler_waiting', 'running', 'stop_requested', 'stopping'].includes(taskState) && <button className="button secondary" onClick={stopCurrentTask}><Square size={14} />停止</button>}{['stopped', 'needs_reconcile'].includes(taskState) && <button className="button secondary" onClick={resumeCurrentTask}><Play size={14} />恢复</button>}<button className="button adopt" onClick={adoptCurrent} disabled={!current.ready || (isAssemblyAsset && assemblyIsStale) || (variant === 'B' && currentCandidateBIsFinish && !hasApprovalCheckpoint)}><Check size={14} />{variant === 'B' && currentCandidateBIsFinish ? '采用精细版' : isAssemblyAsset ? '通过全片审核' : kind === 'video' ? '采用样片' : '采用素材候选'}</button></div><div className="director-task-status"><span className={taskState === 'succeeded' ? 'task-success' : taskState === 'failed' ? 'task-failed' : ''} />任务状态 <strong>{taskState ? taskStateLabel(taskState) : current.ready ? '已有结果' : '尚未提交'}</strong>{currentTask?.queue && <span>{currentTask.queue.reason?.message || currentTask.queue.state}{currentTask.queue.position != null && ` · 队列位置 ${currentTask.queue.position}`}</span>}{taskId && <small>{taskId}</small>}</div>{!currentExecutable && !isAssemblyAsset && <p className="director-next-step">尚未登记制作方案。先添加制作步骤与素材，再开始生成。</p>}{taskId && ['stopped', 'failed', 'needs_reconcile'].includes(taskState) && <div className="director-panel-actions"><button className="button secondary compact" onClick={() => void resumeCurrentTask()}>{taskState === 'needs_reconcile' ? '核对原任务结果' : '恢复原任务'}</button>{taskState === 'needs_reconcile' && currentTask?.payload?.kind !== 'speech' && <button className="button secondary compact" onClick={() => void resolveMissingExecution(taskId)}>确认执行已结束</button>}</div>}<div className="director-inspector-tabs" role="tablist" aria-label="片段档案栏目">{[['intent', '导演意图'], ['materials', '画面与音频'], ['params', '生成参数'], ['history', '历史版本']].map(([id, label]) => <button key={id} role="tab" aria-selected={inspectorTab === id} className={inspectorTab === id ? 'active' : ''} onClick={() => setInspectorTab(id as typeof inspectorTab)}>{label}</button>)}</div>{inspectorTab === 'intent' && <div className="director-inspector-body">{currentPlanSegment?.visual_review?.reason && <section><h3>持久化视觉审核</h3><p className="director-intent">{currentPlanSegment.visual_review.reason}</p></section>}<section><h3>导演意图</h3><p className="director-intent">{current.intent || '暂无导演意图。'}</p></section><section><label htmlFor={`director-prompt-${current.id}`}>提示词</label><textarea id={`director-prompt-${current.id}`} rows={6} value={currentPrompt} placeholder="描述画面、动作、表情和镜头意图。" onChange={event => { updateDraft(current.id, { prompt: event.target.value }); updatePipelineValue('prompt', event.target.value); }} /></section><section><label htmlFor={`director-note-${current.id}`}>审核意见</label><textarea id={`director-note-${current.id}`} rows={4} value={currentNote} placeholder="记录为什么保留、采用或需要重做。" onChange={event => updateDraft(current.id, { note: event.target.value })} /></section><div className="director-intent-meta"><span>{currentPlanSegment?.shot_size ?? '素材类型'}</span><span>{currentPlanSegment?.camera ?? current.specs}</span><span>{currentPlanSegment?.wardrobe ?? '版本档案'}</span></div></div>}{inspectorTab === 'materials' && <div className="director-inspector-body"><section><h3>首尾帧</h3><div className="director-frame-pair">{current.image ? <figure><img src={current.image} alt={`${current.id} 首帧`} /><figcaption>首帧参考</figcaption></figure> : <div className="director-frame-empty">暂无首帧</div>}{current.lastImage ? <figure><img src={current.lastImage} alt={`${current.id} 尾帧`} /><figcaption>尾帧目标</figcaption></figure> : <div className="director-frame-empty">暂无尾帧</div>}</div></section><section><h3>音频素材</h3>{currentAudioSource ? <audio src={currentAudioSource} controls preload="metadata" /> : <p className="director-muted">当前片段没有独立音频文件，使用项目完整母版时间轴。</p>}</section><section><h3>工作流与来源</h3><p className="director-mono">{current.workflow || '尚未登记'}</p>{workflowEntriesForAsset(current).length > 0 && <div className="director-workflow-links">{workflowEntriesForAsset(current).map(entry => <a key={`${entry.id}-${entry.path}`} href={workflowUrl(entry.path)} download={entry.path.split(/[\\/]/).pop()}><ArrowDownToLine size={13} />{entry.comfyui_import === 'ui' ? 'UI 工作流' : 'API 配方'}</a>)}</div>}</section></div>}{inspectorTab === 'params' && <div className="director-inspector-body"><section><h3>当前规格</h3><dl className="director-fact-list"><div><dt>时长</dt><dd>{range(current)}</dd></div><div><dt>规格</dt><dd>{current.specs || '尚未登记'}</dd></div><div><dt>来源</dt><dd>{current.origin || '尚未登记'}</dd></div></dl></section><section><h3>执行入口</h3><p className="director-muted">节点、脚本和模型仍由 ComfyUI 执行，工作台只保存本段的参数档案和任务回执。</p><button className="button secondary pipeline-drawer-button" onClick={() => setPipelineOpen(true)}><Workflow size={14} />打开当前片段 Pipeline</button></section></div>}{inspectorTab === 'history' && <div className="director-inspector-body"><section><h3>历史版本</h3><div className="director-history-row"><span className="history-version active">A</span><div><strong>{candidateA ? '当前候选已存在' : 'A 版本待生成'}</strong><small>{current.origin || '尚未登记来源'}</small></div></div><div className="director-history-row"><span className="history-version">B</span><div><strong>{candidateB ? currentCandidateBIsRework ? '重做样片 · 待审核' : `精细化候选 · ${currentFinishReview}` : '尚未生成 B 版本'}</strong><small>{candidateB ? currentCandidateBIsRework ? '与原样片同规格，只判断内容是否可用' : '样片采用后才能采用精细版' : '重做时可保存为新版本'}</small></div></div></section><section><h3>持久化审核</h3><p className="director-muted">{currentSampleRecord ? `${currentReview} · revision ${currentSampleRecord.revision} · ${currentSampleRecord.source}` : '尚无审核 revision；请使用下方审核与版本档案提交决定。'}</p></section><section><h3>任务尝试</h3>{currentAttempts.length ? currentAttempts.slice(0, 4).map(task => <div className="director-history-row" key={task.id}><span className="history-version">{taskStateLabel(task.status).slice(0, 2)}</span><div><strong>{taskStateLabel(task.status)}</strong><small>{task.id}</small></div></div>) : <p className="director-mono">尚无当前片段任务</p>}</section></div>}</aside>
          </details>
        </section>

        </>}
        {!quickDelivery && workspacePage !== 'creative' && (workspacePage === 'delivery' || advancedOpen) && <section className="director-operations" aria-label="当前片段生产控制">
          <div className="director-operations-head"><div><span className="director-kicker">当前片段控制</span><h2>{workspacePage === 'delivery' ? '批次与交付' : `${current.id} · 分镜详情`}</h2></div><span>{currentExecutable ? '执行入口已登记' : isAssemblyAsset ? '完整版仅使用装配入口' : '请先在「制作步骤」登记制作方案'}</span></div>
          <div className="director-operations-grid">
            {currentPlanSegment && <ReferenceOrigin segment={currentPlanSegment} onOpen={analysisId => { if (project) setReferenceTarget({ projectId: project.id, analysisId }); setWorkspaceMode('reverse'); }} />}
            {currentPlanSegment && project && <CreativeInspectionPanel key={`${project.id}:${currentPlanSegment.id}`} projectId={project.id} segmentId={currentPlanSegment.id} revision={planRevision} onPlanChanged={() => refreshPlan()} />}
            {currentPlanSegment && <details className="director-operation-panel"><summary className="director-panel-title"><div><h3>分镜计划</h3><small>修改后，相关阶段需要重新确认</small></div><span>{currentPlanSegment.dependencies?.length ? `依赖 ${currentPlanSegment.dependencies.join(', ')}` : '无上游依赖'}</span></summary><div className="director-segment-edit-grid"><label>时长（秒）<input type="number" min="0.1" step="0.01" value={segmentEdit.duration_seconds} onChange={event => setSegmentEdit(previous => ({ ...previous, duration_seconds: event.target.value }))} /></label><label>场景<input value={segmentEdit.location} onChange={event => setSegmentEdit(previous => ({ ...previous, location: event.target.value }))} /></label><label>景别<input value={segmentEdit.shot_size} onChange={event => setSegmentEdit(previous => ({ ...previous, shot_size: event.target.value }))} /></label><label>镜头<input value={segmentEdit.camera} onChange={event => setSegmentEdit(previous => ({ ...previous, camera: event.target.value }))} /></label><label>造型<input value={segmentEdit.wardrobe} onChange={event => setSegmentEdit(previous => ({ ...previous, wardrobe: event.target.value }))} /></label><label className="wide">动作表演<textarea rows={2} value={segmentEdit.performance} onChange={event => setSegmentEdit(previous => ({ ...previous, performance: event.target.value }))} /></label><label className="wide">提示词<textarea rows={4} value={currentPrompt} onChange={event => { updateDraft(current.id, { prompt: event.target.value }); setSegmentEdit(previous => ({ ...previous, prompt: event.target.value })); }} /></label><label className="wide">工作流引用<input value={segmentEdit.workflow} placeholder="workflows/...json" onChange={event => setSegmentEdit(previous => ({ ...previous, workflow: event.target.value }))} /></label><MaterialSelect excludedValues={diagnosticPaths} label="首帧" kind="image" value={segmentEdit.first_frame} options={materialOptions} onChange={value => setSegmentEdit(previous => ({ ...previous, first_frame: value }))} onBrowse={() => setMaterialBridgeOpen(true)} /><MaterialSelect excludedValues={diagnosticPaths} label="尾帧" kind="image" value={segmentEdit.last_frame} options={materialOptions} onChange={value => setSegmentEdit(previous => ({ ...previous, last_frame: value }))} onBrowse={() => setMaterialBridgeOpen(true)} /><MaterialSelect excludedValues={diagnosticPaths} label="表演引导音频" kind="audio" value={segmentEdit.audio_guide} options={materialOptions} onChange={value => setSegmentEdit(previous => ({ ...previous, audio_guide: value }))} onBrowse={() => setMaterialBridgeOpen(true)} /><MaterialSelect excludedValues={diagnosticPaths} label="交付音频" kind="audio" value={segmentEdit.delivery_master} options={materialOptions} onChange={value => setSegmentEdit(previous => ({ ...previous, delivery_master: value }))} onBrowse={() => setMaterialBridgeOpen(true)} /><label className="wide">依赖片段<input value={segmentEdit.dependencies} placeholder="例如 S01, S02" onChange={event => setSegmentEdit(previous => ({ ...previous, dependencies: event.target.value }))} /></label></div><div className="director-panel-actions"><button className="button primary compact" onClick={() => void saveCurrentSegment()}>保存分镜 revision</button><button className="button secondary compact" onClick={() => void operateCurrentSegment('copy')}>复制</button><button className="button secondary compact" disabled={index <= 0} onClick={() => void operateCurrentSegment('move', { before_id: planSegments[index - 1]?.id })}>前移</button><button className="button secondary compact" disabled={index < 0 || index >= planSegments.length - 1} onClick={() => void operateCurrentSegment('move', { before_id: planSegments[index + 2]?.id ?? '__end__' })}>后移</button><button className="button secondary compact" onClick={() => void operateCurrentSegment('split', { split_at_seconds: currentPlanSegment.duration_seconds / 2 })}>中点拆分</button><button className="button secondary compact" disabled={index >= planSegments.length - 1} onClick={() => void operateCurrentSegment('merge', { target_id: planSegments[index + 1]?.id })}>合并下一段</button></div>{index >= 0 && index < planSegments.length - 1 && <div className="director-panel-actions"><label>连续合并至<select aria-label="连续合并至" value={mergeEnd} onChange={event => setMergeEnd(event.target.value)}><option value="">选择末段</option>{planSegments.slice(index + 1).map(segment => <option key={segment.id} value={segment.id}>{segment.id} · {segment.location}</option>)}</select></label><button className="button secondary compact" disabled={!mergeEnd || !planSegments.slice(index + 1).some(segment => segment.id === mergeEnd)} onClick={() => { if (window.confirm(`将 ${currentPlanSegment.id} 至 ${mergeEnd} 的连续分镜合并。已有候选将失效，来源历史会保留。确认合并？`)) void operateCurrentSegment('merge_range', { target_id: mergeEnd }); }}>合并所选范围</button><small>已有候选会失效，保留来源历史。</small></div>}</details>}

            {currentPlanSegment && <button type="button" className="director-shot-delete" onClick={() => setDeleteShotOpen(true)}>删除当前分镜</button>}
            {reviewConflictPanel}
            {currentPlanSegment && hasApprovalCheckpoint && <section className="director-finish-action" aria-label="AI 高清处理"><div><strong>样片已采用</strong><p>保留原片 A，生成独立的 AI 高清候选 B，完成后可在版本比较中审核。</p></div><button className="button secondary compact" disabled={taskIsActive || submittingShot.current} onClick={() => void startFinishTask()}><Sparkles size={14} />{currentCandidateBIsFinish ? '重新生成 AI 高清' : '生成 AI 高清候选'}</button></section>}<details className="director-operation-panel" hidden><summary className="director-panel-title"><div><h3>审核与版本档案</h3><small>保存审核结论，保留历史版本</small></div><span>{currentSampleRecord ? `${kind === 'video' ? '样片' : '素材候选'} revision ${currentSampleRecord.revision}` : kind === 'video' ? '尚无样片审核档案' : '尚无素材审核档案'}</span></summary><label className="director-review-note">审核意见<textarea rows={3} value={currentNote} placeholder="记录画面、声音、动作或节奏问题" onChange={event => updateDraft(current.id, { note: event.target.value })} /></label><div className="director-panel-actions"><button className="button secondary compact" disabled={stateSaving || !current.ready} onClick={() => void persistReview(isAssemblyAsset ? 'final' : 'sample', 'pending_review')}>保存意见</button><button className="button secondary compact" disabled={stateSaving || !current.ready} onClick={() => void persistReview(isAssemblyAsset ? 'final' : 'sample', 'changes_requested')}>退回修改</button><button className="button adopt compact" disabled={stateSaving || !current.ready || (isAssemblyAsset && assemblyIsStale)} onClick={() => void adoptCurrent()}><Check size={13} />{isAssemblyAsset ? '通过全片审核' : '采用当前候选'}</button></div><div className="director-review-facts"><span>当前采用：{currentSampleRecord?.adopted_variant ?? '无'}</span><span>精细版：{currentFinishRecord ? `${currentFinishReview} · r${currentFinishRecord.revision}` : '无档案'}</span><span>历史尝试：{currentAttempts.length}</span></div></details>

            {(pipelineOpen || inspectorTab === 'params') && activePipelineStage && <article className="director-operation-panel director-pipeline-controls">
              <div className="director-panel-title"><div><h3>{activePipelineStage.title} · 实际输入</h3><small>{activePipelineStage.execution?.label ?? activePipelineStage.backend}</small></div><button className="button secondary compact" onClick={() => void validatePipelineStage(activePipelineStage)} disabled={pipelineChecking}>{pipelineChecking ? '预检中…' : '预检参数'}</button></div>
              <div className="director-segment-edit-grid">{(activePipelineStage.inputSpecs ?? []).map(input => {
                const value = pipelineValues[input.id] ?? input.example ?? (input.control === 'multi-select' ? [] : '');
                const values = Array.isArray(value) ? value : [];
                return <label key={input.id} className={input.control === 'text' || input.control === 'freeform' ? 'wide' : ''}><span>{input.label}{input.required ? ' *' : ''}</span>{(input.control === 'select' || input.control === 'boolean') && <select value={String(value)} onChange={event => updatePipelineValue(input.id, event.target.value)}>{(input.options ?? (input.control === 'boolean' ? ['enable', 'disable'] : [])).map(option => <option key={option}>{option}</option>)}</select>}{input.control === 'number' && <input type="number" min={input.min} max={input.max} step={input.step} value={String(value)} onChange={event => updatePipelineValue(input.id, event.target.value)} />}{input.control === 'asset' && <select value={String(value)} onChange={event => updatePipelineValue(input.id, event.target.value)}><option value="">选择已登记{input.kind}</option>{pipelineAssetOptions(input).map(option => <option key={option.value} value={option.value}>{option.label}</option>)}</select>}{input.control === 'multi-select' && <span className="director-check-list">{(input.options ?? []).map(option => <label key={option}><input type="checkbox" checked={values.includes(option)} onChange={event => updatePipelineValue(input.id, event.target.checked ? [...values, option] : values.filter(item => item !== option))} />{option}</label>)}</span>}{(!input.control || input.control === 'text' || input.control === 'freeform' || input.control === 'time-range') && <textarea rows={2} value={String(value)} onChange={event => updatePipelineValue(input.id, event.target.value)} />}</label>;
              })}</div>
              {pipelineValidation?.stage_id === activePipelineStage.id && <div className="director-validation"><strong>预检通过 · {pipelineValidation.binding_count ?? 0} 项会写入 API 图</strong><code>{pipelineValidation.args.join(' ') || 'ComfyUI 节点绑定已确认'}</code></div>}
            </article>}

            <article className="director-operation-panel director-delivery-panel"><div className="director-panel-title"><div><h3>批次与装配</h3><small>停止保留已完成片段；恢复从原冻结快照的新尝试开始</small></div><span>{batchTasks.length ? `${batchTasks.filter(task => task.status === 'succeeded').length}/${batchTasks.length}` : '未运行批次'}</span></div><fieldset className="director-batch-selection"><legend>明确选择批次分镜</legend>{planSegments.length ? planSegments.map(segment => <label key={segment.id}><input type="checkbox" checked={selectedBatchAssetIds.includes(segment.id)} onChange={event => setSelectedBatchAssetIds(current => event.target.checked ? [...current, segment.id] : current.filter(id => id !== segment.id))} />{segment.id} · {segment.location}</label>) : <span>尚无分镜</span>}</fieldset><div className="director-panel-actions"><button className="button secondary compact" disabled={batchActive} onClick={startBatch}><Play size={13} />提交已选择分镜批次</button>{batchActive && <button className="button secondary compact" onClick={stopBatch}><Square size={13} />停止批次</button>}{batchId && !batchActive && batchTasks.some(task => task.status !== 'succeeded') && <button className="button secondary compact" onClick={resumeBatch}><Play size={13} />恢复批次</button>}<button className="button primary compact" disabled={assemblySubmitting || (!hasPendingAssembly && (!assemblyReady || activeTaskStates.includes(assemblyTaskState)))} onClick={() => void startAssembly()}><Layers3 size={13} />{hasPendingAssembly ? '沿用装配凭据重试' : assemblyTaskState === 'succeeded' ? '重新装配' : '启动装配'}</button>{hasPendingAssembly && <button className="button secondary compact" disabled={assemblySubmitting} onClick={() => void startAssembly(true)}>核对装配提交</button>}{activeTaskStates.includes(assemblyTaskState) && <button className="button secondary compact" onClick={stopAssembly}><Square size={13} />停止装配</button>}{!hasPendingAssembly && ['stopped', 'failed', 'needs_reconcile'].includes(assemblyTaskState) && <button className="button secondary compact" onClick={resumeAssembly}><Play size={13} />{assemblyTaskState === 'needs_reconcile' ? '核对装配结果' : '恢复装配'}</button>}</div><div className="director-review-facts"><span>已有视频 {planSegments.filter(segment => Boolean(segment.video?.path)).length}/{planSegments.length}</span><span>装配使用各分镜的当前版本</span><span>装配任务：{assemblyIsStale ? '历史成片 · 需重新装配' : assemblyTaskState ? taskStateLabel(assemblyTaskState) : '无'}</span></div></article>

            <details className="director-operation-panel director-execution-record"><summary className="director-panel-title"><div><h3>本次实际执行档案</h3><small>查看实际使用的参数与执行记录</small></div><span>{currentTask ? `${currentTask.status} · ${currentTask.id.slice(0, 8)}` : '尚无任务'}</span></summary>{currentTask?.payload?.execution_snapshot ? <dl><div><dt>实际步骤</dt><dd>{currentTask.payload.execution_snapshot.pipeline_stage_id || '旧任务 · 未登记步骤'} · 已写入 {currentTask.payload.execution_snapshot.applied_bindings?.length ?? 0} 项节点输入</dd></div><div><dt>实际提示词</dt><dd>{currentTask.payload.execution_snapshot.prompt || '工作流未声明提示词覆盖'}</dd></div><div><dt>API 图</dt><dd><a href={workflowUrl(currentTask.payload.execution_snapshot.api_graph ?? '')}>{currentTask.payload.execution_snapshot.api_graph}</a></dd></div><div><dt>来源图</dt><dd>{currentTask.payload.execution_snapshot.source_workflow}</dd></div><div><dt>参数</dt><dd><code>{JSON.stringify(currentTask.payload.execution_snapshot.parameters ?? {})}</code></dd></div><div><dt>素材</dt><dd><code>{JSON.stringify(currentTask.payload.execution_snapshot.materials ?? {})}</code></dd></div></dl> : <p className="director-muted">提交后，这里会显示真正执行的提示词、素材、参数和冻结 API 图。</p>}{currentTask?.error && <p className="director-error">{currentTask.error}</p>}</details>
          </div>
        </section>}
        </>}
      </div>
      {workspaceMode === 'production' && workspacePage === 'shots' && advancedOpen && pipelineOpen && <aside className="director-pipeline-drawer" aria-label="高级制作参数"><div className="director-drawer-head"><div><span className="director-kicker">当前分镜</span><h2>高级制作参数</h2><p>仅在需要调试执行方案时使用。</p></div><button className="icon-button" aria-label="关闭高级参数" onClick={() => setPipelineOpen(false)}><X size={17} /></button></div><div className="director-pipeline-list">{currentPipeline.map(stage => <button key={stage.id} className={`director-pipeline-step ${stage.id === activePipelineStage?.id ? 'active' : ''}`} onClick={() => setPipelineStageId(stage.id)}><span>{String(stage.order).padStart(2, '0')}</span><div><strong>{stage.title}</strong><small>{stage.purpose}</small></div><em>{stage.status}</em></button>)}</div></aside>}
    </main>
      {materialBridgeOpen && <Suspense fallback={null}><MaterialBridge projectId={project?.id ?? ''} initialMode={materialBridgeDevice ? 'device' : 'server'} onClose={() => { setMaterialBridgeOpen(false); setMaterialBridgeDevice(false); }} onRegistered={count => { void refreshRegisteredMaterials(count); }} /></Suspense>}
      {creativeConflictPanel}
      {batchId && <div className="director-batch-note" role="note">批次参数语义：按每个分镜自己的计划与源工作流冻结；当前片段 Inspector 的修改只影响单段重做。需要批量改参时，请先逐段确认后再提交。</div>}
      <WorkbenchOverlays notice={notice} deleteDirectory={deleteDirectory} onCloseDeleteDirectory={() => setDeleteDirectory(null)} onDeletedDirectory={() => { void afterDirectoryDeleted(); }} deleteShotOpen={deleteShotOpen} deleteShotId={currentPlanSegment?.id} deletingShot={deletingShot} onCloseDeleteShot={() => setDeleteShotOpen(false)} onConfirmDeleteShot={() => void deleteCurrentShot()} dialogRef={dialogRef} lightbox={lightbox} onCloseLightbox={() => setLightbox(false)} current={current} />
  </div>;
}
