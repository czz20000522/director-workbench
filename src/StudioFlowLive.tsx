import { lazy, Suspense, useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import {
  ArrowRight, AudioLines, BookOpen, Check, CheckSquare, ChevronLeft, ChevronRight, Clapperboard, FolderOpen, ListTodo, ScanEye,
  Square, Trash2, Wand2,
  Film, HardDrive, Image, Layers3, Library, LoaderCircle, Music, Play, Plus, Upload, Volume2, X,
} from 'lucide-react';
const ScriptWorkbench = lazy(() => import('./ScriptWorkbench'));
const ReverseWorkbench = lazy(() => import('./ReverseWorkbench'));
const ProjectMaterialLibrary = lazy(() => import('./ProjectMaterialLibrary'));
const QuickToolPage = lazy(() => import('./QuickTools'));
const EnhancementPanel = lazy(() => import('./EnhancementPanel'));
const MediaToolsPanel = lazy(() => import('./MediaToolsPanel'));
const AudioEditPanel = lazy(() => import('./AudioEditPanel'));
const KeyframePanel = lazy(() => import('./KeyframePanel'));
const SpeechPanel = lazy(() => import('./SpeechPanel'));
const DubbingPanel = lazy(() => import('./DubbingPanel'));
const AudioSwapPanel = lazy(() => import('./AudioSwapPanel'));
const ObservationPanel = lazy(() => import('./ObservationPanel'));
const MusicPanel = lazy(() => import('./MusicPanel'));
import SeriesResources from './SeriesResources';
import type { QuickSeed, QuickTool } from './QuickTools';
import PresetLibraryPanel from './PresetLibraryPanel';
import ScriptLibraryDialog, { ScriptLibraryEntry } from './ScriptLibrary';
import EnhanceLibraryPanel from './EnhanceLibraryPanel';
import ProjectDossier from './ProjectDossier';
import ProjectLibraryTool from './ProjectLibraryTool';
import { MaterialLibraryPanel, PromptLibraryPanel, ReferenceHints } from './ReferenceLibrary';
import type { PromptTarget } from './ReferenceLibrary';
import TaskProgress, { type TaskProgress as Progress } from './TaskProgress';
import { projectMaterialOptions } from './materialOptions';
import './studio-flow.css';
import './reference-library.css';
import './reverse-workbench.css';
import './material-library.css';
import './post-tools.css';

type Step = 1 | 2 | 3 | 4 | 5 | 6;
type Tool = 'script' | 'reference' | 'materials' | 'tasks' | 'post' | 'dossier' | 'library';
type PostTab = 'enhance' | 'media' | 'audio' | 'swap' | 'keyframe' | 'speech' | 'dubbing' | 'observe' | 'music';
type ProjectAsset = { id: string; title?: string; kind?: string };
type ProjectDetail = { assets?: ProjectAsset[] };
type ProjectSummary = { id: string; title: string; series: string; selected?: boolean };
type Segment = { id: string; duration_seconds?: number; prompt?: string; status?: string; script_scene_id?: string; video?: { path?: string } };
type Plan = { revision: number; segments: Segment[]; assembly?: { output?: string; prompt_id?: string; status?: string } };
type Preset = { id: string; title: string; description?: string; available?: boolean; missing_models?: string[] };
type ProjectView = ProjectSummary & { plan: Plan | null; step: string; progress: number };
type Version = { task_id: string; video_path: string; is_current?: boolean; generated_at?: string; media?: Record<string, unknown> };
type Join = { left_id: string; right_id: string; status: string; review_completed?: boolean; review_ready?: boolean };
type AssemblyView = {
  revision: number; joins?: Join[]; sound?: { policy?: string; selected?: boolean; audio_asset_id?: string | null };
  adoption?: { ready?: boolean; blockers?: unknown[] }; continuity_check?: { ready?: boolean; blockers?: unknown[]; checks?: unknown[] };
  ready?: boolean; blockers?: unknown[];
};
type ReviewState = { reviews?: Record<string, Record<string, { revision?: number; status?: string; candidate_ref?: string }>> };
type Archivable = { asset_id: string; target: 'reference' | 'timbre'; library: string; title: string; source: string; project_id?: string };
type DeletionEntry = {
  project_id: string; title: string; series: string; workspace: string; workspace_exists: boolean;
  snapshot: { file_count: number; size_bytes: number; sample: string[] };
  archivable?: Archivable[]; outputs?: string[];
};
type DeletionPlan = { plan_id: string; expires_at: number; entries: DeletionEntry[]; preserved: string };
type QuickTask = {
  id: string; kind?: string; tool: string; title: string; status: string; created_at?: number; output?: string | null;
  error?: string | null; revision?: number; retry_of?: string | null; deletable: boolean; delete_blocker?: string | null;
  progress?: Progress;
  queue?: { position?: number | null };
  video_mode?: 'text-video' | 'image-video' | null;
};
type TaskPlanEntry = { task_id: string; project_id?: string; status: string; title: string; snapshots: string[] };
type TaskPlan = { plan_id: string; expires_at: number; entries: TaskPlanEntry[]; preserved: string; archivable?: Archivable[]; outputs?: string[] };

// 首页把单步任务按工具分块；视频再看有没有首帧，分开图生与文生。
const quickGroups: { key: string; title: string; test: (task: QuickTask) => boolean }[] = [
  { key: 'image', title: '文生图', test: task => task.tool === '图片' },
  { key: 'image-video', title: '图生视频', test: task => task.tool === '视频' && task.video_mode === 'image-video' },
  { key: 'text-video', title: '文生视频', test: task => task.tool === '视频' && task.video_mode !== 'image-video' },
  { key: 'timbre', title: '音色', test: task => task.tool === '音色设计' },
  { key: 'music', title: '配乐', test: task => task.tool === '配乐' },
  { key: 'enhance', title: '清晰化', test: task => task.tool === '清晰化' },
  { key: 'other', title: '其他', test: () => true },
];

const steps = [
  { id: 2, label: '方案', hint: '显式选择' },
  { id: 3, label: '镜头', hint: '计划与素材' },
  { id: 4, label: '候选', hint: '逐段采用' },
  { id: 5, label: '装配', hint: '检查与成片' },
  { id: 6, label: '交付', hint: '预览与下载' },
] as const;

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, { credentials: 'same-origin', ...init });
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = body?.detail;
    throw new Error(typeof detail === 'string' ? detail : detail?.message || `请求失败（${response.status}）`);
  }
  return body as T;
}

function derive(plan: Plan | null): { step: string; progress: number } {
  if (!plan) return { step: '继续制作', progress: 0 };
  if (!plan.segments.length) return { step: '方案', progress: 25 };
  if (!plan.segments.some(segment => Boolean(segment.video?.path))) return { step: '镜头', progress: 50 };
  return { step: '候选', progress: 70 };
}

function Thumb({ tone = 'rain', label, film = false }: { tone?: string; label?: string; film?: boolean }) {
  return <div className={`sf-thumb sf-thumb-${tone}`} aria-hidden="true"><span>{label ?? '01'}</span>{film && <Clapperboard size={22} />}</div>;
}

function TopBar({ username, projectTitle, onHome, onLogout }: { username: string; projectTitle?: string; onHome: () => void; onLogout: () => void }) {
  return (
    <header className="sf-topbar">
      <button className="sf-brand" onClick={onHome} aria-label="返回首页">
        <span className="sf-brand-mark"><Clapperboard size={18} /></span><span><strong>导演工作台</strong></span>
      </button>
      <div className="sf-topbar-center"><span className="sf-dot" /> {projectTitle || '工具台'}</div>
      <div className="sf-topbar-actions"><span className="sf-account">{(username || 'U').slice(0, 1).toUpperCase()}</span><button className="sf-ghost" onClick={onLogout}>退出</button></div>
    </header>
  );
}

