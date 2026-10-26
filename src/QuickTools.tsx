import { lazy, useEffect, useState } from 'react';
import { AudioLines, BookOpen, ChevronLeft, Image, LoaderCircle, Music, Play, Upload, Wand2 } from 'lucide-react';
import { mediaUrl, request } from './api/client';
import type { OutputResolution } from './ResolutionPicker';
import { capVideoQuality, detectQualityIntent, qualityLabel } from './qualityIntent';
import { PromptLibraryDialog } from './ReferenceLibrary';
import ProgressBar, { type TaskProgress } from './TaskProgress';

const KeyframePanel = lazy(() => import('./KeyframePanel'));
const EnhancementPanel = lazy(() => import('./EnhancementPanel'));
const MusicPanel = lazy(() => import('./MusicPanel'));
const TimbrePanel = lazy(() => import('./TimbrePanel'));

export type QuickTool = 'image' | 'text-video' | 'image-video' | 'enhance' | 'music' | 'timbre';
export type QuickSeed = { prompt?: string; frame?: string; timbre?: string; music?: Record<string, unknown> };
// 点一下才写进提示词；措辞点明"画外、人物不开口"，这是 H3 把旁白当对白的常见原因。
const NARRATION_HINT = '声音：全程画外旁白，镜头里的人物不开口、没有对白口型；人声清楚靠前，环境声压低。';
type Tool = QuickTool;
type Preset = { id: string; title: string; description?: string; available?: boolean; aspect_ratio?: '16:9' | '9:16' | '1:1' };
const videoClarity: { value: OutputResolution; label: string; note: string }[] = [
  { value: 'native', label: '1K', note: '原生' },
  { value: '2k', label: '2K', note: '放大' },
  { value: '4k', label: '4K', note: '放大' },
];
type Task = { id: string; status: string; error?: string; progress?: TaskProgress };
type Version = { task_id: string; video_path: string; is_current?: boolean };

const meta: Record<Tool, { title: string; note: string; icon: typeof Image }> = {
  image: { title: '文生图', note: '输入提示词，生成一张图片。工作台只执行你明确提交的任务。', icon: Image },
  'text-video': { title: '文生视频', note: '选择制作方案，输入提示词，生成一个镜头。', icon: Play },
  'image-video': { title: '图生视频', note: '上传一张开始画面，选择图生方案，生成一个镜头。', icon: Upload },
  enhance: { title: '清晰化', note: '上传图片或视频，提高分辨率。', icon: Wand2 },
  music: { title: '配乐', note: '写风格标签与时长，生成一段新音乐候选。', icon: Music },
  timbre: { title: '音色设计', note: 'VoxCPM2 凭文字造音色，或 IndexTTS 克隆参考音频，出可复用样本。', icon: AudioLines },
};

const statusLabels: Record<string, string> = {
  scheduler_waiting: '等待调度', batch_waiting: '批次等待', preparing: '准备中', submitting: '提交中',
  queued: '排队中', running: '生成中', stop_requested: '停止中', stopping: '正在停止',
  needs_reconcile: '需要核对结果', succeeded: '已生成', failed: '生成失败', stopped: '已停止',
};

let quickWorkspacePromise: Promise<string> | null = null;

/** 单步工具共用一个隐藏容器；打开页面不再产生新作品。 */
function quickWorkspace(): Promise<string> {
  if (!quickWorkspacePromise) {
    quickWorkspacePromise = request<{ project_id: string }>('/api/quick-workspace')
      .then(data => data.project_id)
      .catch(reason => { quickWorkspacePromise = null; throw reason; });
  }
  return quickWorkspacePromise;
}

