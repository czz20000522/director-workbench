import { useEffect, useMemo, useState } from 'react';
import { ArrowDown, ArrowUp, Clapperboard, Film, LoaderCircle, Music2, Plus, Scissors, Trash2, Upload } from 'lucide-react';
import { mediaUrl, request } from './api/client';
import ProgressBar, { type TaskProgress } from './TaskProgress';

/** 剪辑台：把已有片段按顺序剪开、拼接、调音量、铺一条 BGM，导出成片。
 *
 * 这里全部是确定性操作（ffmpeg），不跑模型。素材来自作品素材和三步任务产物，
 * 导出结果会登记回作品，可以继续拿来剪或做清晰化。
 */

type MediaAsset = { id: string; kind: string; title: string; sources?: Record<string, string> };
type TaskRecord = {
  id: string; asset_id?: string; status: string; payload?: { kind?: string }; result?: unknown;
};
type Probe = { ok: boolean; duration?: number; has_audio?: boolean; error?: string };
type Clip = {
  key: string; source: string; label: string;
  start: number; end: number; duration: number;
  hasAudio: boolean; volume: number; muted: boolean;
};
type Task = { id: string; status: string; error?: string; progress?: TaskProgress; result?: unknown };

const VIDEO = /\.(mp4|mov|mkv|webm|m4v)$/i;
const AUDIO = /\.(wav|mp3|m4a|flac|aac|ogg)$/i;

const statusLabels: Record<string, string> = {
  scheduler_waiting: '等待调度', preparing: '准备中', queued: '排队中', running: '导出中',
  succeeded: '已完成', failed: '导出失败', stopped: '已停止', needs_reconcile: '结果待核对',
};

function readOutput(result: unknown): string {
  let value = result;
  if (typeof value === 'string') {
    try { value = JSON.parse(value); } catch { return ''; }
  }
  if (!value || typeof value !== 'object') return '';
  const record = value as Record<string, unknown>;
  if (typeof record.output === 'string' && record.output) return record.output;
  const outputs = record.outputs;
  if (Array.isArray(outputs)) {
    const hit = outputs.find(item => typeof item === 'string' ? item : item && typeof (item as { path?: string }).path === 'string');
    if (typeof hit === 'string') return hit;
    if (hit) return (hit as { path: string }).path;
  }
  return '';
}

function mediaPaths(result: unknown): string[] {
  if (!result || typeof result !== 'object') return [];
  const record = result as Record<string, unknown>;
  const found: string[] = [];
  if (typeof record.path === 'string') found.push(record.path);
  if (Array.isArray(record.outputs)) {
    for (const item of record.outputs) {
      // 视频生成任务把产物写成字符串数组；媒体任务写成 {path} 对象，两种都收。
      if (typeof item === 'string') found.push(item);
      else if (item && typeof item === 'object' && typeof (item as { path?: string }).path === 'string') {
        found.push((item as { path: string }).path);
      }
    }
  }
  return found;
}

const clock = (seconds: number) => {
  const value = Math.max(0, seconds);
  const minutes = Math.floor(value / 60);
  const rest = value - minutes * 60;
  return `${minutes}:${rest.toFixed(1).padStart(4, '0')}`;
};

/** 文件还在不在盘上：媒体端点支持 Range，存在回 206，缺失回 404。
 *
 * 作品登记里会留着早已删掉的文件（换机器、清理过 assets 目录都会这样），
 * 音频没有时长探测，选到这种条目只会在导出时才报"文件不存在"。
 */
async function fileAlive(path: string): Promise<boolean> {
  const url = mediaUrl(path);
  if (!url) return false;
  try {
    const response = await fetch(url, { headers: { Range: 'bytes=0-1' } });
    return response.ok;
  } catch { return false; }
}

const round = (value: number) => Math.round(value * 100) / 100;