function Home({ projects, seriesNames, continueProject, loading, error, onStart, onOpen, onSeries, onRetry, onQuick, onDeleted, onUsePrompt, onUseFrame, onUseTimbre, onUseMusic }: {
  projects: ProjectView[]; seriesNames: string[]; continueProject: ProjectView | null; loading: boolean; error: string;
  onStart: () => void; onOpen: (id: string) => void; onSeries: (series: string) => void; onRetry: () => void;
  onQuick: (tool: QuickTool) => void; onDeleted: () => void;
  onUsePrompt: (text: string, target: PromptTarget) => void; onUseFrame: (path: string, title: string) => void;
  onUseTimbre: (description: string) => void; onUseMusic: (card: Record<string, unknown>) => void;
}) {
  const [batchMode, setBatchMode] = useState(false);
  const [selected, setSelected] = useState<string[]>([]);
  const [plan, setPlan] = useState<DeletionPlan | null>(null);
  const [scriptOpen, setScriptOpen] = useState(false);
  const [archivePick, setArchivePick] = useState<string[]>([]);
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [dialogError, setDialogError] = useState('');

  const toggle = (id: string) => setSelected(current => current.includes(id) ? current.filter(item => item !== id) : [...current, id]);
  const requestDelete = async (ids: string[]) => {
    if (!ids.length) return;
    setBusy(true); setDialogError(''); setConfirmed(false); setArchivePick([]);
    try {
      setPlan(await api<DeletionPlan>('/api/projects/deletion-plan', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project_ids: ids }),
      }));
    } catch (reason) { setDialogError(reason instanceof Error ? reason.message : '无法生成删除预览'); }
    finally { setBusy(false); }
  };
  const applyDelete = async () => {
    if (!plan || !confirmed) return;
    setBusy(true); setDialogError('');
    try {
      await api('/api/projects/deletion', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ plan_id: plan.plan_id, project_ids: plan.entries.map(entry => entry.project_id),
          archive_asset_ids: archivePick, confirm: true }),
      });
      setPlan(null); setSelected([]); setBatchMode(false); onDeleted();
    } catch (reason) { setDialogError(reason instanceof Error ? reason.message : '删除失败'); }
    finally { setBusy(false); }
  };

  const [quick, setQuick] = useState<QuickTask[]>([]);
  const [quickError, setQuickError] = useState('');
  const [quickBatch, setQuickBatch] = useState(false);
  const [quickSelected, setQuickSelected] = useState<string[]>([]);
  const [quickPlan, setQuickPlan] = useState<TaskPlan | null>(null);
  const [quickArchivePick, setQuickArchivePick] = useState<string[]>([]);
  const [quickConfirmed, setQuickConfirmed] = useState(false);

  const loadQuick = async () => {
    try { setQuick((await api<{ tasks: QuickTask[] }>('/api/quick-tasks')).tasks || []); setQuickError(''); }
    catch (reason) { setQuickError(reason instanceof Error ? reason.message : '无法读取单步任务'); }
  };
  useEffect(() => {
    void loadQuick();
    const timer = setInterval(() => void loadQuick(), 2000);
    return () => clearInterval(timer);
  }, []);
  const quickToggle = (id: string) => setQuickSelected(current => current.includes(id) ? current.filter(item => item !== id) : [...current, id]);
  const requestQuickDelete = async (ids: string[]) => {
    if (!ids.length) return;
    setBusy(true); setDialogError(''); setQuickConfirmed(false); setQuickArchivePick([]);
    try {
      setQuickPlan(await api<TaskPlan>('/api/tasks/deletion-plan', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ task_ids: ids }),
      }));
    } catch (reason) { setDialogError(reason instanceof Error ? reason.message : '无法生成删除预览'); }
    finally { setBusy(false); }
  };
  const applyQuickDelete = async () => {
    if (!quickPlan || !quickConfirmed) return;
    setBusy(true); setDialogError('');
    try {
      await api('/api/tasks/deletion', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ plan_id: quickPlan.plan_id, task_ids: quickPlan.entries.map(entry => entry.task_id),
          archive_asset_ids: quickArchivePick, confirm: true }),
      });
      setQuickPlan(null); setQuickSelected([]); setQuickBatch(false); void loadQuick();
    } catch (reason) { setDialogError(reason instanceof Error ? reason.message : '删除失败'); }
    finally { setBusy(false); }
  };

  if (loading) return <main className="sf-page sf-center"><LoaderCircle className="sf-spin" size={20} /><p>正在读取作品…</p></main>;
  if (error) return <main className="sf-page sf-center"><p role="alert">{error}</p><button className="sf-secondary" onClick={onRetry}>重试</button></main>;
  return (
    <div className="sf-home-shell">
      <div className="sf-rail-col sf-rail-col--left">
        <PromptLibraryPanel onUsePrompt={onUsePrompt} />
        <PresetLibraryPanel kind="timbre" onUse={params => onUseTimbre(String(params.voice_description || ''))} />
        <ScriptLibraryEntry onOpen={() => setScriptOpen(true)} />
      </div>
      <main className="sf-page sf-home">
      <header className="sf-home-head">
        <div><h1>工具台</h1><p>选择单步工具，或继续一个已有项目。</p></div>
        <div className="sf-home-actions">
          {batchMode
            ? <>
              <button className="sf-ghost" onClick={() => { setBatchMode(false); setSelected([]); }}>退出批量</button>
              <button className="sf-secondary" disabled={!selected.length || busy} onClick={() => void requestDelete(selected)}>删除所选（{selected.length}） <Trash2 size={15} /></button>
            </>
            : <button className="sf-ghost" onClick={() => setBatchMode(true)}>批量管理</button>}
          <button className="sf-secondary" onClick={onStart}><Plus size={16} /> 新建项目</button>
        </div>
      </header>
      <section className="qt-tools">
        <button onClick={() => onQuick('image')}><Image size={24} /><strong>文生图</strong><span>输入提示词，生成一张图</span></button>
        <button onClick={() => onQuick('text-video')}><Play size={24} /><strong>文生视频</strong><span>文字直接生成一个镜头</span></button>
        <button onClick={() => onQuick('image-video')}><Upload size={24} /><strong>图生视频</strong><span>上传一张图，生成一个镜头</span></button>
        <button onClick={() => onQuick('enhance')}><Wand2 size={24} /><strong>清晰化</strong><span>提高图片或视频分辨率</span></button>
        <button onClick={() => onQuick('music')}><Music size={24} /><strong>配乐</strong><span>按标签与时长生成音乐候选</span></button>
        <button onClick={() => onQuick('timbre')}><AudioLines size={24} /><strong>音色设计</strong><span>文字造音色或克隆参考，出可复用样本</span></button>
      </section>

      {continueProject ? (
        <button className="sf-continue-row" onClick={() => onOpen(continueProject.id)}>
          <span className="sf-work-mark">{continueProject.title.slice(0, 1)}</span>
          <span className="sf-continue-copy">
            <span className="sf-continue-line"><strong>{continueProject.title}</strong><span>{continueProject.series}</span></span>
            <span className="sf-continue-sub">{continueProject.step} · {continueProject.plan?.segments.length ?? 0} 个镜头 · 最近编辑</span>
            <span className="sf-progress"><span style={{ width: `${Math.max(continueProject.progress, 4)}%` }} /></span>
          </span>
          <span className="sf-continue-action">打开项目 <ArrowRight size={15} /></span>
        </button>
      ) : (
        <section className="sf-empty-card"><Film size={20} /><div><h2>还没有作品</h2><p>单步工具可以直接生成；多镜头项目可以新建项目后逐步管理。</p></div></section>
      )}

      {projects.length > 0 && <section className="sf-section">
        <div className="sf-section-head"><h2>项目</h2><span>{projects.length} 部</span></div>
        <div className="sf-work-list">
          {projects.map(project => (
            <div className="sf-work-row" key={project.id}>
              {batchMode && (
                <button className="sf-work-check" aria-label={selected.includes(project.id) ? '取消选择' : '选择'} onClick={() => toggle(project.id)}>
                  {selected.includes(project.id) ? <CheckSquare size={16} /> : <Square size={16} />}
                </button>
              )}
              <button className="sf-work-open" onClick={() => onOpen(project.id)}>
                <span className="sf-work-mark">{project.title.slice(0, 1)}</span>
                <span className="sf-work-main"><strong>{project.title}</strong><small>{project.series}</small></span>
                <span className="sf-work-step">{project.step}</span>
                <span className="sf-work-progress">{project.progress ? `${project.progress}%` : ''}</span>
                <ChevronRight size={15} className="sf-work-arrow" />
              </button>
              <button className="sf-work-delete" aria-label="删除项目" onClick={() => void requestDelete([project.id])}><Trash2 size={15} /></button>
            </div>
          ))}
        </div>
      </section>}

      {quick.length > 0 && <section className="sf-section">
        <div className="sf-section-head">
          <h2>单步任务</h2><span>{quick.length} 条 · 不属于任何作品</span>
          <button className="sf-ghost sf-section-action" onClick={() => { setQuickBatch(value => !value); setQuickSelected([]); }}>{quickBatch ? '退出批量' : '批量管理'}</button>
          {quickBatch && <button className="sf-secondary sf-section-action" disabled={!quickSelected.length || busy} onClick={() => void requestQuickDelete(quickSelected)}>删除所选（{quickSelected.length}） <Trash2 size={15} /></button>}
        </div>
        {quickError && <p className="sf-error" role="alert">{quickError}</p>}
        {(() => {
          const used = new Set<string>();
          return quickGroups.map(group => {
            const tasks = quick.filter(task => {
              if (used.has(task.id) || !group.test(task)) return false;
              if (group.key !== 'other') used.add(task.id);
              return true;
            });
            if (!tasks.length) return null;
            return <section className="sf-quick-group" key={group.key} aria-label={`单步任务 · ${group.title}`}>
              <div className="sf-quick-group-head"><h3>{group.title}</h3><span>{tasks.length} 条</span></div>
              <div className="sf-work-list">
                {tasks.map(task => (
                  <div className="sf-work-row" key={task.id}>
                    {quickBatch && (
                      <button className="sf-work-check" aria-label={quickSelected.includes(task.id) ? '取消选择' : '选择'} onClick={() => quickToggle(task.id)}>
                        {quickSelected.includes(task.id) ? <CheckSquare size={16} /> : <Square size={16} />}
                      </button>
                    )}
                    <span className="sf-work-open">
                      <span className="sf-work-mark">{group.title.slice(0, 1)}</span>
                      <span className="sf-work-main"><strong>{task.title}</strong><small>{task.tool} · {taskStates[task.status] || task.status}{task.created_at ? ` · ${new Date(task.created_at * 1000).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })}` : ''}{task.revision ? ` · 改第 ${task.revision} 版` : ''}</small>{task.error && <small className="sf-task-error">{task.error}</small>}<TaskProgress progress={task.progress} label={task.tool} /></span>
                      {task.output && <a className="sf-secondary" href={mediaFile(task.output)} target="_blank" rel="noreferrer">查看结果</a>}
                    </span>
                    {task.deletable
                      ? <button className="sf-work-delete" aria-label="删除单步任务" onClick={() => void requestQuickDelete([task.id])}><Trash2 size={15} /></button>
                      : <span className="sf-task-blocked" title={task.delete_blocker || undefined}><Trash2 size={15} /></span>}
                  </div>
                ))}
              </div>
            </section>;
          });
        })()}
      </section>}
      {plan && (
        <div className="sf-modal-mask" role="dialog" aria-modal="true">
          <div className="sf-modal">
            <h2>删除作品预览</h2>
            <p className="sf-lede">{plan.preserved}</p>
            <ul className="sf-del-list">
              {plan.entries.map(entry => (
                <li key={entry.project_id}>
                  <strong>{entry.title}</strong><small>{entry.series} · {entry.project_id}</small>
                  <span>{entry.workspace_exists
                    ? `${entry.snapshot.file_count} 个文件 · ${(entry.snapshot.size_bytes / 1048576).toFixed(1)} MB`
                    : '工作区目录已不存在，仅删除登记与任务记录'}</span>
                  {entry.workspace_exists && entry.snapshot.sample.length > 0 && (
                    <span className="sf-del-sample">{entry.snapshot.sample.slice(0, 3).join('、')}</span>
                  )}
                </li>
              ))}
            </ul>
            {(() => {
              const items = plan.entries.flatMap(entry => entry.archivable || []);
              const outputs = plan.entries.flatMap(entry => entry.outputs || []);
              if (!items.length && !outputs.length) return null;
              return <div className="sf-del-archive">
                <p className="sf-del-archive-head">删除前归档 · 可选，默认不勾；勾中的会移进素材库并登记</p>
                {items.map(item => <label className="sf-del-confirm" key={item.asset_id}>
                  <input type="checkbox" checked={archivePick.includes(item.asset_id)}
                    onChange={() => setArchivePick(current => current.includes(item.asset_id) ? current.filter(id => id !== item.asset_id) : [...current, item.asset_id])} />
                  存进{item.library}：{item.title}
                </label>)}
                {!items.length && <p className="sf-post-hint">这次没有可归档的参考图或音色样本。</p>}
                {outputs.length > 0 && <p className="sf-post-hint">另有 {outputs.length} 个生成产物会随删除一并清除（勾选归档的除外）。</p>}
              </div>;
            })()}
            <label className="sf-del-confirm"><input type="checkbox" checked={confirmed} onChange={event => setConfirmed(event.target.checked)} /> 我已核对以上内容，确认删除且不可恢复</label>
            {dialogError && <p className="sf-error" role="alert">{dialogError}</p>}
            <div className="sf-step-actions">
              <button className="sf-ghost" disabled={busy} onClick={() => { setPlan(null); setDialogError(''); }}>取消</button>
              <button className="sf-primary" disabled={busy || !confirmed} onClick={() => void applyDelete()}>{busy ? '删除中…' : '确认删除'} <Trash2 size={15} /></button>
            </div>
          </div>
        </div>
      )}
      {quickPlan && (
        <div className="sf-modal-mask" role="dialog" aria-modal="true">
          <div className="sf-modal">
            <h2>删除单步任务预览</h2>
            <p className="sf-lede">{quickPlan.preserved}</p>
            <ul className="sf-del-list">
              {quickPlan.entries.map(entry => (
                <li key={entry.task_id}>
                  <strong>{entry.title}</strong><small>{entry.task_id.slice(0, 8)} · {taskStates[entry.status] || entry.status}</small>
                  <span>{entry.snapshots.length ? `${entry.snapshots.length} 份冻结工作流快照将一并删除` : '无独立冻结快照'}</span>
                </li>
              ))}
            </ul>
            {(() => {
              const items = quickPlan.archivable || [];
              const outputs = quickPlan.outputs || [];
              if (!items.length && !outputs.length) return null;
              return <div className="sf-del-archive">
                <p className="sf-del-archive-head">删除前归档 · 可选，默认不勾；勾中的会移进素材库并登记</p>
                {items.map(item => <label className="sf-del-confirm" key={item.asset_id}>
                  <input type="checkbox" checked={quickArchivePick.includes(item.asset_id)}
                    onChange={() => setQuickArchivePick(current => current.includes(item.asset_id) ? current.filter(id => id !== item.asset_id) : [...current, item.asset_id])} />
                  存进{item.library}：{item.title}
                </label>)}
                {!items.length && <p className="sf-post-hint">这次没有可归档的参考图或音色样本。</p>}
                {outputs.length > 0 && <p className="sf-post-hint">另有 {outputs.length} 个生成产物会随删除一并清除（勾选归档的除外）。</p>}
              </div>;
            })()}
            <label className="sf-del-confirm"><input type="checkbox" checked={quickConfirmed} onChange={event => setQuickConfirmed(event.target.checked)} /> 我已核对以上内容，确认删除且不可恢复</label>
            {dialogError && <p className="sf-error" role="alert">{dialogError}</p>}
            <div className="sf-step-actions">
              <button className="sf-ghost" disabled={busy} onClick={() => { setQuickPlan(null); setDialogError(''); }}>取消</button>
              <button className="sf-primary" disabled={busy || !quickConfirmed} onClick={() => void applyQuickDelete()}>{busy ? '删除中…' : '确认删除'} <Trash2 size={15} /></button>
            </div>
          </div>
        </div>
      )}
      </main>
      <div className="sf-rail-col sf-rail-col--right">
        <MaterialLibraryPanel onUseFrame={onUseFrame} />
        <PresetLibraryPanel kind="music" onUse={params => onUseMusic(params)} />
        <EnhanceLibraryPanel />
      </div>
      {scriptOpen && <ScriptLibraryDialog onClose={() => setScriptOpen(false)} onChanged={() => void loadQuick()} />}
    </div>
  );
}


