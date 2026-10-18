/** Small stateful hooks extracted from the workbench shell.
 *
 * Each hook owns one concern that used to live inline in `App.tsx`: the persisted sidebar
 * width and its drag handlers, the per-project draft store, and the engine status poll.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import type { PointerEvent as ReactPointerEvent } from 'react';
import { loadDrafts } from './helpers';
import type { Draft, Drafts } from './types';

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
