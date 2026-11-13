import { lazy, Suspense, useEffect, useMemo, useRef, useState } from 'react';
import { AudioLines, BookOpen, ChevronLeft, Image, ImagePlus, LoaderCircle, Music, Play, Scissors, Upload, Wand2 } from 'lucide-react';
import { mediaUrl, request } from './api/client';
import type { AspectRatio, OutputResolution } from './ResolutionPicker';
import { capVideoQuality, detectQualityIntent, qualityLabel } from './qualityIntent';
import { PromptLibraryDialog, PromptLibraryPanel, MaterialLibraryPanel } from './ReferenceLibrary';
import ProgressBar, { type TaskProgress } from './TaskProgress';
import CompanionStudio from './CompanionStudio';
import FirstFrameStudio from './FirstFrameStudio';
import ReferenceImagePanel from './ReferenceImagePanel';
import PromptWriter from './PromptWriter';
import TranslateButton from './TranslateButton';
import StyleBlockPicker, { readStyleBlock } from './StyleBlockPicker';
import VideoPresetPicker, { type VideoPreset, type HardwareSummary } from './VideoPresetPicker';

const KeyframePanel = lazy(() => import('./KeyframePanel'));
const EnhancementPanel = lazy(() => import('./EnhancementPanel'));
const MusicPanel = lazy(() => import('./MusicPanel'));
const TimbrePanel = lazy(() => import('./TimbrePanel'));
const VideoEditPanel = lazy(() => import('./VideoEditPanel'));

export type QuickTool = 'image' | 'text-video' | 'image-video' | 'reference-image' | 'enhance' | 'music' | 'timbre' | 'video-edit';
/** 首页「编辑重生成」把上次那条任务的可编辑输入原样带回来；fromTask 说明来源。 */
export type QuickSeed = {
  prompt?: string; frame?: string; timbre?: string; music?: Record<string, unknown>;
  /** 音色库「带入」：整张卡片的参数和名字，小样也一起带过来。 */
  timbreCard?: { name: string; params: Record<string, unknown> };
  seed?: number; aspect?: AspectRatio; quality?: string; duration?: number; source?: string; text?: string;
  steps?: number;
  /** 制作方案的 id：视频工具必须显式选方案，编辑重生成时把上一条那套带回来。 */
  preset?: string;
  /** 视频任务关联过的人物声音（口型锁定）与配乐，编辑重生成时一起带回。 */
  voice?: string;
  musicTrack?: string;
  /** 参考图生成的底图：编辑重生成时放回参考图槽。 */
  image?: string;
  /** 参考图生成的「降噪」开关，跟任务一起带回来。 */
  refine?: boolean;
  /** 「编辑重生成」带回来的参考槽：首帧工作台据此还原人脸/造型/服装/姿势。 */
  references?: { role: string; material_id?: string; label?: string; path?: string }[];
  profile?: string;
  fromTask?: string;
};
// 点一下才写进提示词；措辞点明"画外、人物不开口"，这是 H3 把旁白当对白的常见原因。
const NARRATION_HINT = '声音：全程画外旁白，镜头里的人物不开口、没有对白口型；人声清楚靠前，环境声压低。';
type Tool = QuickTool;
const videoClarity: { value: OutputResolution; label: string; note: string }[] = [
  { value: 'native', label: '不放大', note: '原生尺寸' },
  { value: '2k', label: '2K', note: '上采样' },
  { value: '4k', label: '4K', note: '上采样' },
];
type Task = { id: string; status: string; error?: string; progress?: TaskProgress };
type Version = { task_id: string; video_path: string; is_current?: boolean };