function StepRail({ step, tool, projectReady, projectTitle, onStep, onTool }: { step: Step; tool: Tool | null; projectReady: boolean; projectTitle: string; onStep: (step: Step) => void; onTool: (tool: Tool | null) => void }) {
  const tools: { id: Tool; label: string; icon: ReactNode }[] = [
    { id: 'script', label: '剧本', icon: <BookOpen size={14} /> },
    { id: 'reference', label: '参考', icon: <ScanEye size={14} /> },
    { id: 'materials', label: '素材', icon: <FolderOpen size={14} /> },
    { id: 'tasks', label: '任务', icon: <ListTodo size={14} /> },
    { id: 'post', label: '后期', icon: <Wand2 size={14} /> },
    { id: 'dossier', label: '档案', icon: <HardDrive size={14} /> },
    { id: 'library', label: '库', icon: <Library size={14} /> },
  ];
  return <aside className="sf-rail">
    <div className="sf-rail-head"><span>项目工具</span><strong>{projectTitle || '新作品'}</strong><small>人工确认后继续</small></div>
    <ol>{steps.map(item => <li key={item.id} className={!tool && item.id === step ? 'active' : item.id < step ? 'done' : ''}><button onClick={() => { onTool(null); onStep(item.id as Step); }} ><span className="sf-step-dot">{item.id < step ? <Check size={12} /> : ''}</span><span><strong>{item.label}</strong><small>{item.hint}</small></span></button></li>)}</ol>
    <div className="sf-rail-tools"><span>工具</span>{tools.map(item => <button key={item.id} className={tool === item.id ? 'active' : ''} disabled={!projectReady} onClick={() => onTool(item.id)}>{item.icon}{item.label}</button>)}</div>
  </aside>;
}

function Wizard({ step, tool, projectReady, projectTitle, onStep, onTool, onHome, children }: { step: Step; tool: Tool | null; projectReady: boolean; projectTitle: string; onStep: (step: Step) => void; onTool: (tool: Tool | null) => void; onHome: () => void; children: ReactNode }) {
  return <div className="sf-workspace"><StepRail step={step} tool={tool} projectReady={projectReady} projectTitle={projectTitle} onStep={onStep} onTool={onTool} /><main className="sf-main"><div className="sf-workspace-top"><button className="sf-back" onClick={onHome}><ChevronLeft size={15} /> 全部项目</button><span className="sf-save-state"><span className="sf-dot" /> 人工确认后继续</span></div>{children}</main></div>;
}