function QuickVideo({ mode, initialFrame, initialPrompt }: { mode: 'text-video' | 'image-video'; initialFrame?: string; initialPrompt?: string }) {
  const [presets, setPresets] = useState<Preset[]>([]);
  const [presetId, setPresetId] = useState('');
  const [prompt, setPrompt] = useState(initialPrompt || '');
  const [libraryOpen, setLibraryOpen] = useState(false);
  const [duration, setDuration] = useState(4);
  const [quality, setQuality] = useState<OutputResolution>('native');
  const [firstFrame, setFirstFrame] = useState(initialFrame || '');
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState('');
  const [progress, setProgress] = useState<TaskProgress | null>(null);
  const [video, setVideo] = useState('');
  const [error, setError] = useState('');
  const [projectId, setProjectId] = useState('');

  useEffect(() => {
    let cancelled = false;
    void request<{ presets: Preset[] }>('/api/production-presets').then(data => {
      if (!cancelled) setPresets((data.presets || []).filter(preset => preset.available !== false));
    }).catch(reason => { if (!cancelled) setError(reason instanceof Error ? reason.message : '无法读取制作方案'); });
    return () => { cancelled = true; };
  }, []);

  const ensureProject = async () => {
    if (projectId) return projectId;
    const id = await quickWorkspace();
    setProjectId(id); return id;
  };

  const upload = async (file: File) => {
    const id = await ensureProject();
    const form = new FormData(); form.append('files', file, `assets/${file.name}`);
    const data = await request<{ uploaded?: string[] }>(`/api/projects/${id}/upload`, { init: { method: 'POST', body: form } });
    setFirstFrame(data.uploaded?.[0] || '');
  };

  // 提示词里写明的画质优先于选择器；8K 视频按规则显式限到 4K。
  const promptIntent = detectQualityIntent(prompt);
  const appliedQuality = promptIntent ? capVideoQuality(promptIntent.resolution) : quality;
  const qualityCapped = promptIntent ? appliedQuality !== promptIntent.resolution : false;
  const aspect = presets.find(item => item.id === presetId)?.aspect_ratio ?? '16:9';

  const submit = async () => {
    if (mode === 'image-video' && !firstFrame) { setError('请先上传开始画面'); return; }
    if (!presetId) { setError('请显式选择一个制作方案'); return; }
    if (!prompt.trim()) { setError('请填写提示词'); return; }
    setBusy(true); setError(''); setStatus('准备单步任务…'); setProgress(null);
    try {
      const id = await ensureProject();
      setStatus('安装制作方案…');
      const installed = await request<{ project: { pipeline?: { id: string; execution?: { references?: string[] } }[] } }>(`/api/projects/${id}/production-presets/${encodeURIComponent(presetId)}`, { init: { method: 'POST' } });
      const stage = installed.project.pipeline?.[0];
      if (!stage) throw new Error('方案未返回可执行阶段');
      const workflow = stage.execution?.references?.[0] || '';
      setStatus('登记镜头…');
      await request(`/api/projects/${id}/plan/segments`, {
        init: { method: 'POST' }, json: {
          segment_id: 'S01', expected_revision: 0, duration_seconds: duration, prompt: prompt.trim(),
          workflow, first_frame: mode === 'image-video' ? firstFrame : undefined,
          generation: { aspect_ratio: aspect, seed: 42, sound_mode: 'performance_reference', speed_profile: 'standard', output_resolution: appliedQuality },
        },
      });
      setStatus('预检…');
      const validation = await request<{ valid: boolean; values?: Record<string, unknown>; revision?: number; blockers?: { message?: string }[] }>(`/api/projects/${id}/pipeline/${encodeURIComponent(stage.id)}/validate`, {
        init: { method: 'POST' }, json: { asset_id: 'S01', values: { prompt: prompt.trim(), seed: 42, duration, 'turbo-sampling': 'enable' } },
      });
      if (!validation.valid) throw new Error(validation.blockers?.map(item => item.message).filter(Boolean).join('；') || '预检未通过');
      setStatus('提交生成任务…');
      const task = await request<Task>(`/api/projects/${id}/tasks`, {
        init: { method: 'POST' }, json: {
          idempotency_key: crypto.randomUUID(), asset_id: 'S01', expected_revision: validation.revision,
          pipeline_stage_id: stage.id, pipeline_values: validation.values || { prompt: prompt.trim(), seed: 42, duration, 'turbo-sampling': 'enable' },
        },
      });
      for (let attempt = 0; attempt < 100; attempt += 1) {
        const current = await request<Task>(`/api/projects/${id}/tasks/${encodeURIComponent(task.id)}`);
        setStatus(statusLabels[current.status] || current.status);
        setProgress(current.progress || null);
        if (['succeeded', 'failed', 'stopped', 'needs_reconcile'].includes(current.status)) {
          if (current.status !== 'succeeded') throw new Error(current.error || `任务状态：${current.status}`);
          break;
        }
        await new Promise(resolve => setTimeout(resolve, 2000));
      }
      const versions = await request<{ items: Version[] }>(`/api/projects/${id}/segments/S01/versions`);
      const output = versions.items?.[0]?.video_path;
      if (!output) throw new Error('任务成功但未找到候选视频');
      setVideo(output); setStatus('已完成');
    } catch (reason) { setError(reason instanceof Error ? reason.message : '生成失败'); setStatus(''); }
    finally { setBusy(false); }
  };

  return <div className="qt-form">
    <p className="qt-note">单步任务不属于任何作品，结果会出现在首页的「单步任务」里，可以逐条或批量删除。</p>
    <label className="sf-field"><span>制作方案 · 必须显式选择</span><select value={presetId} onChange={event => setPresetId(event.target.value)}><option value="">选择方案</option>{presets.map(preset => <option key={preset.id} value={preset.id}>{preset.title}</option>)}</select></label>
    {mode === 'image-video' && <label className="sf-field"><span>开始画面</span><input type="file" accept="image/png,image/jpeg,image/webp" onChange={event => { const file = event.target.files?.[0]; if (file) void upload(file).catch(reason => setError(reason instanceof Error ? reason.message : '上传失败')); event.target.value = ''; }} />{firstFrame && <small className="sf-fine">已上传：{firstFrame}</small>}</label>}
    <div className="sf-field">
      <span className="qt-field-head">提示词<button type="button" className="qt-inline-button" disabled={busy} onClick={() => setLibraryOpen(true)}><BookOpen size={12} /> 从提示词库带入</button><button type="button" className="qt-inline-button" disabled={busy} onClick={() => setPrompt(current => current.includes(NARRATION_HINT) ? current : `${current.trim()}${current.trim() ? '\n' : ''}${NARRATION_HINT}`)}><AudioLines size={12} /> 加入旁白说明</button></span>
      <textarea rows={5} value={prompt} disabled={busy} onChange={event => setPrompt(event.target.value)} placeholder="描述画面、动作和镜头。" />
      <small className="sf-fine">要旁白时，把「声音」写清楚：画外旁白还是人物对白、谁开口、环境声多响——没写清时模型常把旁白当成镜头里的人说话。要百分之百可控，可以先生成画面，再用「配音」和「换音轨」把旁白单独铺上去。</small>
    </div>
    <label className="sf-field"><span>时长（秒）</span><input type="number" min={1} max={15} value={duration} onChange={event => setDuration(Number(event.target.value))} /></label>
    <div className="sf-field">
      <span>画质</span>
      <div className="clarity-levels" role="group" aria-label="视频画质">
        {videoClarity.map(option => <button key={option.value} type="button" aria-pressed={quality === option.value} disabled={busy} onClick={() => setQuality(option.value)}><strong>{option.label}</strong><small>{option.note}</small></button>)}
      </div>
      <small className="sf-fine">默认 1K 原生；2K、4K 生成后用 Lanczos 放大，不增加原生细节；8K 视频暂不提供（转码耗时与体积过大）。</small>
      <small className="sf-fine">画质规则：提示词写明 1K / 2K / 4K / 8K（或高清 / 超高清）时，以提示词为准。</small>
      {promptIntent && <small className="sf-fine">提示词里写了「{promptIntent.phrase}」：本次按 {qualityLabel(appliedQuality)} 输出{qualityCapped ? '（8K 视频暂以 4K 输出）' : ''}，优先于你选择的 {qualityLabel(quality)}。</small>}
    </div>
    {error && <p className="sf-error" role="alert">{error}</p>}
    {status && <p className="qt-status">{busy && <LoaderCircle size={14} className="sf-spin" />}{status}</p>}
    <ProgressBar progress={progress} />
    {video && <div className="qt-result"><video src={mediaUrl(video)} controls preload="metadata" /><a className="sf-secondary" href={mediaUrl(video)} download>下载结果</a></div>}
    <div className="sf-step-actions"><button className="sf-primary" disabled={busy} onClick={() => void submit()}>{busy ? '生成中…' : '生成'}</button></div>
    {libraryOpen && <PromptLibraryDialog target={mode} onUse={text => setPrompt(text)} onClose={() => setLibraryOpen(false)} />}
  </div>;
}

