/** Small stateful hooks extracted from the workbench shell.
 *
 * Each hook owns one concern that used to live inline in `App.tsx`: the persisted sidebar
 * width and its drag handlers, the per-project draft store, and the engine status poll.
 */
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import type { PointerEvent as ReactPointerEvent } from 'react';
import { loadDrafts, normalizeLiveTaskState } from './helpers';
import type { Draft, Drafts, PlanAssembly, PlanSegment, ProjectManifest, TaskRecord } from './types';

const SIDEBAR_WIDTH_KEY = 'director-workbench:sidebar-width';
const DRAFTS_KEY = (projectId: string) => `director-workbench:${projectId}:drafts:v1`;

export function useSidebarWidth() {
  const [sidebarWidth, setSidebarWidth] = useState(() => {
    const saved = Number(localStorage.getItem(SIDEBAR_WIDTH_KEY));
    return Number.isFinite(saved) && saved >= 240 ? saved : 286;
  });

  function resizeSidebar(next: number) {
    const width = Math.max(240, Math.min(Math.floor(window.innerWidth / 2), next));
    setSidebarWidth(width);
    localStorage.setItem(SIDEBAR_WIDTH_KEY, String(width));
  }

  function beginSidebarResize(event: ReactPointerEvent<HTMLDivElement>) {
    event.preventDefault();
    const move = (pointer: PointerEvent) => resizeSidebar(pointer.clientX);
    const finish = () => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', finish);
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', finish);
  }

  return { sidebarWidth, resizeSidebar, beginSidebarResize };
}

type DraftOptions = {
  onStorageError: (failed: boolean) => void;
  onPromptChange: () => void;
};

export function useDrafts(projectId: string | undefined, options: DraftOptions) {
  const [drafts, setDrafts] = useState<Drafts>({});
  const callbacks = useRef(options);
  callbacks.current = options;
  const storageKey = DRAFTS_KEY(projectId ?? 'loading');
  const hydratedKey = useRef<string | null>(null);
  const pendingDrafts = useRef<Drafts | null>(null);
  const timer = useRef<number | null>(null);

  const flush = useCallback(() => {
    if (!projectId || hydratedKey.current !== storageKey || pendingDrafts.current === null) return;
    if (timer.current !== null) {
      window.clearTimeout(timer.current);
      timer.current = null;
    }
    try {
      localStorage.setItem(storageKey, JSON.stringify(pendingDrafts.current));
      pendingDrafts.current = null;
      callbacks.current.onStorageError(false);
    } catch {
      callbacks.current.onStorageError(true);
    }
  }, [projectId, storageKey]);

  useEffect(() => {
    if (!projectId || hydratedKey.current !== storageKey || pendingDrafts.current === null) return;
    if (timer.current !== null) window.clearTimeout(timer.current);
    timer.current = window.setTimeout(flush, 400);
    return () => {
      if (timer.current !== null) {
        window.clearTimeout(timer.current);
        timer.current = null;
      }
    };
  }, [drafts, flush, projectId, storageKey]);

  useEffect(() => {
    const flushOnExit = () => flush();
    const flushWhenHidden = () => { if (document.visibilityState === 'hidden') flush(); };
    window.addEventListener('pagehide', flushOnExit);
    document.addEventListener('visibilitychange', flushWhenHidden);
    return () => {
      window.removeEventListener('pagehide', flushOnExit);
      document.removeEventListener('visibilitychange', flushWhenHidden);
      flush();
    };
  }, [flush]);

  function loadDraftsForProject(id: string) {
    const key = DRAFTS_KEY(id);
    if (hydratedKey.current !== key) {
      if (timer.current !== null) window.clearTimeout(timer.current);
      pendingDrafts.current = null;
      hydratedKey.current = key;
      setDrafts(loadDrafts(key));
    }
  }

  function updateDraft(id: string, patch: Draft) {
    if ('prompt' in patch) callbacks.current.onPromptChange();
    setDrafts(previous => {
      const next = { ...previous, [id]: { ...previous[id], ...patch } };
      pendingDrafts.current = next;
      return next;
    });
  }

  /** Drop the local draft after a restored candidate replaces the edited segment. */
  function clearDraft(id: string) {
    setDrafts(previous => {
      const next = { ...previous };
      delete next[id];
      pendingDrafts.current = next;
      return next;
    });
  }

  /** A newly created project starts without any carried-over draft. */
  function resetDrafts() {
    pendingDrafts.current = {};
    setDrafts({});
  }

  return { drafts, loadDraftsForProject, updateDraft, clearDraft, resetDrafts };
}