function StepTwo({ presets, selected, setSelected, onNext, busy, error }: { presets: Preset[]; selected: string; setSelected: (id: string) => void; onNext: () => void; busy: boolean; error: string }) {
  return <div className="sf-step-panel"><p className="sf-step-label">制作方案</p><h1>选择制作方案</h1><p className="sf-lede">只展示事实和限制，不替你推荐。选中的方案决定后面可用的输入和输出。</p>
    <div className="sf-preset-list">{presets.map(preset => <button key={preset.id} className={`sf-preset-row ${selected === preset.id ? 'selected' : ''}`} disabled={preset.available === false} onClick={() => setSelected(preset.id)}>
      <span className="sf-preset-dot" />
      <span className="sf-preset-body"><span className="sf-preset-title"><strong>{preset.title}</strong><span className={`sf-chip ${preset.available === false ? '' : 'sf-chip-ok'}`}>{preset.available === false ? '缺少模型' : '可用'}</span></span><small>{preset.description || '显式选择后安装到本作'}</small></span>
      <span className="sf-preset-pick">{selected === preset.id ? <><Check size={13} /> 已选择</> : '选择'}</span>
    </button>)}</div>
    {error && <p className="sf-error" role="alert">{error}</p>}
    <div className="sf-step-actions"><span className="sf-fine">方案决定后续输入与输出；不会自动安装或切换。</span><button className="sf-primary" disabled={busy || !selected} onClick={onNext}>{busy ? '安装中…' : '安装方案并准备分镜'} <ArrowRight size={16} /></button></div>
  </div>;
}

function StepThree({ segments, planRevision, prompt, setPrompt, duration, setDuration, onAdd, busy, error, onNext }: {
  segments: Segment[]; planRevision: number; prompt: string; setPrompt: (value: string) => void;
  duration: number; setDuration: (value: number) => void; onAdd: () => void; busy: boolean; error: string; onNext: () => void;
}) {
  return <div className="sf-step-panel"><p className="sf-step-label">镜头</p><h1>管理镜头</h1><p className="sf-lede">每一段独立准备。生成、查看候选和重做都在后面的候选步骤完成。</p>
    <div className="sf-shot-list">{segments.map((segment, index) => <article className="sf-shot" key={segment.id}><div className="sf-shot-number">{String(index + 1).padStart(2, '0')}</div><Thumb tone={index % 2 ? 'sage' : 'amber'} label={segment.id} /><div className="sf-shot-copy"><div className="sf-shot-head"><strong>{segment.id}</strong><span className="sf-chip">{segment.duration_seconds ?? '—'}s</span></div><p>{segment.prompt || '尚未填写提示词'}</p></div><div className="sf-shot-side"><span className={`sf-state ${segment.video?.path ? 'ok' : ''}`}>{segment.video?.path ? '已有候选' : '待生成'}</span></div></article>)}</div>
    <div className="sf-inline-form"><label className="sf-field"><span>新镜头提示词</span><textarea rows={2} value={prompt} onChange={event => setPrompt(event.target.value)} placeholder="描述这个镜头…" /></label><label className="sf-field sf-duration"><span>时长（秒）</span><input type="number" min={1} max={15} value={duration} onChange={event => setDuration(Number(event.target.value))} /></label><button className="sf-secondary" disabled={busy || !prompt.trim()} onClick={onAdd}>{busy ? '添加中…' : <><Plus size={15} /> 添加镜头</>}</button></div>
    {error && <p className="sf-error" role="alert">{error}</p>}
    {!segments.length && <p className="sf-fine">还没有镜头：写好提示词后先点「添加镜头」，它才会进入计划；有了镜头才能进入候选。</p>}
    <div className="sf-step-actions"><span className="sf-fine">当前计划 revision {planRevision}</span><button className="sf-primary" disabled={!segments.length} onClick={onNext}>下一步：进入候选 <ArrowRight size={16} /></button></div>
  </div>;
}

function mediaFile(path: string) { return `/media-file?path=${encodeURIComponent(path)}`; }

function StepFour({ segments, shotId, setShotId, versions, busy, error, onLoad, onAdopt, onNext, onGenerate, generating, task }: {
  segments: Segment[]; shotId: string; setShotId: (id: string) => void; versions: Version[]; busy: boolean; error: string;
  onLoad: (id: string) => void; onAdopt: (version: Version) => void; onNext: () => void; onGenerate: (segment: Segment) => void; generating: boolean; task: { id?: string; status?: string; error?: string } | null;
}) {
  const current = segments.find(segment => segment.id === shotId) || segments[0];
  return <div className="sf-step-panel"><p className="sf-step-label">候选</p><h1>比较候选</h1><p className="sf-lede">选择一段查看已有生成结果。工作台不会自动选“最佳”，采用只影响这一段。</p>
    <div className="sf-shot-tabs">{segments.map(segment => <button key={segment.id} className={segment.id === (current?.id || '') ? 'selected' : ''} onClick={() => { setShotId(segment.id); onLoad(segment.id); }}>{segment.id}<span>{segment.video?.path ? '有候选' : '待生成'}</span></button>)}</div>
    {versions.length ? <div className="sf-version-grid">{versions.map(version => <article className="sf-version" key={version.task_id}><video src={mediaFile(version.video_path)} controls preload="metadata" /><div><strong>{version.is_current ? '当前版本' : '历史候选'}</strong><small>{version.generated_at || version.task_id.slice(0, 8)}</small><button className="sf-secondary" disabled={busy} onClick={() => onAdopt(version)}>采用这个候选</button></div></article>)}</div>
      : <div className="sf-next-card"><Film size={22} /><div><strong>这一段还没有生成候选</strong><small>先生成，再回来看画面、声音和动作，逐段采用。</small></div><button className="sf-primary" disabled={generating} onClick={() => current && onGenerate(current)}>{generating ? '生成中…' : '生成当前段候选'} <ArrowRight size={16} /></button></div>}
    {error && <p className="sf-error" role="alert">{error}</p>}{task && <p className="sf-task-note">生成任务：{task.status || '已提交…'}{task.error ? ` · ${task.error}` : ''}</p>}
    <div className="sf-step-actions"><span className="sf-fine">采用是显式操作；不会自动采用其他镜头。</span><button className="sf-primary" disabled={!segments.every(segment => segment.video?.path)} onClick={onNext}>下一步：装配 <ArrowRight size={16} /></button></div>
  </div>;
}

function StepFive({ assembly, task, busy, error, onSound, onJoin, onSubmit, onReload }: {
  assembly: AssemblyView | null; task: { id?: string; status?: string; error?: string } | null; busy: boolean; error: string; onSound: () => void; onJoin: (join: Join) => void;
  onSubmit: () => void; onReload: () => void;
}) {
  if (!assembly) return <div className="sf-step-panel"><p className="sf-step-label">装配</p><h1>检查后生成成片</h1><div className="sf-next-card"><Layers3 size={22} /><div><strong>正在读取装配状态</strong><small>需要先生成至少一个采用候选。</small></div><button className="sf-secondary" onClick={onReload}>重新读取</button></div>{error && <p className="sf-error">{error}</p>}</div>;
  const joins = assembly.joins || [];
  return <div className="sf-step-panel"><p className="sf-step-label">装配</p><h1>检查后生成成片</h1><p className="sf-lede">采用、接点、音轨和连续性都确认后，才允许生成成片。</p>
    <div className="sf-checklist">
      <div className={`sf-check ${assembly.adoption?.ready ? 'done' : 'active'}`}><Check size={16} /><span><strong>分镜采用</strong><small>{assembly.adoption?.ready ? '所有参与分镜均有当前采用' : '仍有分镜未采用，请回到④'}</small></span></div>
      <div className={`sf-check ${joins.length && joins.every(join => join.review_completed) ? 'done' : 'active'}`}><Check size={16} /><span><strong>接点审核</strong><small>{joins.filter(join => !join.review_completed).length ? `${joins.filter(join => !join.review_completed).length} 个接点待审核` : '接点均已审核'}</small></span></div>
      <div className={`sf-check ${assembly.sound?.selected ? 'done' : 'active'}`}><Volume2 size={16} /><span><strong>音轨方式</strong><small>{assembly.sound?.selected ? `已选择：${assembly.sound.policy}` : '尚未选择音轨方式'}</small></span>{!assembly.sound?.selected && <button className="sf-secondary" disabled={busy} onClick={onSound}>保留各段原音</button>}</div>
      <div className={`sf-check ${assembly.continuity_check?.ready ? 'done' : 'active'}`}><Check size={16} /><span><strong>连续性检查</strong><small>{assembly.continuity_check?.ready ? '只读检查通过' : `${assembly.continuity_check?.blockers?.length || 0} 个阻断`}</small></span></div>
    </div>
    {joins.length > 0 && <div className="sf-join-list">{joins.map(join => <div className="sf-join" key={`${join.left_id}-${join.right_id}`}><span>{join.left_id} → {join.right_id}</span><span className={join.review_completed ? 'sf-ok' : ''}>{join.review_completed ? '已审核' : join.status}</span>{!join.review_completed && <button className="sf-secondary" disabled={busy} onClick={() => onJoin(join)}>通过接点</button>}</div>)}</div>}
    {error && <p className="sf-error" role="alert">{error}</p>}
    <div className="sf-step-actions"><button className="sf-secondary" onClick={onReload}>刷新检查</button><button className="sf-primary" disabled={busy || !assembly.ready} onClick={onSubmit}>{busy ? '提交中…' : '生成成片'} <ArrowRight size={16} /></button></div>
    {task && <p className="sf-task-note">装配任务：{task.status || '已提交…'}{task.error ? ` · ${task.error}` : ''}</p>}
  </div>;
}