const meta: Record<Tool, { title: string; note: string; icon: typeof Image }> = {
  image: { title: '文生图', note: '输入提示词，生成一张图片。工作台只执行你明确提交的任务。', icon: Image },
  'text-video': { title: '文生视频', note: '选择制作方案，输入提示词，生成一个镜头。', icon: Play },
  'image-video': { title: '图生视频', note: '上传一张开始画面，或用素材库拼一张首帧，再选择图生方案生成镜头。', icon: Upload },
  'reference-image': { title: '参考图生成', note: '图生图：上传一张参考图，本地或在线模型识别成提示词，改顺了再批量生成相似但不重样的图。', icon: ImagePlus },
  enhance: { title: '清晰化', note: '上传图片或视频，提高分辨率。', icon: Wand2 },
  music: { title: '配乐', note: '写风格标签与时长，生成一段新音乐候选。', icon: Music },
  timbre: { title: '音色设计', note: 'VoxCPM2 凭文字造音色，或 IndexTTS 克隆参考音频，出可复用样本。', icon: AudioLines },
  'video-edit': { title: '剪辑', note: '把已有片段剪开、排序、拼接，调音量并铺一条 BGM，导出成片。', icon: Scissors },
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

/** 把对白时间轴锁进视频的方案：只有它能让「人物按这段声音开口」。 */
const LOCKED_AUDIO_PRESET = 'h3-first-locked-audio';

function QuickVideo({ mode, workspaceId, adoptInto, initialFrame, initialPrompt, initialDuration, initialQuality, initialPreset,
  initialVoice, initialMusic, promptTicket, frameTicket, restore }: {
  mode: 'text-video' | 'image-video'; workspaceId?: string; initialFrame?: string; initialPrompt?: string;
  initialDuration?: number; initialQuality?: OutputResolution;
  /** 「编辑重生成」带回来的关联声音：改提示词重跑时不用重新选。 */
  initialVoice?: string;
  initialMusic?: string;
  /** 选了作品归属：任务仍落草稿箱（不写该作品分镜），成功后由后端自动复制归入。 */
  adoptInto?: string;
  /** 「编辑重生成」带回来的制作方案：不还原它，表单会按另一条路线重跑。 */
  initialPreset?: string;
  /** 左侧提示词栏点「带入」时递进来的一段文字。 */
  promptTicket?: { text: string; at: number } | null;
  /** 右侧素材栏点「用作开始画面」时递进来的图。 */
  frameTicket?: { path: string; title: string; at: number } | null;
  /** 「编辑重生成」带回来的那一版：首帧工作台据此还原提示词和参考槽。 */
  restore?: {
    prompt?: string;
    references?: { role: string; material_id?: string; label?: string; path?: string }[];
    seed?: number;
    aspect?: '16:9' | '9:16' | '1:1';
  };
}) {
  const [presets, setPresets] = useState<VideoPreset[]>([]);
  const [hardware, setHardware] = useState<HardwareSummary | null>(null);
  const [presetId, setPresetId] = useState(initialPreset || '');
  // 渲染档位：以前这里写死 'enable'，连成人向方案写的「关闭 Turbo · 20 步」都被覆盖，
  // 实际跑成「Turbo LoRA + 真人 LoRA」双串（实测 534 秒，比 20 步还慢）。
  // 现在按方案自己的默认值初始化，用户改过就记住。
  const [turbo, setTurbo] = useState(true);
  const [prompt, setPrompt] = useState(initialPrompt || '');
  // 风格锁：只拼进送模型的 prompt，不改输入框里的文字。
  const [styleBlock, setStyleBlock] = useState(readStyleBlock);
  const [libraryOpen, setLibraryOpen] = useState(false);
  const [duration, setDuration] = useState(Number.isFinite(initialDuration) && (initialDuration as number) > 0 ? (initialDuration as number) : 4);
  const [quality, setQuality] = useState<OutputResolution>(initialQuality ?? 'native');
  // 原生采样档：480p≈0.4MP、768p≈0.6MP。它决定模型真正画多大，和上面的上采样输出是两件事。
  const [nativeMp, setNativeMp] = useState<0.4 | 0.6>(0.6);
  const presetMegapixels = presets.find(item => item.id === presetId)?.megapixels;
  // 换方案时回到该方案自己的原生档；用户在同一方案内手动改过就不再被覆盖。
  useEffect(() => {
    if (typeof presetMegapixels === 'number') setNativeMp(presetMegapixels <= 0.45 ? 0.4 : 0.6);
  }, [presetId]);
  const [firstFrame, setFirstFrame] = useState(initialFrame || '');
  // 首帧来源：素材组合（默认）或直接上传。已经有图进来时先落在「上传」这一栏，
  // 因为那张图本来就不是这次组合出来的。
  const [frameSource, setFrameSource] = useState<'studio' | 'upload'>(initialFrame ? 'upload' : 'studio');
  // 图生视频的「素材组合」分支：首帧台里那个「画面描述 / 改动要求」就是视频提示词，
  // 外层不再重复渲染一个输入框，避免同一段文字有两个框。
  const [studioPrompt, setStudioPrompt] = useState('');
  const hideOwnPrompt = mode === 'image-video' && frameSource === 'studio';
  const effectivePrompt = hideOwnPrompt ? studioPrompt : prompt;
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState('');
  const [progress, setProgress] = useState<TaskProgress | null>(null);
  const [video, setVideo] = useState('');
  // 记住产出这条视频的任务号，后续配乐/音色靠它建立归属。
  const [videoTaskId, setVideoTaskId] = useState('');
  // 关联已有声音：默认都不选 —— 不选就是按提示词让模型自己出声。
  // 人物声音＝口型锁定（需要首帧）；配乐＝生成后混入音轨，不重新出画面。
  const [audioAssets, setAudioAssets] = useState<{ id: string; title: string; path: string }[]>([]);
  const [voicePath, setVoicePath] = useState(initialVoice || '');
  const [musicPath, setMusicPath] = useState(initialMusic || '');
  const [error, setError] = useState('');
  const [projectId, setProjectId] = useState(workspaceId || '');

  useEffect(() => {
    if (workspaceId && !projectId) setProjectId(workspaceId);
  }, [workspaceId, projectId]);

  // 左栏「提示词库」带进来的一段话：直接覆盖提示词，不再弹二次确认。
  useEffect(() => {
    if (promptTicket?.text) setPrompt(promptTicket.text);
  }, [promptTicket?.at, promptTicket?.text]);

  // 右栏素材点「用作开始画面」：素材已经复制进单步工作区，这里只接路径。
  useEffect(() => {
    if (!frameTicket?.path) return;
    setFirstFrame(frameTicket.path);
    setFrameSource('upload');
    setStatus(`开始画面已换成素材库里的「${frameTicket.title || '这张图'}」。`);
  }, [frameTicket?.at, frameTicket?.path, frameTicket?.title]);

  useEffect(() => {
    let cancelled = false;
    // 不可用方案也取回来：选择器里要写清「缺哪个模型」，而不是让它凭空消失。
    void request<{ presets: VideoPreset[]; hardware?: HardwareSummary }>('/api/production-presets').then(data => {
      if (!cancelled) { setPresets(data.presets || []); setHardware(data.hardware || null); }
    }).catch(reason => { if (!cancelled) setError(reason instanceof Error ? reason.message : '无法读取制作方案'); });
    return () => { cancelled = true; };
  }, []);

  // 文生/图生切换时旧方案多半属于另一边，留着会在预检阶段 422。
  // 首次挂载要保留「编辑重生成」带回来的方案，所以跳过第一轮。
  const firstModeRun = useRef(true);
  useEffect(() => {
    if (firstModeRun.current) { firstModeRun.current = false; return; }
    setPresetId('');
    setVoicePath('');
  }, [mode]);

  // 可关联的声音：本作/草稿箱里已登记的音频（配音、音色样、配乐都算）。
  useEffect(() => {
    if (!projectId) return;
    let cancelled = false;
    void request<{ materials: { id: string; title: string; kind: string; path: string }[] }>(
      `/api/projects/${encodeURIComponent(projectId)}/reusable-materials`)
      .then(data => { if (!cancelled) setAudioAssets((data.materials || []).filter(item => item.kind === 'audio')); })
      .catch(() => { if (!cancelled) setAudioAssets([]); });
    return () => { cancelled = true; };
  }, [projectId, videoTaskId]);

  // 换方案等于换路线：锁定音频只在那一个方案里成立，别让上一次的选择被默默忽略。
  const choosePreset = (id: string) => {
    setPresetId(id);
    if (id !== LOCKED_AUDIO_PRESET && voicePath) {
      setVoicePath('');
      setStatus('已换制作方案：人物声音关联已取消。');
    }
  };

  const chooseVoice = (path: string) => {
    setVoicePath(path);
    if (!path) return;
    setPresetId(LOCKED_AUDIO_PRESET);
    // 锁定方案按音频时间轴开口：时长尽量跟这段声音对齐，省得先预检失败一次。
    const probe = new Audio(mediaUrl(path));
    probe.onloadedmetadata = () => {
      if (Number.isFinite(probe.duration) && probe.duration > 0) {
        setDuration(Math.min(15, Math.max(4, Math.round(probe.duration))));
      }
    };
  };

  // 方案表里已经没有这个 id（方案被移除）时清掉，免得带着一个选不中的值去提交。
  useEffect(() => {
    if (!presetId || !presets.length) return;
    if (!presets.some(item => item.id === presetId)) setPresetId('');
  }, [presets, presetId]);

  // 选了方案才决定档位：优先用这个方案上次记住的选择，否则用它的默认值。
  useEffect(() => {
    const preset = presets.find(item => item.id === presetId);
    if (!preset) return;
    if (presetId.startsWith('h3-pika-')) {
      setTurbo(false);
      setDuration(5);
      return;
    }
    let stored: string | null = null;
    try { stored = localStorage.getItem(`director-workbench:video-turbo:${presetId}`); } catch { /* 隐私模式 */ }
    setTurbo(stored ? stored === 'enable' : (preset.turbo ?? true));
  }, [presetId, presets]);

  const chooseTurbo = (value: boolean) => {
    setTurbo(value);
    try { localStorage.setItem(`director-workbench:video-turbo:${presetId}`, value ? 'enable' : 'disable'); } catch { /* 隐私模式 */ }
  };
  // 成人向方案常驻一条真人 LoRA，这时开 Turbo 是「双 LoRA 串联」，耗时和显存都与常规不同。
  const isAdultPreset = Boolean(presets.find(item => item.id === presetId)?.title.includes('成人向'));
  // Pika 是固定 6 步 LCM，没有 Turbo / 精细两条分支，也没有 turbo-sampling 输入。
  const isPikaPreset = presetId.startsWith('h3-pika-');

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
  const presetAspect = presets.find(item => item.id === presetId)?.aspect_ratio;
  // Pika 是后接的路线，方案表里一直排最后，看着像没接上。放到同组最前面。
  const orderedPresets = useMemo(() => {
    const rank = (item: VideoPreset) => (item.id.startsWith('h3-pika-') ? 0 : 1);
    return [...presets].sort((a, b) => rank(a) - rank(b));
  }, [presets]);

  const submit = async () => {
    if (mode === 'image-video' && !firstFrame) { setError('请先上传开始画面'); return; }
    if (!presetId) { setError('请显式选择一个制作方案'); return; }
    if (!effectivePrompt.trim()) { setError('请填写提示词'); return; }
    // 原来写死 42，导致同一组参数每次出片完全一样。这里每次提交换一个，
    // 但整条链路（分镜 / 预检 / 任务）必须共用同一个值，否则会被判为参数冲突。
    const videoSeed = Math.floor(Math.random() * 4294967295);
    const turboValue = turbo ? 'enable' : 'disable';
    const commonValues = { prompt: effectivePrompt.trim(), seed: videoSeed, duration, ...(isPikaPreset ? {} : { 'turbo-sampling': turboValue }) };
    setBusy(true); setError(''); setStatus('准备单步任务…'); setProgress(null);
    try {
      const id = await ensureProject();
      setStatus('安装制作方案…');
      const installed = await request<{ project: { pipeline?: { id: string; execution?: { references?: string[] } }[]; production_preset?: { workflow?: string } } }>(`/api/projects/${id}/production-presets/${encodeURIComponent(presetId)}`, { init: { method: 'POST' } });
      // 单步容器会把历次装过的方案留在 pipeline 里，所以 pipeline[0] 未必是这次选的。
      // 以 production_preset.workflow 为准，再反查它属于哪一条执行阶段。
      const presetWorkflow = installed.project.production_preset?.workflow || '';
      const stage = installed.project.pipeline?.find(item => (item.execution?.references || []).includes(presetWorkflow))
        || installed.project.pipeline?.find(item => item.execution?.references?.[0] === presetWorkflow)
        || installed.project.pipeline?.[0];
      if (!stage) throw new Error('方案未返回可执行阶段');
      const workflow = presetWorkflow || stage.execution?.references?.[0] || '';
      setStatus('登记镜头…');
      // revision 每次落盘都会涨；写死 0 会让第二次生成直接报「分镜计划已被修改」。
      const plan = await request<{ revision?: number }>(`/api/projects/${id}/plan`)
        .catch(() => ({ revision: 0 }));
      const created = await request<{ segment?: { id?: string } }>(`/api/projects/${id}/plan/segments`, {
        init: { method: 'POST' }, json: {
          segment_id: 'S01', expected_revision: plan.revision ?? 0, duration_seconds: duration, prompt: effectivePrompt.trim(),
          workflow, first_frame: mode === 'image-video' ? firstFrame : undefined,
          // 口型锁定：分镜上登记这段表演音频，预检会把它解析成 guide 输入。
          audio_guide: voicePath && mode === 'image-video' ? voicePath : undefined,
          // 画幅、声音模式、速度档、创作分区都由所选方案自己定（后端 generation_defaults）。
          // 前端硬传 standard / performance_reference 会在快速或成人向方案上撞 422。
          generation: { seed: videoSeed, output_resolution: appliedQuality },
        },
      });
      // S01 已存在时后端会自动改成 S01-2/-3…；后续必须跟着用新 id，
      // 否则预检读到旧分镜的 keyframes，会拿上一次的首帧和提示词去生成。
      const segmentId = created.segment?.id || 'S01';
      setStatus('预检…');
      const validation = await request<{ valid: boolean; values?: Record<string, unknown>; revision?: number; blockers?: { message?: string }[] }>(`/api/projects/${id}/pipeline/${encodeURIComponent(stage.id)}/validate`, {
        init: { method: 'POST' }, json: { asset_id: segmentId, values: commonValues },
      });
      if (!validation.valid) throw new Error(validation.blockers?.map(item => item.message).filter(Boolean).join('；') || '预检未通过');
      setStatus('提交生成任务…');
      const task = await request<Task>(`/api/projects/${id}/tasks`, {
        init: { method: 'POST' }, json: {
          idempotency_key: crypto.randomUUID(), asset_id: segmentId, expected_revision: validation.revision,
          pipeline_stage_id: stage.id, pipeline_values: validation.values || commonValues,
          native_megapixels: nativeMp,
          ...(styleBlock ? { style_block: styleBlock } : {}),
          // 只对草稿箱任务有效：成功后自动把产物复制归入该作品（不写它的分镜表）。
          ...(adoptInto ? { adopt_into: adoptInto } : {}),
          // 关联配乐：后端在生成成功后混进成片，画面不重跑。
          ...(musicPath ? { music_overlay: musicPath } : {}),
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
      const versions = await request<{ items: Version[] }>(`/api/projects/${id}/segments/${encodeURIComponent(segmentId)}/versions`);
      const output = versions.items?.[0]?.video_path;
      if (!output) throw new Error('任务成功但未找到候选视频');
      setVideo(output); setVideoTaskId(task.id);
      setStatus(`${musicPath ? '已完成（配乐已混入）' : '已完成'}${adoptInto ? '；产物会自动归入所选作品' : ''}`);
    } catch (reason) { setError(reason instanceof Error ? reason.message : '生成失败'); setStatus(''); }
    finally { setBusy(false); }
  };

  return <div className="qt-studio">
    <div className="qt-studio-main">
    <p className="qt-note">单步任务不属于任何作品，结果会出现在首页的「单步任务」里，可以逐条或批量删除。</p>
    <VideoPresetPicker presets={orderedPresets} mode={mode} value={presetId} onChange={choosePreset} disabled={busy} hardware={hardware} />
    {mode === 'image-video' && <div className="ffs-source">
      <div className="ffs-source-head">
        <span className="ffs-source-title">开始画面</span>
        <div className="ffs-source-tabs" role="group" aria-label="开始画面来源">
          <button type="button" aria-pressed={frameSource === 'studio'} disabled={busy} onClick={() => setFrameSource('studio')}>素材组合</button>
          <button type="button" aria-pressed={frameSource === 'upload'} disabled={busy} onClick={() => setFrameSource('upload')}>已有图片</button>
        </div>
      </div>
      {firstFrame
        ? <div className="ffs-ready"><img src={mediaUrl(firstFrame)} alt="" /><div><strong>开始画面已就绪</strong><small>{firstFrame.split(/[\\/]/).pop()}</small></div></div>
        : <p className="sf-fine">还没有开始画面。{frameSource === 'studio' ? '下面选素材拼一张，或者切到「已有图片」。' : '上传一张本地图片，或从右侧素材库点「用作图生视频开始画面」。'}</p>}
      {frameSource === 'upload' && <label className="sf-field ffs-upload"><span>选择本地图片</span>
        <input type="file" accept="image/png,image/jpeg,image/webp,.heic,.heif" onChange={event => { const file = event.target.files?.[0]; if (file) void upload(file).catch(reason => setError(reason instanceof Error ? reason.message : '上传失败')); event.target.value = ''; }} />
      </label>}
      {frameSource === 'upload' && <p className="sf-fine">右侧「素材参考库」里已经有首帧图参考：点开任意一张，选「用作图生视频开始画面」就会带到这里。</p>}
      {frameSource === 'studio' && (projectId
        ? <FirstFrameStudio projectId={projectId} disabled={busy} defaultAspect={presetAspect} currentFrame={firstFrame}
            onChange={path => setFirstFrame(path)} onPromptChange={setStudioPrompt}
            restoreFrom={restore} />
        : <p className="qt-status"><LoaderCircle size={14} className="sf-spin" />正在准备单步项目…</p>)}
    </div>}
    <div className="qt-audio-link" aria-label="关联已有声音">
      <div className="qt-audio-head">
        <strong>关联已有声音</strong>
        <small>两项都不选 = 按提示词生成，让模型自己出声。</small>
      </div>
      <div className="qt-audio-fields">
        <label className="sf-field">
          <span>人物声音{mode === 'text-video' ? '（需先有开始画面）' : ''}</span>
          <select value={voicePath} disabled={busy || mode === 'text-video'} onChange={event => chooseVoice(event.target.value)}
            title={mode === 'text-video' ? '文生视频没有首帧可以锁口型' : '选一条配音或音色样本，人物按它开口'}>
            <option value="">不关联</option>
            {voicePath && !audioAssets.some(item => item.path === voicePath)
              && <option value={voicePath}>（原文件已不在）{voicePath.split(/[\\/]/).pop()}</option>}
            {audioAssets.map(item => <option key={item.id} value={item.path}>{item.title}</option>)}
          </select>
        </label>
        <label className="sf-field">
          <span>配乐</span>
          <select value={musicPath} disabled={busy} onChange={event => setMusicPath(event.target.value)}
            title="生成完成后混进成片，不重新出画面">
            <option value="">不关联</option>
            {musicPath && !audioAssets.some(item => item.path === musicPath)
              && <option value={musicPath}>（原文件已不在）{musicPath.split(/[\\/]/).pop()}</option>}
            {audioAssets.map(item => <option key={item.id} value={item.path}>{item.title}</option>)}
          </select>
        </label>
      </div>
      <small className="sf-fine">
        {voicePath
          ? '已切到「锁定音频」方案：人物按这段声音开口，提示词只写动作与镜头；时长已按音频长度对齐。'
          : mode === 'text-video'
            ? '文生视频没有首帧可锁口型；要人物对上声音，请用「图生视频」并选好开始画面。'
            : audioAssets.length
              ? '选人物声音会改用锁定音频方案；配乐在生成后混入（保留原生音轨，压到 0.9 倍）。'
              : '还没有可关联的音频：先用「配音」「音色设计」或「配乐」做一条，再回来选。'}
      </small>
    </div>
    {!hideOwnPrompt && <div className="sf-field">
      <span className="qt-field-head">提示词<TranslateButton text={prompt} disabled={busy} onTranslated={setPrompt} /><StyleBlockPicker value={styleBlock} onChange={setStyleBlock} disabled={busy} hint={false} /><button type="button" className="qt-inline-button" disabled={busy} onClick={() => setLibraryOpen(true)}><BookOpen size={12} /> 从提示词库带入</button><button type="button" className="qt-inline-button" disabled={busy} onClick={() => setPrompt(current => current.includes(NARRATION_HINT) ? current : `${current.trim()}${current.trim() ? '\n' : ''}${NARRATION_HINT}`)}><AudioLines size={12} /> 加入旁白说明</button></span>
      <textarea rows={6} value={prompt} disabled={busy} onChange={event => setPrompt(event.target.value)} placeholder="描述画面、动作和镜头。" />
      <PromptWriter disabled={busy} prompt={prompt} label="AI 润色"
        context={{
          project_id: projectId,
          preset_id: presetId || undefined,
          profile: isAdultPreset ? 'adult-realism' : 'general',
          aspect: presetAspect,
        }}
        onUse={text => setPrompt(text)} />
      <small className="sf-fine">要旁白时，把「声音」写清楚：画外旁白还是人物对白、谁开口、环境声多响——没写清时模型常把旁白当成镜头里的人说话。要百分之百可控，可以先生成画面，再用「配音」和「换音轨」把旁白单独铺上去。</small>
    </div>}
    {presetId ? <>
    <div className="qt-pair">
      <label className="sf-field"><span>时长（秒）</span><input type="number" min={isPikaPreset ? 5 : 1} max={isPikaPreset ? 5 : 15} step={isPikaPreset ? 0.1 : 1} value={duration} disabled={isPikaPreset} title={isPikaPreset ? 'Pika 官方基线：生成 124 帧、交付 120 帧 / 5.0 秒' : undefined} onChange={event => setDuration(Number(event.target.value))} /></label>
      <div className="sf-field">
        <span>画质</span>
        {isPikaPreset
          ? <p className="sf-fine">Pika 原生 768p（1056×608），由配方固定，不能切 480p。</p>
          : <div className="clarity-levels is-compact" role="group" aria-label="原生采样">
              <button type="button" aria-pressed={nativeMp === 0.4} disabled={busy} onClick={() => setNativeMp(0.4)}><strong>480p</strong><small>832×480</small></button>
              <button type="button" aria-pressed={nativeMp === 0.6} disabled={busy} onClick={() => setNativeMp(0.6)}><strong>768p</strong><small>1024×576</small></button>
            </div>}
        <div className="clarity-levels is-compact" role="group" aria-label="上采样输出">
          {videoClarity.map(option => <button key={option.value} type="button" aria-pressed={quality === option.value} disabled={busy} onClick={() => setQuality(option.value)}><strong>{option.label}</strong><small>{option.note}</small></button>)}
        </div>
      </div>
    </div>
    <small className="sf-fine">{isPikaPreset
      ? 'Pika 出的是一段 1056×608 / 5.0 秒的带声音画面；下一行的 2K / 4K 只在导出时放大，不增加原生细节。'
      : '上一行是原生采样：480p / 768p 是模型真正生成的尺寸，也是最省显存、最快的档。下一行是上采样路线：2K / 4K 只把生成结果放大，不增加原生细节；8K 视频暂不提供。提示词写明清晰度时以提示词为准。'}</small>
    <div className="sf-field">
      <span>渲染档位</span>
      {isPikaPreset ? <small className="sf-fine">Pika 固定 6 步 LCM：没有 Turbo / 精细分支，也不叠加速 LoRA；配方由方案自带。</small> : <>
      <div className="clarity-levels" role="group" aria-label="渲染档位">
        <button type="button" aria-pressed={turbo} disabled={busy} onClick={() => chooseTurbo(true)}>
          <strong>Turbo · 少步快出</strong>
          <small>{isAdultPreset ? '8 步 · 双 LoRA 实测 534 秒（反而最慢）' : '8 步 · 实测约 153 秒'}</small>
        </button>
        <button type="button" aria-pressed={!turbo} disabled={busy} onClick={() => chooseTurbo(false)}>
          <strong>精细 · 多步</strong>
          <small>20 步 · 实测约 361 秒 · 脸和手更稳</small>
        </button>
      </div>
      <small className="sf-fine">
        {isAdultPreset
          ? '成人向方案：真人 LoRA 常驻，再开 Turbo 会串两条 LoRA，实测 534 秒、比 20 步还慢、显存也最紧——想快不如直接用精细档。'
          : 'Turbo 快 2 倍多，但 8 步蒸馏，手指、发丝这类细节偶尔糙；精细多花一倍多时间换稳。显存三条路都顶在 13~14 GiB，差别在时间不在爆显存。'}
        默认跟着方案走（卡片上标了「8 步 · Turbo」或「20 步」），你改过之后按方案记住。
      </small>
      </>}
    </div>
    </> : <p className="sf-fine">先选一套制作方案：时长、画质和步数都由它决定——Pika 固定 6 步 / 5 秒，H3 是 8 步 Turbo 或 20 步精细。</p>}
    {promptIntent && <small className="sf-fine">提示词里写了「{promptIntent.phrase}」：本次按 {qualityLabel(appliedQuality)} 输出{qualityCapped ? '（8K 视频暂以 4K 输出）' : ''}，优先于你选择的 {qualityLabel(quality)}。</small>}
    {error && <p className="sf-error" role="alert">{error}</p>}
    {status && <p className="qt-status">{busy && <LoaderCircle size={14} className="sf-spin" />}{status}</p>}
    <ProgressBar progress={progress} />
    <div className="sf-step-actions"><button className="sf-primary" disabled={busy} onClick={() => void submit()}>{busy ? '生成中…' : '生成'}</button></div>
    {libraryOpen && <PromptLibraryDialog target={mode} onUse={text => setPrompt(text)} onClose={() => setLibraryOpen(false)} />}
    </div>

    <aside className="qt-studio-side" aria-label="预览">
      <section className="qt-preview">
        <header className="qt-preview-head">
          <span>产物预览</span>
          {video ? <a className="sf-inline-link" href={mediaUrl(video)} download>下载</a> : <small>{busy ? '生成中…' : '还没有结果'}</small>}
        </header>
        {video
          ? <video src={mediaUrl(video)} controls preload="metadata" />
          : mode === 'image-video' && firstFrame && frameSource === 'upload'
            // 素材组合那条路，表单里已经有大图了；只有「已有图片」这条才需要在这里回显。
            ? <figure className="qt-preview-frame"><img src={mediaUrl(firstFrame)} alt="开始画面" /><figcaption>开始画面（出片后会在这里换成视频）</figcaption></figure>
            : <p className="qt-preview-empty">生成一条之后，视频会固定停在这一栏，方便你一边看一边改下面的参数。</p>}
      </section>
      {video && videoTaskId && projectId && <section className="qt-companions" aria-label="这条视频的配套素材">
        <h3 className="qt-companions-title">配套素材</h3>
        <p className="qt-companions-note">在这条视频下面直接做，产物会记下「属于 {videoTaskId.slice(0, 8)}」；参数按默认值走，要细调再去独立工具。</p>
        <CompanionStudio projectId={projectId} parentTaskId={videoTaskId} duration={duration} kind="music" />
        <CompanionStudio projectId={projectId} parentTaskId={videoTaskId} duration={duration} kind="timbre" />
      </section>}
      <section className="qt-preview-tips">
        <strong>这一栏是干嘛的</strong>
        <p>左栏改参数，右栏盯结果。视频不会把表单顶下去，你可以一边看画面一边调时长和提示词。</p>
      </section>
    </aside>
  </div>;
}

export default function QuickToolPage({ tool, onBack, seed }: { tool: Tool; onBack: () => void; seed?: QuickSeed | null }) {
  const [projectId, setProjectId] = useState('');
  const [error, setError] = useState('');
  // 「生成到哪里」：默认草稿箱；选了作品就按作品归属处理（图片直接登记资产，
  // 视频先落草稿再自动归入，两者都不写该作品的分镜表）。
  const [projects, setProjects] = useState<{ id: string; title: string; series?: string }[]>([]);
  const [target, setTarget] = useState('');
  const scoped = tool === 'image' || tool === 'reference-image' || tool === 'text-video' || tool === 'image-video';
  const videoTool = tool === 'text-video' || tool === 'image-video';
  // 左右两栏不是装饰：左栏的提示词库、右栏的素材库都要能把东西递进中间的表单，
  // 所以用一个时间戳票据，让工具面板知道「这是刚点的，不是上次残留的」。
  const [promptTicket, setPromptTicket] = useState<{ text: string; at: number } | null>(null);
  const [frameTicket, setFrameTicket] = useState<{ path: string; title: string; at: number } | null>(null);
  const info = meta[tool];
  // 带回来的画质只认这个工具真有的档位；8K 只属于图片，视频最高 4K。
  const imageQuality: OutputResolution = seed?.quality === '2k' || seed?.quality === '4k' || seed?.quality === '8k' ? seed.quality : 'native';
  const videoQuality: OutputResolution = seed?.quality === '2k' || seed?.quality === '4k' ? seed.quality : 'native';
  const timbreDraft = seed?.fromTask && seed.timbre
    ? { engine: 'voxcpm-2' as const, voice_description: seed.timbre,
        ...(seed.text ? { text: seed.text } : {}), ...(Number.isInteger(seed.seed) ? { seed: seed.seed as number } : {}) }
    : null;
  // 音色库带入的是整张卡片；空字段不塞进去，免得把面板里已有的默认台词盖掉。
  const timbreCardParams = seed?.timbreCard?.params;
  const timbreCardDraft = timbreCardParams
    ? { engine: timbreCardParams.engine === 'indextts-2.5' ? 'indextts-2.5' as const : 'voxcpm-2' as const,
        voice_description: String(timbreCardParams.voice_description || ''),
        ...(timbreCardParams.voice_reference ? { voice_reference: String(timbreCardParams.voice_reference) } : {}),
        ...(timbreCardParams.text ? { text: String(timbreCardParams.text) } : {}) }
    : null;

  useEffect(() => {
    let cancelled = false;
    void quickWorkspace().then(id => { if (!cancelled) setProjectId(id); })
      .catch(reason => { if (!cancelled) setError(reason instanceof Error ? reason.message : '准备单步任务失败'); });
    return () => { cancelled = true; };
  }, []);

  // 作品清单只给能「选归属」的工具拉；清晰化/配乐这些落点固定的工具不用多一次请求。
  useEffect(() => {
    if (!scoped) return;
    let cancelled = false;
    void request<{ projects: { id: string; title: string; series?: string }[] }>('/api/projects')
      .then(data => { if (!cancelled) setProjects(data.projects || []); })
      .catch(() => { if (!cancelled) setProjects([]); });
    return () => { cancelled = true; };
  }, [scoped]);

  const promptTarget = (tool === 'image' || tool === 'text-video' || tool === 'image-video') ? tool : undefined;
  // 出图/出片才有「查提示词、翻素材」的需求；配乐、音色、清晰化给两栏反而是噪音。
  const withRails = Boolean(promptTarget);

  const body = <>
    <button className="sf-back" onClick={onBack}><ChevronLeft size={15} /> 返回工具台</button>
    <header className="qt-head"><span className="qt-icon"><info.icon size={22} /></span><div><p className="sf-step-label">单步工具</p><h1>{info.title}</h1><p>{info.note}</p></div></header>
    {scoped && <div className="qt-target" aria-label="生成到哪里">
      <span className="qt-target-label">生成到</span>
      <select value={target} onChange={event => setTarget(event.target.value)} aria-label="选择生成归属">
        <option value="">草稿箱（不属于任何作品）</option>
        {projects.map(item => <option key={item.id} value={item.id}>{item.title}{item.series ? ` · ${item.series}` : ''}</option>)}
      </select>
      <small>{target
        ? (videoTool ? '先落草稿箱生成，成功后自动复制进该作品；不写它的分镜表。' : '直接登记成该作品的资产；不写它的分镜表。')
        : (projects.length ? '试拍留在这里，随时可删；想归到作品就选上面那栏。' : '还没有正式作品；先新建一部作品，这里就能选归属。')}</small>
    </div>}
    {seed?.fromTask && <p className="qt-status">已带回「{seed.fromTask}」的输入；改完再点生成，工作台不会自动重跑上一条。</p>}
    {error && <p className="sf-error" role="alert">{error}</p>}
    {tool === 'image' && ((target || projectId) ? <KeyframePanel projectId={target || projectId} simple fresh initialMode="text" initialPrompt={seed?.prompt} promptTicket={promptTicket} initialSeed={seed?.seed} initialAspect={seed?.aspect} initialResolution={imageQuality} initialSteps={seed?.steps} options={[]} onBrowse={() => {}} onComplete={() => {}} /> : !error && <p className="qt-status"><LoaderCircle size={14} className="sf-spin" />正在准备单步项目…</p>)}
    {tool === 'reference-image' && <ReferenceImagePanel projectId={target || projectId}
      restore={seed?.fromTask && seed?.image
        ? { image: seed.image, prompt: seed.prompt, aspect: seed.aspect, refine: seed.refine }
        : undefined} />}
    {tool === 'enhance' && (projectId ? <EnhancementPanel projectId={projectId} initialSource={seed?.source || ''} onBack={onBack} /> : !error && <p className="qt-status"><LoaderCircle size={14} className="sf-spin" />正在准备单步项目…</p>)}
    {tool === 'music' && (projectId ? <MusicPanel projectId={projectId} initialCard={seed?.music || null} initialSeed={seed?.seed} onComplete={() => {}} /> : !error && <p className="qt-status"><LoaderCircle size={14} className="sf-spin" />正在准备单步项目…</p>)}
    {tool === 'timbre' && (projectId ? <TimbrePanel projectId={projectId} initialDescription={seed?.timbre} initialDraft={timbreDraft || timbreCardDraft} sourceCard={seed?.timbreCard?.name} onComplete={() => {}} /> : !error && <p className="qt-status"><LoaderCircle size={14} className="sf-spin" />正在准备单步项目…</p>)}
    {tool === 'video-edit' && (projectId ? <Suspense fallback={<p className="qt-status"><LoaderCircle size={14} className="sf-spin" />正在载入剪辑台…</p>}><VideoEditPanel projectId={projectId} /></Suspense> : !error && <p className="qt-status"><LoaderCircle size={14} className="sf-spin" />正在准备单步项目…</p>)}
    {(tool === 'text-video' || tool === 'image-video') && <QuickVideo mode={tool} workspaceId={projectId} adoptInto={target || undefined} initialFrame={seed?.frame} initialPrompt={seed?.prompt} initialDuration={seed?.duration} initialQuality={videoQuality} initialPreset={seed?.preset} initialVoice={seed?.voice} initialMusic={seed?.musicTrack} promptTicket={promptTicket} frameTicket={frameTicket}
      restore={seed?.fromTask && seed?.references?.length
        ? { prompt: seed.prompt, references: seed.references, seed: seed.seed, aspect: seed.aspect }
        : undefined} />}
  </>;

  if (!withRails) return <main className="sf-page qt-page">{body}</main>;
  return <div className="sf-home-shell qt-shell">
    <div className="sf-rail-col sf-rail-col--left">
      <PromptLibraryPanel target={promptTarget} onUsePrompt={text => setPromptTicket({ text, at: Date.now() })} />
    </div>
    <main className="sf-page qt-page">{body}</main>
    <div className="sf-rail-col sf-rail-col--right">
      <MaterialLibraryPanel {...(tool === 'image-video' ? { onUseFrame: (path: string, title: string) => setFrameTicket({ path, title, at: Date.now() }) } : {})} />
    </div>
  </div>;
}