export type ComfyInfo = { version?: string; gpu_free_mib?: number; queue_running?: number; queue_pending?: number };

export function useComfyStatus() {
  const [comfyOnline, setComfyOnline] = useState(false);
  const [comfyInfo, setComfyInfo] = useState<ComfyInfo>({});

  useEffect(() => {
    let cancelled = false;
    const refresh = async () => {
      try {
        const response = await fetch('/api/status');
        if (!response.ok) throw new Error('status');
        const data = await response.json();
        if (!cancelled) {
          setComfyOnline(Boolean(data.comfy?.online));
          setComfyInfo(data.comfy ?? {});
        }
      } catch {
        if (!cancelled) setComfyOnline(false);
      }
    };
    void refresh();
    const timer = window.setInterval(refresh, 4000);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, []);

  return { comfyOnline, comfyInfo };
}

type ProjectTasksOptions = {
  projectId?: string;
  assemblyAssetId?: string;
  currentAssetId: string;
  batchId: string;
  onTaskSucceeded?: () => void;
};

/** Owns the HTTP snapshot and reconnecting SSE stream for one project. */
export function useProjectTasks({ projectId, assemblyAssetId, currentAssetId, batchId, onTaskSucceeded }: ProjectTasksOptions) {
  const [taskState, setTaskState] = useState('');
  const [taskId, setTaskId] = useState('');
  const [currentTask, setCurrentTask] = useState<TaskRecord | null>(null);
  const [projectTasks, setProjectTasks] = useState<TaskRecord[]>([]);
  const [assemblyTaskState, setAssemblyTaskState] = useState('');
  const [assemblyTaskId, setAssemblyTaskId] = useState('');
  const succeededTaskRef = useRef('');
  const callbackRef = useRef(onTaskSucceeded);
  callbackRef.current = onTaskSucceeded;

  const applyTasks = useCallback((records: TaskRecord[]) => {
    const scoped = records.filter(task => task.project_id === projectId);
    setProjectTasks(scoped);
    const selected = scoped.find(task => task.asset_id === currentAssetId) ?? null;
    setCurrentTask(selected);
    setTaskId(selected?.id ?? '');
    setTaskState(normalizeLiveTaskState(selected?.status ?? ''));
    const assembly = assemblyAssetId ? scoped.find(task => task.asset_id === assemblyAssetId) : undefined;
    setAssemblyTaskId(assembly?.id ?? '');
    setAssemblyTaskState(assembly?.status ?? '');
    const completed = scoped.find(task => task.status === 'succeeded');
    if (completed?.id && completed.id !== succeededTaskRef.current) {
      succeededTaskRef.current = completed.id;
      callbackRef.current?.();
    }
  }, [assemblyAssetId, currentAssetId, projectId]);

  const refreshTasks = useCallback(async () => {
    if (!projectId) return;
    const response = await fetch(`/api/projects/${encodeURIComponent(projectId)}/tasks?limit=100`);
    if (!response.ok) throw new Error('tasks');
    const data = await response.json() as { tasks?: TaskRecord[] };
    applyTasks(Array.isArray(data.tasks) ? data.tasks : []);
  }, [applyTasks, projectId]);

  useEffect(() => {
    succeededTaskRef.current = '';
    setTaskId(''); setTaskState(''); setCurrentTask(null); setProjectTasks([]); setAssemblyTaskId(''); setAssemblyTaskState('');
  }, [projectId]);

  useEffect(() => {
    if (!projectId) return;
    const poll = () => { void refreshTasks().catch(() => { /* SSE remains the live source when HTTP is unavailable */ }); };
    poll();
    const timer = window.setInterval(poll, 15000);
    return () => window.clearInterval(timer);
  }, [projectId, refreshTasks]);

  useEffect(() => {
    if (!projectId) return;
    let cancelled = false;
    let source: EventSource | null = null;
    let reconnectTimer: number | null = null;
    let delay = 500;
    const connect = () => {
      if (cancelled) return;
      source = new EventSource(`/api/projects/${encodeURIComponent(projectId)}/events`);
      source.onopen = () => { delay = 500; };
      source.onmessage = event => {
        try {
          const data = JSON.parse(event.data) as { tasks?: TaskRecord[] };
          applyTasks(Array.isArray(data.tasks) ? data.tasks : []);
        } catch { /* malformed transient events do not affect the review UI */ }
      };
      source.onerror = () => {
        source?.close();
        source = null;
        if (cancelled || reconnectTimer !== null) return;
        reconnectTimer = window.setTimeout(() => { reconnectTimer = null; delay = Math.min(delay * 2, 10000); connect(); }, delay);
      };
    };
    connect();
    return () => {
      cancelled = true;
      if (reconnectTimer !== null) window.clearTimeout(reconnectTimer);
      source?.close();
    };
  }, [applyTasks, batchId, projectId]);

  return { taskState, setTaskState, taskId, setTaskId, currentTask, setCurrentTask, projectTasks, setProjectTasks, assemblyTaskState, setAssemblyTaskState, assemblyTaskId, setAssemblyTaskId };
}

type PlaybackAsset = { id: string; ready?: boolean };
type PlaybackOptions = {
  mediaKey: string;
  start: number;
  duration: number;
  kind: string;
  currentEnd?: number;
  mediaElement: HTMLMediaElement | null;
  mediaRef: { current: HTMLMediaElement | null };
  autoplayRef: { current: boolean };
  advancing: { current: boolean };
  ready: boolean;
  continuous: boolean;
  focusList: PlaybackAsset[];
  focusIndex: number;
  onAdvance: (asset: PlaybackAsset) => void;
  onNotice: (message: string) => void;
};

/** Isolates media lifecycle, transport state and continuous preview behavior. */
export function usePlayback(options: PlaybackOptions) {
  const { mediaKey, start, duration, kind, currentEnd, mediaElement, mediaRef, autoplayRef, advancing, ready, continuous, focusList, focusIndex, onAdvance, onNotice } = options;
  const [playing, setPlaying] = useState(false);
  const [mediaLoad, setMediaLoad] = useState<{ key: string; status: 'loading' | 'ready' | 'error' }>({ key: '', status: 'loading' });
  const noticeRef = useRef(onNotice);
  noticeRef.current = onNotice;
  const epoch = useRef(0);

  useLayoutEffect(() => {
    epoch.current += 1;
    return () => { epoch.current += 1; };
  }, [mediaElement, mediaKey, start]);

  useEffect(() => {
    advancing.current = false;
    setPlaying(false);
    const media = mediaElement;
    if (!media) return;
    let cancelled = false;
    let initialized = false;
    const mediaEpoch = epoch.current;
    setMediaLoad({ key: mediaKey, status: 'loading' });
    const initialize = () => {
      if (cancelled || mediaRef.current !== media || epoch.current !== mediaEpoch) return;
      if (media.error) { setMediaLoad({ key: mediaKey, status: 'error' }); return; }
      if (media.readyState < 3) return;
      setMediaLoad({ key: mediaKey, status: 'ready' });
      if (initialized) return;
      initialized = true;
      media.currentTime = start;
      if (autoplayRef.current) {
        autoplayRef.current = false;
        void media.play().catch(error => { if (!cancelled && epoch.current === mediaEpoch && mediaRef.current === media && error?.name !== 'AbortError') noticeRef.current('浏览器暂停了自动播放，请点击播放继续。'); });
      } else media.pause();
    };
    const events = ['loadedmetadata', 'loadeddata', 'canplay', 'canplaythrough', 'progress', 'playing'];
    for (const event of events) media.addEventListener(event, initialize);
    initialize();
    return () => { cancelled = true; for (const event of events) media.removeEventListener(event, initialize); };
  }, [advancing, autoplayRef, mediaElement, mediaKey, mediaRef, start]);

  const finish = useCallback(() => {
    if (advancing.current) return;
    advancing.current = true;
    mediaRef.current?.pause();
    setPlaying(false);
    const next = focusList[focusIndex + 1];
    if (continuous && next?.ready) {
      autoplayRef.current = true;
      onAdvance(next);
    } else {
      autoplayRef.current = false;
      noticeRef.current(continuous && next ? '下一段尚未生成，连续预览已暂停。' : '本次播放结束。');
    }
  }, [advancing, autoplayRef, continuous, focusIndex, focusList, mediaRef, onAdvance]);

  const togglePlay = useCallback(async () => {
    const media = mediaRef.current;
    const playbackDisabled = !ready || (kind !== 'image' && mediaLoad.key !== mediaKey ? true : mediaLoad.status !== 'ready');
    if (!media || playbackDisabled) return;
    const mediaEpoch = epoch.current;
    if (!media.paused) { media.pause(); return; }
    advancing.current = false;
    if (media.ended || (duration > 0 && media.currentTime >= start + duration - 0.08)) media.currentTime = start;
    try {
      await media.play();
      if (epoch.current === mediaEpoch && mediaRef.current === media && media.isConnected) noticeRef.current('');
    } catch (error) {
      if (epoch.current === mediaEpoch && mediaRef.current === media && media.isConnected && !(error instanceof DOMException && error.name === 'AbortError')) noticeRef.current('暂时无法播放，请检查素材是否可读取。');
    }
  }, [advancing, duration, kind, mediaKey, mediaLoad, mediaRef, ready, start]);

  const mediaProps = useMemo(() => ({
    controls: true,
    preload: 'metadata' as const,
    onPlay: () => { setPlaying(true); advancing.current = false; },
    onPause: () => setPlaying(false),
    onEnded: finish,
    onError: (event: { currentTarget: HTMLMediaElement }) => { if (mediaRef.current !== event.currentTarget) return; setMediaLoad({ key: mediaKey, status: 'error' }); noticeRef.current('素材加载失败，请检查本机作品文件。'); },
    onTimeUpdate: () => { const media = mediaRef.current; if (media && kind === 'audio' && !media.paused && media.currentTime >= (currentEnd ?? Infinity) - 0.04) finish(); },
  }), [advancing, currentEnd, finish, kind, mediaKey, mediaRef]);

  const playbackDisabled = !ready || (kind !== 'image' && (mediaLoad.key !== mediaKey || mediaLoad.status !== 'ready'));
  return { playing, mediaLoad, mediaStatus: mediaLoad.key === mediaKey ? mediaLoad.status : 'loading', playbackDisabled, togglePlay, finish, mediaProps };
}

type ProjectSessionOptions = {
  privateMode: boolean;
  onProjectLoaded: (project: ProjectManifest) => void;
  onOffline: () => void;
};

/** Loads the active project once and leaves private-mode recovery to the caller. */
export function useProjectSession({ privateMode, onProjectLoaded, onOffline }: ProjectSessionOptions) {
  const [loading, setLoading] = useState(true);
  const callbacks = useRef({ onProjectLoaded, onOffline });
  callbacks.current = { onProjectLoaded, onOffline };
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    fetch('/api/project')
      .then(response => response.ok ? response.json() : Promise.reject(new Error('project')))
      .then(data => { if (!cancelled) callbacks.current.onProjectLoaded(data as ProjectManifest); })
      .catch(() => { if (!cancelled) callbacks.current.onOffline(); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [privateMode]);
  return { loading };
}

type PlanOptions = {
  projectId?: string;
  privateMode: boolean;
  onRefreshState?: () => void;
};

/** Owns the active plan snapshot and its revision-bound refresh request. */
export function usePlan({ projectId, privateMode, onRefreshState }: PlanOptions) {
  const [planSegments, setPlanSegments] = useState<PlanSegment[]>([]);
  const [planRevision, setPlanRevision] = useState(0);
  const [planAssembly, setPlanAssembly] = useState<PlanAssembly | null>(null);
  const projectRef = useRef(projectId);
  projectRef.current = projectId;
  const stateCallback = useRef(onRefreshState);
  stateCallback.current = onRefreshState;

  const refreshPlan = useCallback(async () => {
    if (privateMode && !projectId) return;
    const requestedProjectId = projectId;
    const response = await fetch(projectId ? `/api/projects/${encodeURIComponent(projectId)}/plan` : '/api/plan');
    if (!response.ok) throw new Error('plan');
    const data = await response.json();
    if (projectRef.current !== requestedProjectId) return;
    if (Array.isArray(data.segments)) setPlanSegments(data.segments as PlanSegment[]);
    setPlanRevision(Number(data.revision) || 0);
    setPlanAssembly(data.assembly && typeof data.assembly === 'object' ? data.assembly as PlanAssembly : null);
    stateCallback.current?.();
  }, [privateMode, projectId]);

  return { planSegments, setPlanSegments, planRevision, setPlanRevision, planAssembly, setPlanAssembly, refreshPlan };
}