function StepSix({ output, busy, error, assembly, onFinal, onOld }: { output: string; busy: boolean; error: string; assembly: AssemblyView | null; onFinal: () => void; onOld: () => void }) {
  return <div className="sf-step-panel"><p className="sf-step-label">交付</p><h1>成片</h1>{output ? <div className="sf-final-grid"><div className="sf-film"><video src={mediaFile(output)} controls preload="metadata" /><span className="sf-film-duration">{assembly?.revision !== undefined ? `计划 revision ${assembly.revision}` : ''}</span></div><div className="sf-final-meta"><h2>成片候选</h2><p>通过全片审核后才算交付；下载不等于采用。</p><div className="sf-final-actions"><button className="sf-primary" disabled={busy} onClick={onFinal}>{busy ? '提交中…' : '通过全片审核'}</button><a className="sf-secondary" href={mediaFile(output)} download>下载</a></div><button className="sf-secondary sf-wide-button" onClick={onOld}>返回作品库</button></div></div> : <div className="sf-next-card"><Film size={22} /><div><strong>还没有成片</strong><small>完成装配后在这里预览和审核。</small></div><button className="sf-secondary" onClick={onOld}>返回作品库</button></div>}{error && <p className="sf-error">{error}</p>}</div>;
}

type QueueTask = {
  id: string; project_id: string; project_title: string; asset_id?: string | null; kind?: string | null; status: string;
  batch_id?: string | null; sequence?: number | null; created_at?: number; error?: string | null; prompt?: string;
  revision?: number; retry_of?: string | null; seed?: number | null; width?: number | null; height?: number | null;
  deletable: boolean; editable: boolean; delete_blocker?: string | null; edit_blocker?: string | null;
  progress?: Progress;
  queue?: { position?: number | null; reason?: { code?: string; message?: string } | null } | null;
};

const taskStates: Record<string, string> = {
  queued: '排队中', scheduler_waiting: '等待调度', batch_waiting: '批次等待', preparing: '准备中', submitting: '提交中',
  running: '运行中', stop_requested: '停止中', stopping: '停止中', needs_reconcile: '待核对',
  succeeded: '已完成', failed: '失败', stopped: '已停止',
};
const RESUMABLE = new Set(['stopped', 'failed', 'needs_reconcile']);
const TERMINAL = new Set(['succeeded', 'failed', 'stopped']);

function TaskPage({ projectId }: { projectId: string }) {
  const [tasks, setTasks] = useState<QueueTask[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState('');
  const [editing, setEditing] = useState<{ task: QueueTask; derive: boolean } | null>(null);
  const [draft, setDraft] = useState({ prompt: '', seed: '', width: '', height: '' });
  const [removing, setRemoving] = useState<QueueTask | null>(null);
  const [removeConfirmed, setRemoveConfirmed] = useState(false);

  const load = async () => {
    try {
      const data = await api<{ tasks: QueueTask[] }>(`/api/queue/tasks?project_id=${encodeURIComponent(projectId)}&limit=80`);
      setTasks(data.tasks || []); setError('');
    } catch (reason) { setError(reason instanceof Error ? reason.message : '读取队列失败'); }
    finally { setLoading(false); }
  };
  useEffect(() => {
    void load();
    const timer = setInterval(() => void load(), 2000);
    return () => clearInterval(timer);
  }, [projectId]);

  const run = async (action: () => Promise<unknown>) => {
    setBusy(editing?.task.id || removing?.id || 'page');
    try { await action(); setEditing(null); setRemoving(null); setRemoveConfirmed(false); await load(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : '操作未完成'); }
    finally { setBusy(''); }
  };
  const openEditor = (task: QueueTask, derive: boolean) => {
    setEditing({ task, derive });
    setDraft({ prompt: task.prompt || '', seed: task.seed === null || task.seed === undefined ? '' : String(task.seed),
      width: task.width ? String(task.width) : '', height: task.height ? String(task.height) : '' });
  };
  const submitEditor = () => {
    if (!editing) return;
    const base = `/api/projects/${encodeURIComponent(projectId)}/tasks/${encodeURIComponent(editing.task.id)}`;
    const body: Record<string, unknown> = {};
    if (draft.prompt.trim() && draft.prompt.trim() !== editing.task.prompt) body.prompt = draft.prompt.trim();
    if (draft.seed !== '' && Number(draft.seed) !== editing.task.seed) body.seed = Number(draft.seed);
    if (draft.width !== '' && Number(draft.width) !== editing.task.width) body.width = Number(draft.width);
    if (draft.height !== '' && Number(draft.height) !== editing.task.height) body.height = Number(draft.height);
    if (!Object.keys(body).length) { setEditing(null); return; }
    if (editing.derive) void run(() => api(base + '/revise', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }));
    else void run(() => api(base, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }));
  };
  const act = (task: QueueTask, verb: string) => void run(() => api(
    `/api/projects/${encodeURIComponent(projectId)}/tasks/${encodeURIComponent(task.id)}/${verb}`, { method: 'POST' }));
  const remove = (task: QueueTask) => void run(() => api(
    `/api/projects/${encodeURIComponent(projectId)}/tasks/${encodeURIComponent(task.id)}`,
    { method: 'DELETE', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ confirm: true }) }));

  return <div className="sf-step-panel sf-tool-panel"><p className="sf-step-label">工具</p><h1>任务队列</h1>
    <p className="sf-lede">本作所有生成、装配与后期任务。GPU 每次只跑一个，其余按顺序等待；停止、改参数和删除都只影响你点下的那一条。</p>
    <div className="sf-tool-toolbar"><button className="sf-secondary" disabled={busy === 'page'} onClick={() => void load()}>刷新</button></div>
    {error && <p className="sf-error" role="alert">{error}</p>}
    {loading ? <p className="sf-fine">正在读取队列…</p> : !tasks.length ? <p className="sf-fine">还没有任务。</p> : (
      <div className="sf-task-list">{tasks.map(task => <div className="sf-task-row" key={task.id}>
        <span className={`sf-task-state ${task.status === 'succeeded' ? 'done' : task.status === 'failed' ? 'failed' : ''}`}>{taskStates[task.status] || task.status}</span>
        <span className="sf-task-main">
          <strong>{task.asset_id || 'MASTER'}{task.revision ? <em className="sf-task-rev">改第 {task.revision} 版</em> : null}{task.retry_of ? <em className="sf-task-rev">改参数重跑</em> : null}</strong>
          <small>{task.kind === 'workflow' ? '生成任务' : task.kind || '任务'} · {task.created_at ? new Date(task.created_at * 1000).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : task.id.slice(0, 8)}{task.batch_id ? ` · 批次 ${String(task.batch_id).slice(0, 8)} 第 ${task.sequence ?? '?'} 条` : ''}{task.queue?.position ? ` · 队列第 ${task.queue.position} 位` : ''}{task.queue?.reason?.message ? ` · ${task.queue.reason.message}` : ''}</small>
          {task.prompt && <small className="sf-task-prompt">{task.prompt}</small>}
          <TaskProgress progress={task.progress} />
          {task.error && <small className="sf-task-error">{task.error}</small>}
        </span>
        <span className="sf-task-actions">
          {!TERMINAL.has(task.status) && task.status !== 'needs_reconcile' && <button className="sf-secondary" disabled={busy === task.id} onClick={() => act(task, 'stop')}>停止</button>}
          {RESUMABLE.has(task.status) && <button className="sf-secondary" disabled={busy === task.id} onClick={() => act(task, 'resume')}>重试</button>}
          {task.editable && <button className="sf-secondary" disabled={!!busy} onClick={() => openEditor(task, false)}>改参数</button>}
          {!task.editable && task.kind === 'keyframe' && <button className="sf-secondary" disabled={!!busy} title={task.edit_blocker || undefined} onClick={() => openEditor(task, true)}>改参数重跑</button>}
          {task.deletable ? <button className="sf-task-delete" disabled={!!busy} aria-label="删除任务" onClick={() => { setRemoving(task); setRemoveConfirmed(false); }}><Trash2 size={15} /></button>
            : <span className="sf-task-blocked" title={task.delete_blocker || undefined}><Trash2 size={15} /></span>}
        </span>
      </div>)}</div>
    )}
    {editing && <div className="sf-modal-mask" role="dialog" aria-modal="true"><div className="sf-modal">
      <h2>{editing.derive ? '改参数重跑' : '修改未派发的任务'}</h2>
      <p className="sf-lede">{editing.derive ? '原任务保留为历史，另起一条带溯源的新任务排队。' : '这条任务还没派发，改动会重新冻结工作流；已派发的任务不能改。'}</p>
      <label className="sf-field"><span>画面描述</span><textarea className="sf-intent-field" rows={5} value={draft.prompt} onChange={event => setDraft({ ...draft, prompt: event.target.value })} /></label>
      <div className="sf-choice-row">
        <label className="sf-field"><span>随机编号</span><input type="number" min={0} value={draft.seed} onChange={event => setDraft({ ...draft, seed: event.target.value })} /></label>
        <label className="sf-field"><span>宽</span><input type="number" min={512} max={1536} step={8} value={draft.width} onChange={event => setDraft({ ...draft, width: event.target.value })} /></label>
        <label className="sf-field"><span>高</span><input type="number" min={512} max={1536} step={8} value={draft.height} onChange={event => setDraft({ ...draft, height: event.target.value })} /></label>
      </div>
      <div className="sf-step-actions"><button className="sf-ghost" disabled={!!busy} onClick={() => setEditing(null)}>取消</button>
        <button className="sf-primary" disabled={!!busy} onClick={submitEditor}>{busy === editing.task.id ? '提交中…' : editing.derive ? '重跑一版' : '保存并重冻结'}</button></div>
    </div></div>}
    {removing && <div className="sf-modal-mask" role="dialog" aria-modal="true"><div className="sf-modal">
      <h2>删除任务记录</h2>
      <p className="sf-lede">将删除这条任务记录、幂等键、资源采样和不再被引用的冻结工作流快照。ComfyUI 输出目录里的图片与成片不会被删除。</p>
      <ul className="sf-del-list"><li><strong>{removing.asset_id || 'MASTER'}</strong><small>{removing.project_title} · {removing.id.slice(0, 8)}</small><span>{taskStates[removing.status] || removing.status}{removing.prompt ? ` · ${removing.prompt.slice(0, 60)}` : ''}</span></li></ul>
      <label className="sf-del-confirm"><input type="checkbox" checked={removeConfirmed} onChange={event => setRemoveConfirmed(event.target.checked)} /> 我确认删除这条任务记录</label>
      <div className="sf-step-actions"><button className="sf-ghost" disabled={!!busy} onClick={() => setRemoving(null)}>取消</button>
        <button className="sf-primary" disabled={!removeConfirmed || !!busy} onClick={() => remove(removing)}>{busy === removing.id ? '删除中…' : '确认删除'} <Trash2 size={15} /></button></div>
    </div></div>}
  </div>;
}