export default function VideoEditPanel({ projectId }: { projectId: string }) {
  const [sources, setSources] = useState<{ path: string; title: string; origin: string }[]>([]);
  const [audioSources, setAudioSources] = useState<{ path: string; title: string }[]>([]);
  // 本地上传的文件在后端只进了作品的 media 登记，没有登记成素材，
  // 所以每次 load() 重建列表都会把它冲掉。单独存一份，刷新页面之前一直留着。
  const [uploads, setUploads] = useState<{ path: string; title: string }[]>([]);
  const [uploadedAudio, setUploadedAudio] = useState<{ path: string; title: string }[]>([]);
  // 音频没有时长探测：只有这里确认过文件还在，下拉里才允许选。
  const [audioAlive, setAudioAlive] = useState<Record<string, boolean>>({});
  const [probes, setProbes] = useState<Record<string, Probe>>({});
  const [clips, setClips] = useState<Clip[]>([]);
  const [music, setMusic] = useState('');
  const [musicVolume, setMusicVolume] = useState(0.25);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState('');
  const [progress, setProgress] = useState<TaskProgress | null>(null);
  const [error, setError] = useState('');
  const [result, setResult] = useState('');
  const [uploadBusy, setUploadBusy] = useState(false);
  const [projectTitle, setProjectTitle] = useState('');

  const total = useMemo(() => clips.reduce((sum, clip) => sum + Math.max(0, clip.end - clip.start), 0), [clips]);

  const allSources = useMemo(() => {
    const map = new Map<string, { path: string; title: string; origin: string }>();
    for (const item of sources) map.set(item.path, item);
    for (const item of uploads) map.set(item.path, { ...item, origin: '本地上传' });
    return [...map.values()];
  }, [sources, uploads]);

  // 文件已经被删的条目不再出现在素材墙上：点进去也只会报「文件不可用」。
  // 还没探测到的保留（probes[path] 为 undefined），避免刷新瞬间整墙清空。
  const [origin, setOrigin] = useState<'all' | '作品素材' | '任务产物' | '作品目录' | '本地上传'>('all');
  const liveSources = useMemo(() => allSources.filter(item => probes[item.path]?.ok !== false), [allSources, probes]);
  const deadCount = allSources.length - liveSources.length;
  const originCounts = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const item of liveSources) counts[item.origin] = (counts[item.origin] || 0) + 1;
    return counts;
  }, [liveSources]);
  const visibleSources = origin === 'all' ? liveSources : liveSources.filter(item => item.origin === origin);

  const allAudio = useMemo(() => {
    const map = new Map<string, { path: string; title: string }>();
    for (const item of audioSources) map.set(item.path, item);
    for (const item of uploadedAudio) map.set(item.path, item);
    return [...map.values()];
  }, [audioSources, uploadedAudio]);

  const verifyAudio = async (items: { path: string }[]) => {
    if (!items.length) return;
    const entries = await Promise.all(items.map(async item => [item.path, await fileAlive(item.path)] as const));
    setAudioAlive(current => ({ ...current, ...Object.fromEntries(entries) }));
  };

  // 选中的音乐如果其实已经不在了，立刻收回选择，别等导出时才报错。
  useEffect(() => {
    if (music && audioAlive[music] === false) {
      setMusic('');
      setError('这条音频文件已经不在磁盘上，换一条或重新上传。');
    }
  }, [audioAlive, music]);

  const load = async () => {
    setLoading(true); setError('');
    try {
      const [project, tasks] = await Promise.all([
        request<{ assets?: MediaAsset[]; title?: string }>(`/api/projects/${encodeURIComponent(projectId)}`),
        request<{ tasks?: TaskRecord[] }>(`/api/projects/${encodeURIComponent(projectId)}/tasks`),
      ]);
      setProjectTitle(project.title || '');
      const video = new Map<string, { path: string; title: string; origin: string }>();
      const audio = new Map<string, { path: string; title: string }>();
      const assetIds = new Set<string>();
      for (const asset of project.assets || []) {
        const path = asset.sources?.A || '';
        if (!path) continue;
        assetIds.add(asset.id);
        if (asset.kind === 'video' && VIDEO.test(path)) video.set(path, { path, title: asset.title || '视频素材', origin: '作品素材' });
        if (asset.kind === 'audio' && AUDIO.test(path)) audio.set(path, { path, title: asset.title || '音频素材' });
      }
      for (const task of tasks.tasks || []) {
        if (task.status !== 'succeeded') continue;
        // 这条任务的产物已经登记成作品素材了，列表里再出现一次只是噪音。
        if (task.asset_id && assetIds.has(task.asset_id)) continue;
        const paths = [readOutput(task.result), ...mediaPaths(task.result)].filter(Boolean);
        for (const path of paths) {
          if (VIDEO.test(path) && !video.has(path)) {
            video.set(path, { path, title: path.split(/[\\/]/).pop() || '任务产物', origin: '任务产物' });
          }
          if (AUDIO.test(path) && !audio.has(path)) {
            audio.set(path, { path, title: path.split(/[\\/]/).pop() || '任务音频' });
          }
        }
      }
      // 作品 video 目录里还有一批「记录被清理过、文件还在」的视频，直接扫出来补进列表。
      // 扫描失败只是少一来源，不该影响已经加载好的素材。
      try {
        const scanned = await request<{ items: { path: string; title: string }[] }>(
          `/api/projects/${encodeURIComponent(projectId)}/video-sources`);
        for (const item of scanned.items || []) {
          if (!item.path || video.has(item.path)) continue;
          video.set(item.path, { path: item.path, title: item.title, origin: '作品目录' });
        }
      } catch { /* 忽略：已登记的素材仍然可用 */ }
      const videoList = [...video.values()];
      const audioList = [...audio.values()];
      setSources(videoList);
      setAudioSources(audioList);
      // 音频没有时长探测，单独确认一遍文件还在：登记里可能留着早就删掉的文件。
      void verifyAudio(audioList);
      if (videoList.length) {
        const probed = await request<{ items: { source: string; ok: boolean; duration?: number; has_audio?: boolean; error?: string }[] }>(
          '/api/video-edit-probe', { json: { sources: videoList.map(item => item.path) }, timeoutMs: 120_000 },
        );
        setProbes(current => ({ ...current, ...Object.fromEntries(probed.items.map(item => [item.source, item])) }));
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '读取素材失败');
    } finally { setLoading(false); }
  };

  useEffect(() => { void load(); }, [projectId]);

  const addClip = (source: { path: string; title: string }) => {
    const info = probes[source.path];
    if (!info) {
      setError('这段视频还没探测到时长：等一秒再点，或刷新页面重新读取素材。');
      return;
    }
    if (!info.ok || !info.duration) {
      setError(info.error || '这段视频读不出时长，文件可能已被移走或损坏。');
      return;
    }
    setError('');
    setClips(list => [...list, {
      key: crypto.randomUUID(), source: source.path, label: source.title,
      start: 0, end: info.duration as number, duration: info.duration as number,
      hasAudio: Boolean(info.has_audio), volume: 1, muted: false,
    }]);
  };

  const patch = (key: string, changes: Partial<Clip>) => {
    setClips(list => list.map(clip => (clip.key === key ? { ...clip, ...changes } : clip)));
  };
  const move = (index: number, delta: number) => {
    setClips(list => {
      const next = [...list];
      const target = index + delta;
      if (target < 0 || target >= next.length) return list;
      [next[index], next[target]] = [next[target], next[index]];
      return next;
    });
  };

  const upload = async (files: FileList) => {
    setUploadBusy(true); setError('');
    try {
      const form = new FormData();
      for (const file of Array.from(files)) form.append('files', file, `assets/${file.name}`);
      // 上传接口只把文件写进作品目录，返回的 project 是**上传前**的快照，
      // 本次真正落盘的路径只在 uploaded 里；同名文件还会被自动改名成 xxx-1.mp4。
      const data = await request<{ uploaded?: string[]; project?: { media?: Record<string, string> } }>(
        `/api/projects/${encodeURIComponent(projectId)}/upload`, { init: { method: 'POST', body: form } },
      );
      const landed = (data.uploaded || []).filter(path => typeof path === 'string' && path);
      const addedVideo = landed.filter(path => VIDEO.test(path))
        .map(path => ({ path, title: path.split(/[\\/]/).pop() || '上传的视频' }));
      const addedAudio = landed.filter(path => AUDIO.test(path))
        .map(path => ({ path, title: path.split(/[\\/]/).pop() || '上传的音频' }));
      if (addedVideo.length) {
        setUploads(list => [...addedVideo, ...list.filter(item => !addedVideo.some(added => added.path === item.path))]);
        const probed = await request<{ items: { source: string; ok: boolean; duration?: number; has_audio?: boolean; error?: string }[] }>(
          '/api/video-edit-probe', { json: { sources: addedVideo.map(item => item.path) }, timeoutMs: 120_000 },
        );
        setProbes(current => ({ ...current, ...Object.fromEntries(probed.items.map(item => [item.source, item])) }));
      }
      if (addedAudio.length) {
        setUploadedAudio(list => [...addedAudio, ...list.filter(item => !addedAudio.some(added => added.path === item.path))]);
        void verifyAudio(addedAudio);
      }
      const ignored = Array.from(files).length - addedVideo.length - addedAudio.length;
      if (!addedVideo.length && !addedAudio.length) {
        setError('文件传上去了，但作品登记里没找到它们；刷新页面再看一次。');
        return;
      }
      setStatus(`已加入 ${addedVideo.length + addedAudio.length} 个文件${ignored > 0 ? `（另有 ${ignored} 个不是视频或音频，已跳过）` : ''}，可以直接点素材卡片加入时间线。`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '上传失败');
    } finally { setUploadBusy(false); }
  };

  const submit = async () => {
    if (!clips.length) { setError('先往时间线里加至少一段'); return; }
    const bad = clips.find(clip => !(clip.end > clip.start) || clip.start < 0 || clip.end > clip.duration + 0.001);
    if (bad) { setError(`「${bad.label}」的入点/出点不合法`); return; }
    setBusy(true); setError(''); setResult(''); setProgress(null); setStatus('提交导出…');
    try {
      const task = await request<Task & { task?: Task }>(`/api/projects/${encodeURIComponent(projectId)}/video-edit-tasks`, {
        json: {
          idempotency_key: crypto.randomUUID(),
          clips: clips.map(clip => ({
            source: clip.source, label: clip.label,
            start_seconds: round(clip.start), end_seconds: round(clip.end),
            volume: clip.volume, muted: clip.muted,
          })),
          ...(music ? { background_music: music, background_volume: musicVolume } : {}),
        },
      });
      const taskId = task.id || task.task?.id;
      if (!taskId) throw new Error('任务未返回编号');
      let output = '';
      for (let attempt = 0; attempt < 240; attempt += 1) {
        const detail = await request<Task & { task?: Task }>(`/api/tasks/${encodeURIComponent(taskId)}`);
        const current = detail.task || detail;
        setStatus(statusLabels[current.status] || current.status);
        setProgress(current.progress || null);
        if (['succeeded', 'failed', 'stopped', 'needs_reconcile'].includes(current.status)) {
          if (current.status !== 'succeeded') throw new Error(current.error || `导出${statusLabels[current.status] || current.status}`);
          output = readOutput(current.result);
          break;
        }
        await new Promise(resolve => window.setTimeout(resolve, 1000));
      }
      if (!output) throw new Error('任务成功但没有拿到成片路径');
      setResult(output);
      setStatus(`成片已导出：${clock(total)} · ${clips.length} 段`);
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '导出失败');
      setStatus('');
    } finally { setBusy(false); }
  };

  return <div className="ve">
    {loading && <p className="qt-status"><LoaderCircle size={14} className="sf-spin" />正在读取素材…</p>}
    {error && <p className="sf-error" role="alert">{error}</p>}

    <section className="ve-section">
      <header className="ve-head">
        <div><h2><Film size={15} /> 素材</h2><p>{projectTitle ? `当前作品：${projectTitle} · ` : ''}作品素材和已完成的视频任务都能直接剪；也可以上传本地文件。</p></div>
        <label className={`sf-secondary ve-upload${uploadBusy ? ' is-busy' : ''}`}>
          <Upload size={14} /> {uploadBusy ? '上传中…' : '上传视频 / 音频'}
          <input type="file" hidden multiple accept="video/*,audio/*,.mkv,.m4v,.wav,.mp3,.m4a,.flac,.aac,.ogg"
            onChange={event => { const files = event.target.files; if (files?.length) void upload(files); event.target.value = ''; }} />
        </label>
      </header>
      {!loading && !allSources.length && <p className="sf-fine">还没有视频素材。先用「文生视频 / 图生视频」出一条，或者在上面上传。</p>}
      {liveSources.length > 0 && <div className="ve-origin-tabs" role="group" aria-label="按来源筛选素材">
        {([['all', '全部'], ['作品素材', '作品素材'], ['任务产物', '任务产物'], ['作品目录', '目录扫描'], ['本地上传', '本地上传']] as const)
          .filter(([key]) => key === 'all' || originCounts[key])
          .map(([key, label]) => <button key={key} type="button" aria-pressed={origin === key} onClick={() => setOrigin(key)}>
            {label}<span>{key === 'all' ? liveSources.length : originCounts[key]}</span>
          </button>)}
      </div>}
      {deadCount > 0 && <p className="sf-fine">已隐藏 {deadCount} 条失效素材（对应文件已被删除或移入回收站）。</p>}
      <div className="ve-sources">
        {visibleSources.map(source => {
          const info = probes[source.path];
          const note = info
            ? (info.ok && info.duration
                ? `${clock(info.duration)} · ${info.has_audio ? '有原声' : '无音轨'}`
                : (info.error || '读不出时长'))
            : (loading ? '读取中…' : '还没读到时长，刷新再试');
          return <button key={source.path} type="button" className="ve-source"
            onClick={() => addClip(source)} title={source.path}>
            <span className="ve-source-top"><Clapperboard size={13} /><em>{source.origin}</em></span>
            <strong>{source.title}</strong>
            <small>{note}</small>
            <span className="ve-source-add"><Plus size={12} /> 加入时间线</span>
          </button>;
        })}
      </div>
    </section>

    <section className="ve-section">
      <header className="ve-head">
        <div><h2><Scissors size={15} /> 时间线</h2><p>按这里的顺序拼接；每段可以改入点、出点、音量或静音。</p></div>
        <span className="ve-total">{clips.length} 段 · {clock(total)}</span>
      </header>
      {!clips.length && <p className="sf-fine">时间线是空的：点上面任意一张素材卡片。</p>}
      <div className="ve-timeline">
        {clips.map((clip, index) => <div className="ve-clip" key={clip.key}>
          <span className="ve-clip-index">{index + 1}</span>
          <div className="ve-clip-name">
            <strong>{clip.label}</strong>
            <small>{clock(clip.duration)} · {clip.hasAudio ? '有原声' : '无音轨'}</small>
          </div>
          <label className="ve-num">入点<input type="number" min={0} max={clip.duration} step={0.1} value={round(clip.start)}
            onChange={event => patch(clip.key, { start: Math.min(Math.max(0, Number(event.target.value)), clip.end - 0.05) })} /></label>
          <label className="ve-num">出点<input type="number" min={0} max={clip.duration} step={0.1} value={round(clip.end)}
            onChange={event => patch(clip.key, { end: Math.min(Math.max(clip.start + 0.05, Number(event.target.value)), clip.duration) })} /></label>
          <label className="ve-vol">音量<input type="range" min={0} max={1.5} step={0.05} value={clip.volume} disabled={clip.muted}
            onChange={event => patch(clip.key, { volume: Number(event.target.value) })} /><small>{Math.round(clip.volume * 100)}%</small></label>
          <label className="ve-mute"><input type="checkbox" checked={clip.muted}
            onChange={event => patch(clip.key, { muted: event.target.checked })} />静音</label>
          <span className="ve-clip-actions">
            <button type="button" disabled={index === 0} aria-label="上移" onClick={() => move(index, -1)}><ArrowUp size={13} /></button>
            <button type="button" disabled={index === clips.length - 1} aria-label="下移" onClick={() => move(index, 1)}><ArrowDown size={13} /></button>
            <button type="button" aria-label="删除这一段" onClick={() => setClips(list => list.filter(item => item.key !== clip.key))}><Trash2 size={13} /></button>
          </span>
        </div>)}
      </div>
    </section>

    <section className="ve-section ve-audio">
      <header className="ve-head">
        <div><h2><Music2 size={15} /> 背景音乐</h2><p>可选。铺满整条成片，原声不够的地方可以垫一条。</p></div>
      </header>
      <div className="ve-audio-row">
        <label className="sf-field"><span>选择音频</span>
          <select value={music} onChange={event => setMusic(event.target.value)}>
            <option value="">不铺音乐</option>
            {allAudio.map(item => {
              const dead = audioAlive[item.path] === false;
              return <option key={item.path} value={item.path} disabled={dead}>
                {item.title}{dead ? '（文件已不在）' : ''}
              </option>;
            })}
          </select>
        </label>
        {music && <label className="ve-vol ve-music-vol">音乐音量<input type="range" min={0} max={1} step={0.05} value={musicVolume}
          onChange={event => setMusicVolume(Number(event.target.value))} /><small>{Math.round(musicVolume * 100)}%</small></label>}
      </div>
      {!allAudio.length && <p className="sf-fine">素材里还没有音频。可以先用「配乐」或「音色设计」生成，或者在上面的上传入口选一个音频文件。</p>}
    </section>

    {status && <p className="qt-status">{busy && <LoaderCircle size={14} className="sf-spin" />}{status}</p>}
    <ProgressBar progress={progress} />
    <div className="ve-actions">
      <button type="button" className="sf-primary" disabled={busy || !clips.length} onClick={() => void submit()}>
        {busy ? '导出中…' : `导出成片 · ${clock(total)}`}
      </button>
      {result && <a className="sf-secondary" href={mediaUrl(result)} download>下载 mp4</a>}
    </div>

    {result && <section className="ve-result">
      <video src={mediaUrl(result)} controls preload="metadata" />
      <p className="sf-fine">这条成片已经登记回作品素材，可以继续拿来剪、推清晰化，或作为图生视频的开始画面。</p>
    </section>}
  </div>;
}
