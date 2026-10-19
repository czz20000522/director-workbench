import { useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import {
  ArrowRight, Check, ChevronLeft, ChevronRight, Clapperboard, FileText,
  Film, Image, Layers3, LoaderCircle, Plus, Play, Sparkles, Upload, Volume2, Wand2, X,
} from 'lucide-react';
import './studio-flow-prototype.css';

type Step = 1 | 2 | 3 | 4 | 5 | 6;
type QuickTool = 'text-video' | 'image-video' | 'text-image' | 'enhance';
type ProjectSummary = { id: string; title: string; series: string; selected?: boolean };
type Segment = { id: string; duration_seconds?: number; prompt?: string; status?: string; video?: { path?: string } };
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

const steps = [
  { id: 1, label: '愿望', hint: '故事与规格' },
  { id: 2, label: '方案', hint: '显式选择' },
  { id: 3, label: '分镜', hint: '逐段准备' },
  { id: 4, label: '候选', hint: '现有工作台' },
  { id: 5, label: '装配', hint: '现有工作台' },
  { id: 6, label: '交付', hint: '现有工作台' },
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
  if (!plan.segments.length) return { step: '③ 分镜', progress: 25 };
  if (!plan.segments.some(segment => Boolean(segment.video?.path))) return { step: '③ 分镜', progress: 50 };
  return { step: '④ 候选', progress: 70 };
}

function Thumb({ tone = 'rain', label, film = false }: { tone?: string; label?: string; film?: boolean }) {
  return <div className={`sf-thumb sf-thumb-${tone}`} aria-hidden="true"><span>{label ?? '01'}</span>{film && <Clapperboard size={22} />}</div>;
}

function TopBar({ username, onHome, onLogout }: { username: string; onHome: () => void; onLogout: () => void }) {
  return (
    <header className="sf-topbar">
      <button className="sf-brand" onClick={onHome} aria-label="返回首页">
        <span className="sf-brand-mark"><Clapperboard size={18} /></span><span><strong>导演工作台</strong></span>
      </button>
      <div className="sf-topbar-center"><span className="sf-dot" /> 本机工作区 <span className="sf-sep">/</span> 新界面预览</div>
      <div className="sf-topbar-actions"><span className="sf-account">{(username || 'U').slice(0, 1).toUpperCase()}</span><button className="sf-ghost" onClick={onLogout}>退出</button></div>
    </header>
  );
}

function Home({ projects, continueProject, loading, error, onStart, onQuick, onOpen, onRetry }: {
  projects: ProjectView[]; continueProject: ProjectView | null; loading: boolean; error: string;
  onStart: () => void; onQuick: (tool: QuickTool) => void; onOpen: (id: string) => void; onRetry: () => void;
}) {
  if (loading) return <main className="sf-page sf-center"><LoaderCircle className="sf-spin" size={22} /><p>正在读取作品…</p></main>;
  if (error) return <main className="sf-page sf-center"><p role="alert">{error}</p><button className="sf-secondary" onClick={onRetry}>重试</button></main>;
  return (
    <main className="sf-page sf-home">
      <section className="sf-home-head">
        <div><h1>继续上次</h1><p className="sf-lede">打开就能接着上次的分镜、候选或装配继续。</p></div>
        <button className="sf-secondary sf-large" onClick={onStart}><Plus size={17} /> 新建影片</button>
      </section>
      {continueProject ? (
        <button className="sf-continue-hero" onClick={() => onOpen(continueProject.id)}>
          <Thumb tone="rain" label={continueProject.title} film />
          <div className="sf-continue-main">
            <div className="sf-continue-meta"><span className="sf-chip sf-chip-live">草稿 · {continueProject.step}</span><span>{continueProject.plan?.segments.length ?? 0} 个镜头</span></div>
            <h2>{continueProject.title}</h2>
            <p>{continueProject.series} · 继续准备分镜或进入现有工作台生成候选。</p>
            <div className="sf-progress"><span style={{ width: `${continueProject.progress}%` }} /></div>
            <small>继续制作</small>
          </div>
          <span className="sf-continue-cta"><span className="sf-primary">继续创作 <ArrowRight size={17} /></span><small>{continueProject.step}</small></span>
        </button>
      ) : (
        <section className="sf-empty-card"><Film size={22} /><div><h2>还没有作品</h2><p>从一句愿望开始，工作台会一步步让你决定方案、分镜和候选。</p></div><button className="sf-primary" onClick={onStart}>新建第一部作品</button></section>
      )}
      {projects.length > 0 && <section className="sf-section">
        <div className="sf-section-head"><h2>最近作品</h2><span>{projects.length} 部</span></div>
        <div className="sf-recent-grid">
          {projects.slice(0, 8).map(project => (
            <button className="sf-recent-card" key={project.id} onClick={() => onOpen(project.id)}>
              <Thumb tone={project.id === continueProject?.id ? 'rain' : 'sage'} label={project.title} />
              <div className="sf-recent-copy"><span className="sf-chip">{project.step}</span><h3>{project.title}</h3><p>{project.series}</p></div>
              <span className="sf-recent-progress">{project.progress}%</span>
            </button>
          ))}
        </div>
      </section>}
      <section className="sf-section sf-quick-section"><details className="sf-quick"><summary><span><strong>快速开始</strong><small>单步工具，使用现有工作台</small></span><ChevronRight size={17} /></summary><div className="sf-quick-grid">
        <button onClick={() => onQuick('text-video')}><Play size={19} /><strong>文生视频</strong><span>只生成一个镜头</span></button>
        <button onClick={() => onQuick('image-video')}><Film size={19} /><strong>图生视频</strong><span>让一张图动起来</span></button>
        <button onClick={() => onQuick('text-image')}><Image size={19} /><strong>文生图</strong><span>先生成一张素材</span></button>
        <button onClick={() => onQuick('enhance')}><Wand2 size={19} /><strong>清晰化</strong><span>处理已有图片或视频</span></button>
      </div></details></section>
    </main>
  );
}

function Intent({ onClose, onFull, onQuick }: { onClose: () => void; onFull: () => void; onQuick: (tool: QuickTool) => void }) {
  return <div className="sf-modal-backdrop" role="dialog" aria-modal="true" aria-label="选择创作意图"><div className="sf-modal">
    <button className="sf-modal-close" onClick={onClose} aria-label="关闭"><X size={18} /></button>
    <p className="sf-step-label">新建</p><h2>你想做什么？</h2><p className="sf-modal-lede">整片流程和单步工具分开。</p>
    <button className="sf-intent-primary" onClick={onFull}><span className="sf-intent-icon"><Clapperboard size={20} /></span><span><strong>做一部完整影片</strong><small>愿望 → 方案 → 分镜 → 候选 → 装配 → 成片</small></span><ArrowRight size={18} /></button>
    <div className="sf-intent-grid">
      <button onClick={() => onQuick('text-video')}><Play size={18} /><strong>只生成一个镜头</strong><small>使用现有工作台</small></button>
      <button onClick={() => onQuick('text-image')}><Image size={18} /><strong>只生成一张图</strong><small>使用现有工作台</small></button>
      <button onClick={() => onQuick('enhance')}><Wand2 size={18} /><strong>处理已有素材</strong><small>使用现有工作台</small></button>
    </div>
    <div className="sf-zone-pick"><span>内容分区</span><button className="sf-zone selected">普通创作</button><button className="sf-zone">成人向创作 <small>需明确边界</small></button></div>
  </div></div>;
}

function QuickGateway({ tool, onBack }: { tool: QuickTool; onBack: () => void }) {
  const info = {
    'text-video': ['文生视频', '只生成一个镜头'], 'image-video': ['图生视频', '让一张图动起来'],
    'text-image': ['文生图', '先生成一张素材'], enhance: ['清晰化', '处理已有图片或视频'],
  }[tool];
  return <main className="sf-quick-page"><button className="sf-back" onClick={onBack}><ChevronLeft size={16} /> 返回首页</button><section className="sf-quick-panel"><div className="sf-quick-icon"><Wand2 size={24} /></div><p className="sf-step-label">快速工具</p><h1>{info[0]}</h1><p>{info[1]}。该入口仍使用现有工作台，下一步再并入新流程。</p><div className="sf-quick-actions"><button className="sf-primary" onClick={() => { window.location.href = '/'; }}>打开现有工作台 <ArrowRight size={16} /></button><button className="sf-secondary" onClick={onBack}>返回</button></div></section></main>;
}

function StepRail({ step, onStep }: { step: Step; onStep: (step: Step) => void }) {
  return <aside className="sf-rail"><div className="sf-rail-head"><span>创作流程</span><strong>新作品</strong><small>草稿自动保存</small></div><ol>{steps.map(item => <li key={item.id} className={item.id === step ? 'active' : item.id < step ? 'done' : ''}><button onClick={() => onStep(item.id as Step)} disabled={item.id > step}><span className="sf-step-dot">{item.id < step ? <Check size={12} /> : item.id}</span><span><strong>{item.label}</strong><small>{item.hint}</small></span></button></li>)}</ol></aside>;
}

function Wizard({ step, onStep, onHome, children }: { step: Step; onStep: (step: Step) => void; onHome: () => void; children: ReactNode }) {
  return <div className="sf-workspace"><StepRail step={step} onStep={onStep} /><main className="sf-main"><div className="sf-workspace-top"><button className="sf-back" onClick={onHome}><ChevronLeft size={15} /> 全部作品</button><span className="sf-save-state"><span className="sf-dot" /> 自动保存</span></div>{children}</main></div>;
}

function StepOne({ title, setTitle, series, setSeries, duration, setDuration, onNext, busy, error }: {
  title: string; setTitle: (value: string) => void; series: string; setSeries: (value: string) => void;
  duration: number; setDuration: (value: number) => void; onNext: () => void; busy: boolean; error: string;
}) {
  return <div className="sf-step-panel"><p className="sf-step-label">① 愿望</p><h1>这部片想讲什么</h1><p className="sf-lede">先写清楚故事和规格。后面每一步都会给你明确的选择。</p>
    <div className="sf-form-grid">
      <label className="sf-field sf-wide"><span>作品名</span><input value={title} onChange={event => setTitle(event.target.value)} placeholder="例如：雨停之前" /></label>
      <label className="sf-field"><span>系列</span><input value={series} onChange={event => setSeries(event.target.value)} placeholder="例如：短片 · 概念" /></label>
      <label className="sf-field"><span>目标时长</span><select value={duration} onChange={event => setDuration(Number(event.target.value))}><option value={15}>15 秒</option><option value={30}>30 秒</option><option value={60}>60 秒</option></select></label>
      <div className="sf-field sf-wide"><span>起点</span><div className="sf-choice-row"><button className="sf-choice selected"><Sparkles size={17} /><strong>原创</strong><small>从文字开始</small></button><button className="sf-choice"><Upload size={17} /><strong>参考</strong><small>后续接入</small></button><button className="sf-choice"><FileText size={17} /><strong>剧本</strong><small>后续接入</small></button></div></div>
    </div>
    {error && <p className="sf-error" role="alert">{error}</p>}
    <div className="sf-step-actions"><button className="sf-primary" disabled={busy || !title.trim()} onClick={onNext}>{busy ? '创建中…' : '创建并选择方案'} <ArrowRight size={16} /></button></div>
  </div>;
}

function StepTwo({ presets, selected, setSelected, onNext, busy, error }: { presets: Preset[]; selected: string; setSelected: (id: string) => void; onNext: () => void; busy: boolean; error: string }) {
  return <div className="sf-step-panel"><p className="sf-step-label">② 方案</p><h1>选择制作方案</h1><p className="sf-lede">只展示事实和限制，不替你推荐。选中的方案决定后面可用的输入和输出。</p>
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
  return <div className="sf-step-panel"><p className="sf-step-label">③ 分镜</p><h1>准备分镜</h1><p className="sf-lede">每一段独立准备；先生成、审核和重做的能力仍由现有工作台完成。</p>
    <div className="sf-shot-list">{segments.map((segment, index) => <article className="sf-shot" key={segment.id}><div className="sf-shot-number">{String(index + 1).padStart(2, '0')}</div><Thumb tone={index % 2 ? 'sage' : 'amber'} label={segment.id} /><div className="sf-shot-copy"><div className="sf-shot-head"><strong>{segment.id}</strong><span className="sf-chip">{segment.duration_seconds ?? '—'}s</span></div><p>{segment.prompt || '尚未填写提示词'}</p></div><div className="sf-shot-side"><span className={`sf-state ${segment.video?.path ? 'ok' : ''}`}>{segment.video?.path ? '已有候选' : '待生成'}</span></div></article>)}</div>
    <div className="sf-inline-form"><label className="sf-field"><span>新镜头提示词</span><textarea rows={2} value={prompt} onChange={event => setPrompt(event.target.value)} placeholder="描述这个镜头…" /></label><label className="sf-field sf-duration"><span>时长（秒）</span><input type="number" min={1} max={15} value={duration} onChange={event => setDuration(Number(event.target.value))} /></label><button className="sf-secondary" disabled={busy || !prompt.trim()} onClick={onAdd}>{busy ? '添加中…' : <><Plus size={15} /> 添加镜头</>}</button></div>
    {error && <p className="sf-error" role="alert">{error}</p>}
    <div className="sf-step-actions"><span className="sf-fine">当前计划 revision {planRevision}</span><button className="sf-primary" disabled={!segments.length} onClick={onNext}>下一步：进入候选 <ArrowRight size={16} /></button></div>
  </div>;
}

function mediaFile(path: string) { return `/media-file?path=${encodeURIComponent(path)}`; }

function StepFour({ segments, shotId, setShotId, versions, busy, error, onLoad, onAdopt, onOld, onNext }: {
  segments: Segment[]; shotId: string; setShotId: (id: string) => void; versions: Version[]; busy: boolean; error: string;
  onLoad: (id: string) => void; onAdopt: (version: Version) => void; onOld: () => void; onNext: () => void;
}) {
  const current = segments.find(segment => segment.id === shotId) || segments[0];
  return <div className="sf-step-panel"><p className="sf-step-label">④ 候选</p><h1>比较候选</h1><p className="sf-lede">选择一段查看已有生成结果。工作台不会自动选“最佳”，采用只影响这一段。</p>
    <div className="sf-shot-tabs">{segments.map(segment => <button key={segment.id} className={segment.id === (current?.id || '') ? 'selected' : ''} onClick={() => { setShotId(segment.id); onLoad(segment.id); }}>{segment.id}<span>{segment.video?.path ? '有候选' : '待生成'}</span></button>)}</div>
    {versions.length ? <div className="sf-version-grid">{versions.map(version => <article className="sf-version" key={version.task_id}><video src={mediaFile(version.video_path)} controls preload="metadata" /><div><strong>{version.is_current ? '当前版本' : '历史候选'}</strong><small>{version.generated_at || version.task_id.slice(0, 8)}</small><button className="sf-secondary" disabled={busy} onClick={() => onAdopt(version)}>采用这个候选</button></div></article>)}</div>
      : <div className="sf-next-card"><Film size={22} /><div><strong>这一段还没有生成候选</strong><small>生成和重做仍使用现有工作台；先生成，再回来做逐段采用。</small></div><button className="sf-primary" onClick={onOld}>去现有工作台生成 <ArrowRight size={16} /></button></div>}
    {error && <p className="sf-error" role="alert">{error}</p>}
    <div className="sf-step-actions"><span className="sf-fine">采用是显式操作；不会自动采用其他镜头。</span><button className="sf-primary" disabled={!segments.every(segment => segment.video?.path)} onClick={onNext}>下一步：装配 <ArrowRight size={16} /></button></div>
  </div>;
}

function StepFive({ assembly, task, busy, error, onSound, onJoin, onSubmit, onReload, onOld }: {
  assembly: AssemblyView | null; task: { id?: string; status?: string; error?: string } | null; busy: boolean; error: string; onSound: () => void; onJoin: (join: Join) => void;
  onSubmit: () => void; onReload: () => void; onOld: () => void;
}) {
  if (!assembly) return <div className="sf-step-panel"><p className="sf-step-label">⑤ 装配</p><h1>检查后生成成片</h1><div className="sf-next-card"><Layers3 size={22} /><div><strong>正在读取装配状态</strong><small>需要先生成至少一个采用候选。</small></div><button className="sf-secondary" onClick={onReload}>重新读取</button><button className="sf-secondary" onClick={onOld}>现有工作台</button></div>{error && <p className="sf-error">{error}</p>}</div>;
  const joins = assembly.joins || [];
  return <div className="sf-step-panel"><p className="sf-step-label">⑤ 装配</p><h1>检查后生成成片</h1><p className="sf-lede">采用、接点、音轨和连续性都确认后，才允许生成成片。</p>
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
  return <div className="sf-step-panel"><p className="sf-step-label">⑥ 交付</p><h1>成片</h1>{output ? <div className="sf-final-grid"><div className="sf-film"><video src={mediaFile(output)} controls preload="metadata" /><span className="sf-film-duration">{assembly?.revision !== undefined ? `计划 revision ${assembly.revision}` : ''}</span></div><div className="sf-final-meta"><h2>成片候选</h2><p>通过全片审核后才算交付；下载不等于采用。</p><div className="sf-final-actions"><button className="sf-primary" disabled={busy} onClick={onFinal}>{busy ? '提交中…' : '通过全片审核'}</button><a className="sf-secondary" href={mediaFile(output)} download>下载</a></div><button className="sf-secondary sf-wide-button" onClick={onOld}>返回现有工作台</button></div></div> : <div className="sf-next-card"><Film size={22} /><div><strong>还没有成片</strong><small>完成装配后在这里预览和审核。</small></div><button className="sf-secondary" onClick={onOld}>现有工作台</button></div>}{error && <p className="sf-error">{error}</p>}</div>;
}

export default function StudioFlowLive({ username, onLogout }: { username: string; onLogout: () => void }) {
  const [screen, setScreen] = useState<'home' | 'intent' | 'wizard' | 'quick'>('home');
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
  const [duration, setDuration] = useState(15);
  const [prompt, setPrompt] = useState('');
  const [shotDuration, setShotDuration] = useState(4);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [quickTool, setQuickTool] = useState<QuickTool>('text-video');
  const [versions, setVersions] = useState<Version[]>([]);
  const [versionShot, setVersionShot] = useState('');
  const [reviews, setReviews] = useState<ReviewState>({});
  const [assembly, setAssembly] = useState<AssemblyView | null>(null);
  const [assemblyOutput, setAssemblyOutput] = useState('');
  const [assemblyTask, setAssemblyTask] = useState<{ id?: string; status?: string; error?: string } | null>(null);

  const loadProjects = async () => {
    setLoading(true); setLoadError('');
    try {
      const data = await api<{ projects: ProjectSummary[]; current_project_id?: string; default_project_id?: string }>('/api/projects');
      const summaries = (data.projects || []).slice(0, 8);
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
      const detail = await api<{ production_preset?: { id?: string }; pipeline?: { id: string; execution?: { references?: string[] } }[] }>(`/api/projects/${id}`);
      setProjectId(id); setPlan(projectPlan); setPresetId(detail.production_preset?.id || ''); setWorkflow(detail.pipeline?.[0]?.execution?.references?.[0] || '');
      setStep(projectPlan.assembly?.output ? 6 : projectPlan.segments.length ? 3 : 1); setScreen('wizard');
    } catch (reason) { setError(reason instanceof Error ? reason.message : '无法读取作品计划'); }
    finally { setBusy(false); }
  };

  const createProject = async () => {
    if (!title.trim()) return;
    setBusy(true); setError('');
    try {
      const created = await api<{ project: ProjectSummary }>('/api/projects/create', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ series: series.trim() || '新作品', title: title.trim(), creation_mode: 'advanced' }) });
      setProjectId(created.project.id); setPlan({ revision: 0, segments: [] });
      const catalog = await api<{ presets: Preset[] }>('/api/production-presets');
      setPresets(catalog.presets || []); setStep(2);
    } catch (reason) { setError(reason instanceof Error ? reason.message : '创建作品失败'); }
    finally { setBusy(false); }
  };

  const installPreset = async () => {
    if (!projectId || !presetId) return;
    setBusy(true); setError('');
    try {
      const installed = await api<{ project: { pipeline?: { id: string; execution?: { references?: string[] } }[] } }>(`/api/projects/${projectId}/production-presets/${encodeURIComponent(presetId)}`, { method: 'POST' });
      const stage = installed.project.pipeline?.[0];
      setWorkflow(stage?.execution?.references?.[0] || '');
      setStep(3);
      setPlan(await api<Plan>(`/api/projects/${projectId}/plan`));
    } catch (reason) { setError(reason instanceof Error ? reason.message : '安装方案失败'); }
    finally { setBusy(false); }
  };

  const addShot = async () => {
    if (!projectId || !prompt.trim()) return;
    setBusy(true); setError('');
    try {
      const segmentId = `S${String((plan?.segments.length || 0) + 1).padStart(2, '0')}`;
      await api(`/api/projects/${projectId}/plan/segments`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ segment_id: segmentId, expected_revision: plan?.revision ?? 0, duration_seconds: shotDuration, prompt: prompt.trim(), preset_id: presetId || undefined, workflow: workflow || undefined, generation: { aspect_ratio: '16:9', seed: 42, sound_mode: 'performance_reference', speed_profile: 'standard', output_resolution: 'native' } }) });
      setPrompt(''); setPlan(await api<Plan>(`/api/projects/${projectId}/plan`));
    } catch (reason) { setError(reason instanceof Error ? reason.message : '添加镜头失败'); }
    finally { setBusy(false); }
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

  const startFull = () => { setTitle(''); setSeries('新作品'); setDuration(15); setPlan(null); setProjectId(''); setPresetId(''); setError(''); setStep(1); setScreen('wizard'); };
  const newProject = () => setScreen('intent');
  const quick = (tool: QuickTool) => { setQuickTool(tool); setScreen('quick'); };
  const home = () => { setScreen('home'); setError(''); void loadProjects(); };

  return <div className="sf-root">
    <TopBar username={username} onHome={home} onLogout={onLogout} />
    {screen === 'home' && <Home projects={projects} continueProject={projects[0] || null} loading={loading} error={loadError} onStart={newProject} onQuick={quick} onOpen={id => void openProject(id)} onRetry={() => void loadProjects()} />}
    {screen === 'intent' && <Intent onClose={() => setScreen('home')} onFull={startFull} onQuick={quick} />}
    {screen === 'quick' && <QuickGateway tool={quickTool} onBack={() => setScreen('home')} />}
    {screen === 'wizard' && <Wizard step={step} onStep={setStep} onHome={home}>
      {step === 1 && <StepOne title={title} setTitle={setTitle} series={series} setSeries={setSeries} duration={duration} setDuration={setDuration} busy={busy} error={error} onNext={() => void createProject()} />}
      {step === 2 && <StepTwo presets={presets} selected={presetId} setSelected={setPresetId} busy={busy} error={error} onNext={() => void installPreset()} />}
      {step === 3 && <StepThree segments={plan?.segments || []} planRevision={plan?.revision ?? 0} prompt={prompt} setPrompt={setPrompt} duration={shotDuration} setDuration={setShotDuration} onAdd={() => void addShot()} busy={busy} error={error} onNext={() => setStep(4)} />}
      {step === 4 && <StepFour segments={plan?.segments || []} shotId={versionShot} setShotId={setVersionShot} versions={versions} busy={busy} error={error} onLoad={id => void loadVersions(id)} onAdopt={version => void adoptVersion(version)} onOld={() => void openOldWorkspace()} onNext={() => setStep(5)} />}
      {step === 5 && <StepFive assembly={assembly} task={assemblyTask} busy={busy} error={error} onSound={() => void setNativeSound()} onJoin={join => void approveJoin(join)} onSubmit={() => void submitAssembly()} onReload={() => void loadAssembly()} onOld={() => void openOldWorkspace()} />}
      {step === 6 && <StepSix output={assemblyOutput} assembly={assembly} busy={busy} error={error} onFinal={() => void approveFinal()} onOld={() => void openOldWorkspace()} />}
      {step > 1 && step < 4 && <div className="sf-bottom-bar"><button className="sf-secondary" onClick={() => setStep((step - 1) as Step)}><ChevronLeft size={15} /> 上一步</button><span>每一步都可以保存后离开</span></div>}
    </Wizard>}
  </div>;
}