function ToolPage({ tool, projectId, plan, projectDetail, postTab, setPostTab, onStep, onRefreshPlan, onRefreshProject, onOpenShot, onBrowse, onDeleted }: {
  tool: Tool; projectId: string; plan: Plan | null; projectDetail: ProjectDetail | null; postTab: PostTab; setPostTab: (tab: PostTab) => void; onStep: (step: Step) => void;
  onRefreshPlan: () => Promise<void>; onRefreshProject: () => Promise<void>; onOpenShot: (shotId: string) => void; onBrowse: () => void; onDeleted: () => void;
}) {
  if (tool === 'script') return <div className="sf-step-panel sf-tool-panel"><p className="sf-step-label">工具</p><h1>剧本</h1><p className="sf-lede">编辑本作剧本；从场景生成镜头时，回到镜头工具显式添加。</p><ScriptWorkbench projectId={projectId} shots={(plan?.segments || []).map(segment => ({ id: segment.id, script_scene_id: segment.script_scene_id }))} request={fetch} onCreateShot={() => onStep(3)} onSelectShot={shotId => onOpenShot(shotId)} /></div>;
  if (tool === 'reference') return <div className="sf-step-panel sf-tool-panel"><p className="sf-step-label">工具</p><h1>参考</h1><p className="sf-lede">拆解参考视频；转成分镜需要你明确确认，不会自动写入创作计划。</p><ReverseWorkbench projectId={projectId} onConverted={() => { void onRefreshPlan(); onStep(3); }} /></div>;
  if (tool === 'materials') return <div className="sf-step-panel sf-tool-panel"><p className="sf-step-label">工具</p><h1>素材</h1><p className="sf-lede">登记、查看和复用本作素材。</p><ProjectMaterialLibrary projectId={projectId} onRegistered={() => { void onRefreshPlan(); }} /></div>;
  if (tool === 'tasks') return <TaskPage projectId={projectId} />;
  if (tool === 'dossier') return <ProjectDossier projectId={projectId} onDeleted={onDeleted} />;
  if (tool === 'library') return <ProjectLibraryTool />;
  const assets = projectDetail?.assets || [];
  const audioAssets = assets.filter(asset => asset.kind === 'audio').map(asset => ({ id: asset.id, title: asset.title || asset.id }));
  const mediaAssets = assets.filter(asset => asset.kind === 'audio' || asset.kind === 'video').map(asset => ({ id: asset.id, title: asset.title || asset.id, kind: asset.kind || 'video' }));
  const videoAssets = assets.filter(asset => asset.kind === 'video').map(asset => ({ id: asset.id, title: asset.title || asset.id }));
  return <div className="sf-step-panel sf-tool-panel"><p className="sf-step-label">工具</p><h1>后期</h1><div className="sf-post-tabs"><button className={postTab === 'enhance' ? 'selected' : ''} onClick={() => setPostTab('enhance')}>清晰化</button><button className={postTab === 'media' ? 'selected' : ''} onClick={() => setPostTab('media')}>图片 / 视频</button><button className={postTab === 'audio' ? 'selected' : ''} onClick={() => setPostTab('audio')}>音频</button><button className={postTab === 'swap' ? 'selected' : ''} onClick={() => setPostTab('swap')}>换音轨</button><button className={postTab === 'keyframe' ? 'selected' : ''} onClick={() => setPostTab('keyframe')}>关键帧</button><button className={postTab === 'speech' ? 'selected' : ''} onClick={() => setPostTab('speech')}>配音</button><button className={postTab === 'dubbing' ? 'selected' : ''} onClick={() => setPostTab('dubbing')}>锚点配音</button><button className={postTab === 'observe' ? 'selected' : ''} onClick={() => setPostTab('observe')}>画面观察</button><button className={postTab === 'music' ? 'selected' : ''} onClick={() => setPostTab('music')}>配乐</button></div>
    {postTab === 'enhance' && <EnhancementPanel projectId={projectId} onBack={() => setPostTab('media')} />}
    {postTab === 'media' && <MediaToolsPanel projectId={projectId} assets={mediaAssets} defaultOpen onComplete={() => void onRefreshProject()} onBrowse={onBrowse} />}
    {postTab === 'audio' && <AudioEditPanel projectId={projectId} audioAssets={audioAssets} defaultOpen onComplete={() => void onRefreshProject()} onBrowse={onBrowse} />}
    {postTab === 'swap' && <AudioSwapPanel projectId={projectId} videos={videoAssets} audioAssets={audioAssets} onComplete={() => void onRefreshProject()} />}
    {postTab === 'keyframe' && <KeyframePanel projectId={projectId} defaultOpen options={projectMaterialOptions(projectDetail as never, plan?.segments as never || [])} onBrowse={onBrowse} onComplete={() => void onRefreshProject()} />}
    {postTab === 'speech' && <SpeechPanel projectId={projectId} defaultOpen options={projectMaterialOptions(projectDetail as never, plan?.segments as never || [])} onBrowse={onBrowse} onComplete={() => void onRefreshProject()} />}
    {postTab === 'dubbing' && <DubbingPanel projectId={projectId} videos={videoAssets} options={projectMaterialOptions(projectDetail as never, plan?.segments as never || [])} onBrowse={onBrowse} onComplete={() => void onRefreshProject()} />}
    {postTab === 'music' && <MusicPanel projectId={projectId} onComplete={() => void onRefreshProject()} />}
    {postTab === 'observe' && <ObservationPanel projectId={projectId} options={projectMaterialOptions(projectDetail as never, plan?.segments as never || [])} onBrowse={onBrowse} onComplete={() => void onRefreshProject()} />}
  </div>;
}

