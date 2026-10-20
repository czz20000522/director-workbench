import { lazy, useEffect, useState } from 'react';
import { ChevronLeft, Image, LoaderCircle, Play, Upload, Wand2 } from 'lucide-react';
import { mediaUrl, request } from './api/client';

const KeyframePanel = lazy(() => import('./KeyframePanel'));
const EnhancementPanel = lazy(() => import('./EnhancementPanel'));

type Tool = 'image' | 'text-video' | 'image-video' | 'enhance';
type Preset = { id: string; title: string; description?: string; available?: boolean };
type Task = { id: string; status: string; error?: string };
type Version = { task_id: string; video_path: string; is_current?: boolean };

const meta: Record<Tool, { title: string; note: string; icon: typeof Image }> = {
  image: { title: '文生图', note: '输入提示词，生成一张图片。工作台只执行你明确提交的任务。', icon: Image },
  'text-video': { title: '文生视频', note: '选择制作方案，输入提示词，生成一个镜头。', icon: Play },
  'image-video': { title: '图生视频', note: '上传一张开始画面，选择图生方案，生成一个镜头。', icon: Upload },
  enhance: { title: '清晰化', note: '上传图片或视频，提高分辨率。', icon: Wand2 },
};

async function createSingleProject(title: string): Promise<string> {
  const created = await request<{ project: { id: string } }>('/api/projects/create', {
    init: { method: 'POST' }, json: { series: '单步工具', title, creation_mode: 'advanced' },
  });
  return created.project.id;
}

function QuickVideo({ mode }: { mode: 'text-video' | 'image-video' }) {
  const [presets, setPresets] = useState<Preset[]>([]);
  const [presetId, setPresetId] = useState('');
  const [prompt, setPrompt] = useState('');
  const [duration, setDuration] = useState(4);
  const [firstFrame, setFirstFrame] = useState('');
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState('');
  const [video, setVideo] = useState('');
  const [error, setError] = useState('');
  const [created, setCreated] = useState(false);
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
    const id = await createSingleProject(`单步 · ${mode === 'text-video' ? '文生视频' : '图生视频'} · ${new Date().toLocaleString('zh-CN', { hour12: false })}`);
    setProjectId(id); setCreated(true); return id;
  };

  const upload = async (file: File) => {
    const id = await ensureProject();
    const form = new FormData(); form.append('files', file, `assets/${file.name}`);
    const data = await request<{ uploaded?: string[] }>(`/api/projects/${id}/upload`, { init: { method: 'POST', body: form } });
    setFirstFrame(data.uploaded?.[0] || '');
  };

  const submit = async () => {
    if (mode === 'image-video' && !firstFrame) { setError('请先上传开始画面'); return; }
    if (!presetId) { setError('请显式选择一个制作方案'); return; }
    if (!prompt.trim()) { setError('请填写提示词'); return; }
    setBusy(true); setError(''); setStatus('创建单步项目…');
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
          generation: { aspect_ratio: '16:9', seed: 42, sound_mode: 'performance_reference', speed_profile: 'standard', output_resolution: 'native' },
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
        setStatus(current.status);
        if (['succeeded', 'failed', 'stopped', 'needs_reconcile'].includes(current.status)) {
          if (current.status !== 'succeeded') throw new Error(current.error || `任务状态：${current.status}`);
          break;
        }
        await new Promise(resolve => setTimeout(resolve, 3000));
      }
      const versions = await request<{ items: Version[] }>(`/api/projects/${id}/segments/S01/versions`);
      const output = versions.items?.[0]?.video_path;
      if (!output) throw new Error('任务成功但未找到候选视频');
      setVideo(output); setStatus('已完成');
    } catch (reason) { setError(reason instanceof Error ? reason.message : '生成失败'); setStatus(''); }
    finally { setBusy(false); }
  };

  return <div className="qt-form">
    {!created && <p className="qt-note">第一次提交时会创建一个「单步工具」项目来承载结果，不进入影片向导。</p>}
    <label className="sf-field"><span>制作方案 · 必须显式选择</span><select value={presetId} onChange={event => setPresetId(event.target.value)}><option value="">选择方案</option>{presets.map(preset => <option key={preset.id} value={preset.id}>{preset.title}</option>)}</select></label>
    {mode === 'image-video' && <label className="sf-field"><span>开始画面</span><input type="file" accept="image/png,image/jpeg,image/webp" onChange={event => { const file = event.target.files?.[0]; if (file) void upload(file).catch(reason => setError(reason instanceof Error ? reason.message : '上传失败')); event.target.value = ''; }} />{firstFrame && <small className="sf-fine">已上传：{firstFrame}</small>}</label>}
    <label className="sf-field"><span>提示词</span><textarea rows={5} value={prompt} onChange={event => setPrompt(event.target.value)} placeholder="描述画面、动作和镜头。" /></label>
    <label className="sf-field"><span>时长（秒）</span><input type="number" min={1} max={15} value={duration} onChange={event => setDuration(Number(event.target.value))} /></label>
    {error && <p className="sf-error" role="alert">{error}</p>}
    {status && <p className="qt-status">{busy && <LoaderCircle size={14} className="sf-spin" />}{status}</p>}
    {video && <div className="qt-result"><video src={mediaUrl(video)} controls preload="metadata" /><a className="sf-secondary" href={mediaUrl(video)} download>下载结果</a></div>}
    <div className="sf-step-actions"><button className="sf-primary" disabled={busy} onClick={() => void submit()}>{busy ? '生成中…' : '生成'}</button></div>
  </div>;
}

export default function QuickToolPage({ tool, onBack }: { tool: Tool; onBack: () => void }) {
  const [projectId, setProjectId] = useState('');
  const [error, setError] = useState('');
  const info = meta[tool];

  useEffect(() => {
    if (tool !== 'image' && tool !== 'enhance') return;
    let cancelled = false;
    void createSingleProject(`单步 · ${info.title} · ${new Date().toLocaleString('zh-CN', { hour12: false })}`)
      .then(id => { if (!cancelled) setProjectId(id); })
      .catch(reason => { if (!cancelled) setError(reason instanceof Error ? reason.message : '创建单步项目失败'); });
    return () => { cancelled = true; };
  }, [tool]);

  return <main className="sf-page qt-page">
    <button className="sf-back" onClick={onBack}><ChevronLeft size={15} /> 返回工具台</button>
    <header className="qt-head"><span className="qt-icon"><info.icon size={22} /></span><div><p className="sf-step-label">单步工具</p><h1>{info.title}</h1><p>{info.note}</p></div></header>
    {error && <p className="sf-error" role="alert">{error}</p>}
    {tool === 'image' && (projectId ? <KeyframePanel projectId={projectId} simple initialMode="text" options={[]} onBrowse={() => {}} onComplete={() => {}} /> : !error && <p className="qt-status"><LoaderCircle size={14} className="sf-spin" />正在准备单步项目…</p>)}
    {tool === 'enhance' && (projectId ? <EnhancementPanel projectId={projectId} onBack={onBack} /> : !error && <p className="qt-status"><LoaderCircle size={14} className="sf-spin" />正在准备单步项目…</p>)}
    {(tool === 'text-video' || tool === 'image-video') && <QuickVideo mode={tool} />}
  </main>;
}