export default function QuickToolPage({ tool, onBack, seed }: { tool: Tool; onBack: () => void; seed?: QuickSeed | null }) {
  const [projectId, setProjectId] = useState('');
  const [error, setError] = useState('');
  const info = meta[tool];

  useEffect(() => {
    let cancelled = false;
    void quickWorkspace().then(id => { if (!cancelled) setProjectId(id); })
      .catch(reason => { if (!cancelled) setError(reason instanceof Error ? reason.message : '准备单步任务失败'); });
    return () => { cancelled = true; };
  }, []);

  return <main className="sf-page qt-page">
    <button className="sf-back" onClick={onBack}><ChevronLeft size={15} /> 返回工具台</button>
    <header className="qt-head"><span className="qt-icon"><info.icon size={22} /></span><div><p className="sf-step-label">单步工具</p><h1>{info.title}</h1><p>{info.note}</p></div></header>
    {error && <p className="sf-error" role="alert">{error}</p>}
    {tool === 'image' && (projectId ? <KeyframePanel projectId={projectId} simple fresh initialMode="text" initialPrompt={seed?.prompt} options={[]} onBrowse={() => {}} onComplete={() => {}} /> : !error && <p className="qt-status"><LoaderCircle size={14} className="sf-spin" />正在准备单步项目…</p>)}
    {tool === 'enhance' && (projectId ? <EnhancementPanel projectId={projectId} onBack={onBack} /> : !error && <p className="qt-status"><LoaderCircle size={14} className="sf-spin" />正在准备单步项目…</p>)}
    {tool === 'music' && (projectId ? <MusicPanel projectId={projectId} initialCard={seed?.music || null} onComplete={() => {}} /> : !error && <p className="qt-status"><LoaderCircle size={14} className="sf-spin" />正在准备单步项目…</p>)}
    {tool === 'timbre' && (projectId ? <TimbrePanel projectId={projectId} initialDescription={seed?.timbre} onComplete={() => {}} /> : !error && <p className="qt-status"><LoaderCircle size={14} className="sf-spin" />正在准备单步项目…</p>)}
    {(tool === 'text-video' || tool === 'image-video') && <QuickVideo mode={tool} initialFrame={seed?.frame} initialPrompt={seed?.prompt} />}
  </main>;
}