export default function StudioFlowLive({ username, onLogout }: { username: string; onLogout: () => void }) {
  const [screen, setScreen] = useState<'home' | 'series' | 'wizard' | 'quick'>('home');
  const [seriesNames, setSeriesNames] = useState<string[]>([]);
  const [activeSeries, setActiveSeries] = useState('');
  const [newProjectOpen, setNewProjectOpen] = useState(false);
  const [quickTool, setQuickTool] = useState<QuickTool>('image');
  const [quickSeed, setQuickSeed] = useState<QuickSeed | null>(null);
  const [projects, setProjects] = useState<ProjectView[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState('');
  const [step, setStep] = useState<Step>(1);
  const [projectId, setProjectId] = useState('');
  const [plan, setPlan] = useState<Plan | null>(null);
  const [presets, setPresets] = useState<Preset[]>([]);
  const [presetId, setPresetId] = useState('');
  const [workflow, setWorkflow] = useState('');
  const [title, setTitle] = useState('');
  const [series, setSeries] = useState('新作品');
  const [prompt, setPrompt] = useState('');
  const [shotDuration, setShotDuration] = useState(4);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [tool, setTool] = useState<Tool | null>(null);
  const [projectTitle, setProjectTitle] = useState('');
  const [projectDetail, setProjectDetail] = useState<ProjectDetail | null>(null);
  const [postTab, setPostTab] = useState<PostTab>('enhance');
  const [stageId, setStageId] = useState('');
  const [versions, setVersions] = useState<Version[]>([]);
  const [generationTask, setGenerationTask] = useState<{ id?: string; status?: string; error?: string } | null>(null);
  const [versionShot, setVersionShot] = useState('');
  const [reviews, setReviews] = useState<ReviewState>({});
  const [assembly, setAssembly] = useState<AssemblyView | null>(null);
  const [assemblyOutput, setAssemblyOutput] = useState('');
  const [assemblyTask, setAssemblyTask] = useState<{ id?: string; status?: string; error?: string } | null>(null);

  const loadProjects = async () => {
    setLoading(true); setLoadError('');
    try {
      const data = await api<{ projects: ProjectSummary[]; current_project_id?: string; default_project_id?: string }>('/api/projects');
      const allSummaries = data.projects || [];
    setSeriesNames(Array.from(new Set(allSummaries.map(item => item.series).filter(Boolean))).sort());
    void api<{ series: { name: string }[] }>('/api/private/series')
      .then(data => setSeriesNames((data.series || []).map(item => item.name).filter(Boolean).sort()))
      .catch(() => undefined);
    const summaries = allSummaries.slice(0, 8);
      const selectedId = data.current_project_id || data.default_project_id || summaries[0]?.id;
      const targets = [selectedId, ...summaries.map(item => item.id).filter(id => id !== selectedId)].filter(Boolean).slice(0, 5);
      const planById = new Map<string, Plan | null>();
      await Promise.all(targets.map(async id => {
        try { planById.set(id, await api<Plan>(`/api/projects/${id}/plan`, { signal: AbortSignal.timeout(3000) })); }
        catch { planById.set(id, null); }
      }));
      const views = summaries.map(summary => {
        const projectPlan = planById.get(summary.id) ?? null;
        return { ...summary, plan: projectPlan, ...derive(projectPlan) };
      });
      if (!views.length) { setProjects([]); return; }
      const selected = views.find(view => view.id === selectedId) || views[0];
      setProjects([selected, ...views.filter(view => view.id !== selected.id)]);
    } catch (reason) { setLoadError(reason instanceof Error ? reason.message : '无法读取作品'); }
    finally { setLoading(false); }
  };

  useEffect(() => {
    const deepLink = new URLSearchParams(window.location.search).get('project');
    if (deepLink) { void openProject(deepLink); } else { void loadProjects(); }
  }, []);
  useEffect(() => {
    if (!projectId) { setProjectDetail(null); return; }
    let cancelled = false;
    void api<ProjectDetail>(`/api/projects/${projectId}`).then(detail => { if (!cancelled) setProjectDetail(detail); }).catch(() => { if (!cancelled) setProjectDetail(null); });
    return () => { cancelled = true; };
  }, [projectId]);
  useEffect(() => {
    if (step === 2 && projectId && presets.length === 0) {
      void api<{ presets: Preset[] }>('/api/production-presets').then(catalog => setPresets(catalog.presets || [])).catch(() => setError('无法读取制作方案'));
    }
  }, [step, projectId]);
  useEffect(() => {
    if (!projectId || !plan) return;
    if (step === 4) {
      const shot = plan.segments.find(segment => segment.video?.path)?.id || plan.segments[0]?.id;
      if (shot) void loadVersions(shot);
      void loadAssembly();
    }
    if (step === 5) void loadAssembly();
    if (step === 6) void loadAssembly();
  }, [step, projectId]);

  const openProject = async (id: string) => {
    setBusy(true); setError('');
    try {
      const projectPlan = await api<Plan>(`/api/projects/${id}/plan`);
      const detail = await api<{ title?: string; series?: string; production_preset?: { id?: string }; pipeline?: { id: string; execution?: { references?: string[] } }[] }>(`/api/projects/${id}`);
      setProjectId(id); setTool(null); setProjectTitle(detail.title || '');
      setTitle(detail.title || ''); setSeries(detail.series || '新作品'); setPlan(projectPlan); setPresetId(detail.production_preset?.id || ''); setWorkflow(detail.pipeline?.[0]?.execution?.references?.[0] || ''); setStageId(detail.pipeline?.[0]?.id || '');
      setStep(projectPlan.assembly?.output ? 6 : projectPlan.segments.length ? 3 : 2); setScreen('wizard');
    } catch (reason) { setError(reason instanceof Error ? reason.message : '无法读取作品计划'); }
    finally { setBusy(false); }
  };

  const createProject = async (): Promise<boolean> => {
    if (!title.trim()) return false;
    setBusy(true); setError('');
    try {
      const created = await api<{ project: ProjectSummary }>('/api/projects/create', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ series: series.trim() || '新作品', title: title.trim(), creation_mode: 'advanced' }) });
      setProjectId(created.project.id); setTool(null); setProjectTitle(created.project.title || title.trim()); setPlan({ revision: 0, segments: [] });
      const catalog = await api<{ presets: Preset[] }>('/api/production-presets');
      setPresets(catalog.presets || []); setStep(2); return true;
    } catch (reason) { setError(reason instanceof Error ? reason.message : '创建作品失败'); return false; }
    finally { setBusy(false); }
  };

  const installPreset = async () => {
    if (!projectId || !presetId) return;
    setBusy(true); setError('');
    try {
      const installed = await api<{ project: { pipeline?: { id: string; execution?: { references?: string[] } }[] } }>(`/api/projects/${projectId}/production-presets/${encodeURIComponent(presetId)}`, { method: 'POST' });
      const stage = installed.project.pipeline?.[0];
      setWorkflow(stage?.execution?.references?.[0] || ''); setStageId(stage?.id || '');
      setStep(3);
      setPlan(await api<Plan>(`/api/projects/${projectId}/plan`));
    } catch (reason) { setError(reason instanceof Error ? reason.message : '安装方案失败'); }
    finally { setBusy(false); }
  };

  const addShot = async () => {
    if (!projectId || !prompt.trim()) return;
    setBusy(true); setError('');
    try {
      // 先取一次最新计划：页面上的 revision 可能是安装方案之前的旧值，
      // 用旧 revision 提交会被服务端以「计划已更新」拒绝，看起来就是"点了没反应"。
      let latest: Plan | null = null;
      try { latest = await api<Plan>(`/api/projects/${projectId}/plan`); setPlan(latest); } catch { latest = plan; }
      const revision = latest?.revision ?? 0;
      const segmentId = `S${String((latest?.segments.length || 0) + 1).padStart(2, '0')}`;
      await api(`/api/projects/${projectId}/plan/segments`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ segment_id: segmentId, expected_revision: revision, duration_seconds: shotDuration, prompt: prompt.trim(), preset_id: presetId || undefined, workflow: workflow || undefined, generation: { aspect_ratio: '16:9', seed: 42, sound_mode: 'performance_reference', speed_profile: 'standard', output_resolution: 'native' } }) });
      setPrompt(''); setPlan(await api<Plan>(`/api/projects/${projectId}/plan`));
    } catch (reason) {
      try { setPlan(await api<Plan>(`/api/projects/${projectId}/plan`)); } catch { /* keep the old plan */ }
      setError(reason instanceof Error ? reason.message : '添加镜头失败');
    } finally { setBusy(false); }
  };

  const openOldWorkspace = async () => {
    if (!projectId) { window.location.href = '/'; return; }
    try { await api('/api/projects/select', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ project_id: projectId }) }); } catch { /* selection is a convenience; old UI can still list projects */ }
    window.location.href = '/';
  };

  const loadVersions = async (shotId: string) => {
    setVersionShot(shotId); setError('');
    try {
      const data = await api<{ items: Version[] }>(`/api/projects/${projectId}/segments/${encodeURIComponent(shotId)}/versions`);
      setVersions(data.items || []);
    } catch (reason) { setError(reason instanceof Error ? reason.message : '读取候选失败'); }
  };

  const loadAssembly = async () => {
    if (!projectId) return;
    try {
      const [edit, currentPlan, state] = await Promise.all([
        api<AssemblyView>(`/api/projects/${projectId}/assembly/edit`),
        api<Plan & { assembly?: { output?: string; prompt_id?: string; status?: string } }>(`/api/projects/${projectId}/plan`),
        api<ReviewState>(`/api/projects/${projectId}/state`),
      ]);
      setAssembly(edit); setAssemblyOutput(currentPlan.assembly?.output || '');  setReviews(state); setPlan(currentPlan);
    } catch (reason) { setError(reason instanceof Error ? reason.message : '读取装配状态失败'); }
  };

  const adoptVersion = async (version: Version) => {
    if (!versionShot) return;
    setBusy(true); setError('');
    try {
      const record = reviews.reviews?.[versionShot]?.sample || reviews.reviews?.[versionShot]?.finish;
      await api(`/api/projects/${projectId}/adoptions`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ asset_id: versionShot, stage: 'sample', candidate_ref: version.video_path, expected_review_revision: record?.revision || 0, expected_plan_revision: plan?.revision || 0, note: '用户在新界面明确核对候选后采用', confirm_adoption: true }) });
      setPlan(await api<Plan>(`/api/projects/${projectId}/plan`));
      await loadVersions(versionShot);
    } catch (reason) { setError(reason instanceof Error ? reason.message : '采用失败'); }
    finally { setBusy(false); }
  };

  const setNativeSound = async () => {
    if (!assembly) return;
    setBusy(true); setError('');
    try {
      await api(`/api/projects/${projectId}/assembly/sound`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ expected_revision: assembly.revision, policy: 'segment_native' }) });
      await loadAssembly();
    } catch (reason) { setError(reason instanceof Error ? reason.message : '设置音轨失败'); }
    finally { setBusy(false); }
  };

  const approveJoin = async (join: Join) => {
    if (!assembly) return;
    setBusy(true); setError('');
    try {
      await api(`/api/projects/${projectId}/assembly/joins/${encodeURIComponent(join.left_id)}/${encodeURIComponent(join.right_id)}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ expected_revision: assembly.revision, status: 'approved', constraint: '', note: '用户在新界面明确审核接点' }) });
      await loadAssembly();
    } catch (reason) { setError(reason instanceof Error ? reason.message : '接点审核失败'); }
    finally { setBusy(false); }
  };

  const submitAssembly = async () => {
    if (!assembly) return;
    setBusy(true); setError('');
    try {
      const task = await api<{ id: string; status?: string }>(`/api/projects/${projectId}/assembly`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ idempotency_key: crypto.randomUUID(), expected_revision: assembly.revision }) });
      setAssemblyTask(task);
      for (let attempt = 0; attempt < 60; attempt += 1) {
        const current = await api<{ id: string; status: string; error?: string }>(`/api/projects/${projectId}/tasks/${encodeURIComponent(task.id)}`);
        setAssemblyTask(current);
        if (['succeeded', 'failed', 'stopped', 'needs_reconcile'].includes(current.status)) break;
        await new Promise(resolve => setTimeout(resolve, 3000));
      }
      await loadAssembly();
      if ((assemblyOutput || '') !== '') setStep(6);
      else setStep(6);
    } catch (reason) { setError(reason instanceof Error ? reason.message : '装配提交失败'); }
    finally { setBusy(false); }
  };

  const approveFinal = async () => {
    if (!assemblyOutput) return;
    setBusy(true); setError('');
    try {
      const record = reviews.reviews?.MASTER?.final;
      await api(`/api/projects/${projectId}/adoptions`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ asset_id: 'MASTER', stage: 'final', candidate_ref: assemblyOutput, expected_review_revision: record?.revision || 0, expected_plan_revision: plan?.revision || 0, note: '用户在新界面明确审核成片后采用', confirm_adoption: true }) });
      setError(''); setAssemblyTask(null); await loadAssembly();
    } catch (reason) { setError(reason instanceof Error ? reason.message : '成片采用失败'); }
    finally { setBusy(false); }
  };

  const generateCandidate = async (segment: Segment) => {
    if (!projectId || !stageId) { setError('当前作品没有已安装的制作方案，请先完成方案选择。'); return; }
    setBusy(true); setError(''); setGenerationTask({ status: '预检中…' });
    try {
      const values = { prompt: segment.prompt || '', seed: 42, duration: segment.duration_seconds || shotDuration, 'turbo-sampling': 'enable' };
      const validation = await api<{ valid: boolean; values?: Record<string, unknown>; revision?: number; blockers?: { message?: string }[] }>(`/api/projects/${projectId}/pipeline/${encodeURIComponent(stageId)}/validate`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ asset_id: segment.id, values }) });
      if (!validation.valid) throw new Error(validation.blockers?.map(item => item.message).filter(Boolean).join('；') || '预检未通过');
      const task = await api<{ id: string; status?: string }>(`/api/projects/${projectId}/tasks`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ idempotency_key: crypto.randomUUID(), asset_id: segment.id, expected_revision: validation.revision, pipeline_stage_id: stageId, pipeline_values: validation.values || values }) });
      setGenerationTask(task);
      for (let attempt = 0; attempt < 100; attempt += 1) {
        const current = await api<{ id: string; status: string; error?: string }>(`/api/projects/${projectId}/tasks/${encodeURIComponent(task.id)}`);
        setGenerationTask(current);
        if (['succeeded', 'failed', 'stopped', 'needs_reconcile'].includes(current.status)) break;
        await new Promise(resolve => setTimeout(resolve, 3000));
      }
      await loadVersions(segment.id);
      setPlan(await api<Plan>(`/api/projects/${projectId}/plan`));
    } catch (reason) { setError(reason instanceof Error ? reason.message : '生成失败'); }
    finally { setBusy(false); }
  };

  const refreshPlan = async () => { if (projectId) setPlan(await api<Plan>(`/api/projects/${projectId}/plan`)); };
  const refreshProject = async () => { if (projectId) setProjectDetail(await api<ProjectDetail>(`/api/projects/${projectId}`)); };

  const openShot = (shotId: string) => { setTool(null); setVersionShot(shotId); setStep(4); void loadVersions(shotId); };

  const startFull = () => { setTitle(''); setSeries('新作品'); setPlan(null); setProjectId(''); setProjectTitle(''); setPresetId(''); setTool(null); setError(''); setNewProjectOpen(true); };
  const home = () => { setScreen('home'); setActiveSeries(''); setError(''); void loadProjects(); };

  return <div className="sf-root">
    <TopBar username={username} projectTitle={screen === 'wizard' ? projectTitle : undefined} onHome={home} onLogout={onLogout} />
    {screen === 'home' && <Home projects={projects} seriesNames={seriesNames} continueProject={projects[0] || null} loading={loading} error={loadError} onStart={startFull} onOpen={id => void openProject(id)} onSeries={name => { setActiveSeries(name); setScreen('series'); }} onRetry={() => void loadProjects()} onQuick={tool => { setQuickSeed(null); setQuickTool(tool); setScreen('quick'); }} onDeleted={() => void loadProjects()} onUsePrompt={(text, target) => { setQuickSeed({ prompt: text }); setQuickTool(target); setScreen('quick'); }} onUseFrame={path => { setQuickSeed({ frame: path }); setQuickTool('image-video'); setScreen('quick'); }} onUseTimbre={description => { setQuickSeed({ timbre: description }); setQuickTool('timbre'); setScreen('quick'); }} onUseMusic={card => { setQuickSeed({ music: card }); setQuickTool('music'); setScreen('quick'); }} />}
    {screen === 'quick' && <Suspense fallback={<div className="sf-center"><LoaderCircle className="sf-spin" size={20} /><p>正在载入工具…</p></div>}><QuickToolPage tool={quickTool} seed={quickSeed} onBack={() => setScreen('home')} /></Suspense>}
    {newProjectOpen && <div className="sf-modal-backdrop" role="dialog" aria-modal="true" aria-label="新建项目"><div className="sf-modal"><button className="sf-modal-close" onClick={() => setNewProjectOpen(false)} aria-label="关闭"><X size={18} /></button><p className="sf-step-label">新建</p><h2>新建项目</h2><p className="sf-modal-lede">项目只是一个结果容器；创建后再选择方案、镜头和生成工具。</p><label className="sf-field"><span>项目名</span><input value={title} onChange={event => setTitle(event.target.value)} placeholder="例如：咖啡测试" /></label><label className="sf-field"><span>系列</span><input value={series} onChange={event => setSeries(event.target.value)} placeholder="例如：单步工具" /></label><ReferenceHints query={title} />{error && <p className="sf-error">{error}</p>}<div className="sf-step-actions"><button className="sf-primary" disabled={busy || !title.trim()} onClick={() => { void createProject().then(created => { if (created) { setNewProjectOpen(false); setScreen('wizard'); } }); }}>{busy ? '创建中…' : '创建项目'}</button></div></div></div>}
    {screen === 'series' && <SeriesResources series={activeSeries} episodes={projects.filter(project => project.series === activeSeries)} onBack={() => setScreen('home')} onOpen={id => void openProject(id)} />}
        {screen === 'wizard' && <Wizard step={step} tool={tool} projectReady={Boolean(projectId)} projectTitle={projectTitle} onStep={next => { setTool(null); setStep(next); }} onTool={setTool} onHome={home}>
      {tool && projectId ? <Suspense fallback={<div className="sf-center"><LoaderCircle className="sf-spin" size={20} /><p>正在载入工具…</p></div>}><ToolPage tool={tool} projectId={projectId} plan={plan} projectDetail={projectDetail} postTab={postTab} setPostTab={setPostTab} onStep={next => { setTool(null); setStep(next); }} onRefreshPlan={refreshPlan} onRefreshProject={refreshProject} onOpenShot={openShot} onBrowse={() => setTool('materials')} onDeleted={() => { void loadProjects(); setTool(null); setScreen('home'); }} /></Suspense> : <>
      {step === 2 && <StepTwo presets={presets} selected={presetId} setSelected={setPresetId} busy={busy} error={error} onNext={() => void installPreset()} />}
      {step === 3 && <StepThree segments={plan?.segments || []} planRevision={plan?.revision ?? 0} prompt={prompt} setPrompt={setPrompt} duration={shotDuration} setDuration={setShotDuration} onAdd={() => void addShot()} busy={busy} error={error} onNext={() => setStep(4)} />}
      {step === 4 && <StepFour segments={plan?.segments || []} shotId={versionShot} setShotId={setVersionShot} versions={versions} busy={busy} error={error} onLoad={id => void loadVersions(id)} onAdopt={version => void adoptVersion(version)} onNext={() => setStep(5)} onGenerate={segment => void generateCandidate(segment)} generating={busy} task={generationTask} />}
      {step === 5 && <StepFive assembly={assembly} task={assemblyTask} busy={busy} error={error} onSound={() => void setNativeSound()} onJoin={join => void approveJoin(join)} onSubmit={() => void submitAssembly()} onReload={() => void loadAssembly()} />}
      {step === 6 && <StepSix output={assemblyOutput} assembly={assembly} busy={busy} error={error} onFinal={() => void approveFinal()} onOld={() => void openOldWorkspace()} />}
      {step > 2 && step < 4 && <div className="sf-bottom-bar"><button className="sf-secondary" onClick={() => setStep((step - 1) as Step)}><ChevronLeft size={15} /> 上一步</button><span>每一步都可以保存后离开</span></div>}
      </>}
    </Wizard>}
  </div>;
}







