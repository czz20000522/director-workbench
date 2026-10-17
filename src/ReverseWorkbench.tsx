import { useEffect, useMemo, useRef, useState } from 'react';
import { Activity, ArrowDownToLine, ArrowRight, Boxes, BrainCircuit, Check, Film, FileText, LoaderCircle, MapPinned, Mic2, RefreshCw, Search, Upload, Users } from 'lucide-react';
import TaskQueueStatus, { type QueueReceipt } from './TaskQueueStatus';

type Stage = { id: string; label: string; status: string; executor: string };
type TimelineSegment = { id: string; start_seconds: number; end_seconds: number; frame?: string | null; status: string; facts: string[]; inference: string; user_note: string };
type ArtifactReviewHistory = { summary: string | null; user_status: string | null; status: string | null; provenance: string | null; confidence: number | null; revision: number | null; recorded_at: number | string | null };
type Artifact = { review_history?: ArtifactReviewHistory[]; title: string; status: string; provenance: string; summary: string; evidence?: string[]; confidence?: number | null; user_status: string; model_candidate?: { summary: string }; original_candidate?: { summary: string } };
type Analysis = {
  import_task_id?: string;
  import_status?: string;
  import_task?: QueueReceipt & { error?: string };
  content_mode?: string;
  content_mode_reason?: string;
  content_mode_user_status?: string;
  revision?: number;
  id: string;
  title: string;
  status: string;
  created_at: string;
  source: { original_name: string; path: string; duration_seconds: number; width?: number; height?: number; fps?: number; has_audio?: boolean };
  stages: Stage[];
  timeline: TimelineSegment[];
  artifacts: Record<string, Artifact>;
  production_seed?: string;
};
type ModelStatus = {
  primary?: { label: string; ready: boolean; model_ready: boolean; projector_ready: boolean; model_size_bytes: number; projector_size_bytes: number };
  fast_optional?: { label: string; role: string; ready: boolean; model_size_bytes: number };
};

const artifactOrder = ['source', 'timeline', 'transcript', 'audio', 'story', 'shots', 'characters', 'scenes', 'actions', 'mechanisms', 'adaptation', 'production_seed'];
const artifactIcons: Record<string, typeof Film> = { source: Film, timeline: Activity, transcript: FileText, audio: Mic2, story: FileText, shots: Film, characters: Users, scenes: MapPinned, actions: Activity, mechanisms: BrainCircuit, adaptation: Boxes, production_seed: ArrowRight };
const provenanceLabels: Record<string, string> = { observable_fact: '可观测事实', asr_candidate: 'ASR 候选', model_inference: '模型推断', user_confirmed: '用户确认', adaptation_advice: '复刻建议' };

async function responseError(response: Response) {
  const text = await response.text();
  try {
    const body = JSON.parse(text) as { detail?: unknown };
    if (typeof body.detail === 'string') return body.detail;
    if (body.detail && typeof body.detail === 'object' && 'message' in body.detail) return String(body.detail.message);
  } catch { /* A plain-text backend error is already suitable for display. */ }
  return text || `请求失败（${response.status}）`;
}

function mediaUrl(path?: string | null) {
  return path ? `/media-file?path=${encodeURIComponent(path)}` : undefined;
}

function reviewTime(value: number | string | null) {
  if (value == null || value === '') return '时间未记录';
  const date = new Date(typeof value === 'number' ? value * 1000 : value);
  return Number.isFinite(date.getTime()) ? date.toLocaleString('zh-CN') : '时间未记录';
}

function clock(seconds: number) {
  const minutes = Math.floor(seconds / 60);
  const rest = Math.max(0, seconds - minutes * 60);
  return `${String(minutes).padStart(2, '0')}:${rest.toFixed(1).padStart(4, '0')}`;
}

function TimelineCard({ segment, busy, onSave }: { segment: TimelineSegment; busy: boolean; onSave: (id: string, note: string) => Promise<boolean> }) {
  const [note, setNote] = useState(segment.user_note);
  const [editing, setEditing] = useState(false);
  return <article>
    <div className="reverse-frame">{segment.frame ? <img src={mediaUrl(segment.frame)} alt={`${segment.id} 关键帧`} /> : <Film size={23} />}<b>{segment.id}</b></div>
    <strong>{clock(segment.start_seconds)} - {clock(segment.end_seconds)}</strong>
    <p>{segment.user_note || segment.inference}</p>
    <span>{segment.status === 'reviewed' ? '已人工校订' : segment.status === 'model_analyzed' ? '模型观察 · 待校订' : '待分析 / 校订'}</span>
    {editing ? <div className="reverse-timeline-editor"><label>这一段的观察与改编意图<textarea aria-label={`${segment.id} 人工校订`} rows={4} value={note} onChange={event => setNote(event.target.value)} /></label><button className="button secondary compact" disabled={busy || !note.trim()} onClick={async () => { if (await onSave(segment.id, note)) setEditing(false); }}>{busy ? '保存中…' : '保存校订'}</button><button className="text-button" disabled={busy} onClick={() => { setNote(segment.user_note); setEditing(false); }}>取消</button></div> : <button className="button secondary compact" disabled={busy} onClick={() => { setNote(segment.user_note || segment.inference); setEditing(true); }}>校订这一段</button>}
  </article>;
}

const contentModes: Record<string, string> = { narrative: '叙事', music_performance: '音乐表演', dance: '舞蹈', showcase: '展示', mood: '氛围', technical: '技术演示', other: '其他' };

function ClassificationEditor({ analysis, projectId, disabled, onSaved }: { analysis: Analysis; projectId: string; disabled: boolean; onSaved: (analysis: Analysis) => void }) {
  const [mode, setMode] = useState(analysis.content_mode || 'other');
  const [reason, setReason] = useState(analysis.content_mode_reason || '');
  const [revision, setRevision] = useState(analysis.revision ?? 0);
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState('');
  const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  function begin() { setMode(analysis.content_mode || 'other'); setReason(analysis.content_mode_reason || ''); setRevision(analysis.revision ?? 0); setMessage(''); setEditing(true); }
  async function save() {
    if (saving || !reason.trim()) return;
    setSaving(true); setMessage('');
    const url = `/api/projects/${encodeURIComponent(projectId)}/reverse-analyses/${encodeURIComponent(analysis.id)}`;
    try {
      const response = await fetch(`${url}/classification`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ content_mode: mode, reason: reason.trim(), expected_revision: revision }) });
      if (response.status === 409) {
        const latestResponse = await fetch(url);
        if (!latestResponse.ok) throw new Error('内容类型已变化，暂时无法读取最新版本；请稍后重新打开校订。');
        const latest = await latestResponse.json() as Analysis;
        if (!alive.current) return;
        onSaved(latest); setMode(latest.content_mode || 'other'); setReason(latest.content_mode_reason || ''); setRevision(latest.revision ?? 0);
        setMessage('记录已更新，已读取最新版本。请核对后重新填写并保存。'); return;
      }
      if (!response.ok) throw new Error(await responseError(response));
      const payload = await response.json();
      if (!alive.current) return;
      onSaved(payload.analysis); setEditing(false); setMessage('内容类型已保存为人工校订。');
    } catch (error) { if (alive.current) setMessage(error instanceof Error ? error.message : '保存失败'); }
    finally { if (alive.current) setSaving(false); }
  }
  return <div className="reverse-classification">
    <span>内容类型 · {contentModes[analysis.content_mode || ''] || '待判断'}{analysis.content_mode_user_status === 'reviewed' ? ' · 已校订' : ''}</span>
    <button className="text-button" disabled={disabled || saving} onClick={() => editing ? setEditing(false) : begin()}>{editing ? '收起校订' : '校订类型'}</button>
    {editing && <div className="reverse-timeline-editor"><label>内容类型<select aria-label="内容类型" value={mode} disabled={saving} onChange={event => setMode(event.target.value)}>{Object.entries(contentModes).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label><label>判断理由<textarea aria-label="内容类型判断理由" rows={2} maxLength={2000} value={reason} disabled={saving} onChange={event => setReason(event.target.value)} /></label><button className="button secondary compact" disabled={disabled || saving || !reason.trim() || reason.length > 2000} onClick={() => void save()}>{saving ? '保存中…' : '保存内容类型'}</button></div>}
    {message && <p role="status">{message}</p>}
  </div>;
}

export default function ReverseWorkbench({ projectId, initialAnalysisId = '', onConverted }: { projectId: string; initialAnalysisId?: string; onConverted: (count: number) => void }) {
  const [referencePage, setReferencePage] = useState<'timeline' | 'analysis'>('timeline');
  const [analyses, setAnalyses] = useState<Analysis[]>([]);
  const [activeId, setActiveId] = useState(initialAnalysisId);
  const [modelStatus, setModelStatus] = useState<ModelStatus>({});
  const [uploading, setUploading] = useState(false);
  const [importControlling, setImportControlling] = useState(false);
  const [startingAnalysis, setAnalyzing] = useState(false);
  const [semanticJob, setSemanticJob] = useState<{ id: string; task_id?: string; status: string; queue?: QueueReceipt['queue']; error?: string; progress?: { observed_frames: number; total_frames: number } } | null>(null);
  const observedJob = useRef('');
  const analyzing = startingAnalysis || ['queued', 'scheduler_waiting', 'preparing', 'submitting', 'running', 'stop_requested', 'stopping'].includes(semanticJob?.status ?? '');
  const analysisProgress = ['queued', 'running'].includes(semanticJob?.status ?? '') ? semanticJob?.progress : undefined;
  const analysisLabel = analysisProgress
    ? analysisProgress.observed_frames === analysisProgress.total_frames
      ? '正在整理分析结果…'
      : `已分析 ${analysisProgress.observed_frames} / ${analysisProgress.total_frames} 帧`
    : '正在后台分析…';
  const [converting, setConverting] = useState(false);
  const [conversionConflict, setConversionConflict] = useState(false);
  const [saving, setSaving] = useState(false);
  const [loading, setLoading] = useState(true);
  const [timelineSaving, setTimelineSaving] = useState(false);
  const [selectedArtifact, setSelectedArtifact] = useState('story');
  const [artifactDraft, setArtifactDraft] = useState('');
  const [notice, setNotice] = useState('');
  const [error, setError] = useState('');
  const fileRef = useRef<HTMLInputElement>(null);
  const active = analyses.find(item => item.id === activeId) ?? analyses[0];
  const importing = Boolean(active?.import_task_id && !['succeeded', 'failed', 'stopped', 'needs_reconcile'].includes(active.import_task?.status ?? active.import_status ?? 'queued'));
  const selected = active?.artifacts?.[selectedArtifact];
  const artifactContext = useRef({ key: '', epoch: 0 });
  const artifactContextKey = JSON.stringify([projectId, active?.id, selectedArtifact]);
  if (artifactContext.current.key !== artifactContextKey) artifactContext.current = { key: artifactContextKey, epoch: artifactContext.current.epoch + 1 };
  const artifactDraftRef = useRef(artifactDraft);
  artifactDraftRef.current = artifactDraft;
  useEffect(() => { setSaving(false); }, [artifactContextKey]);
  useEffect(() => () => { artifactContext.current.epoch += 1; }, []);

  async function load() {
    if (!projectId) return;
    const [analysisResponse, modelResponse] = await Promise.all([
      fetch(`/api/projects/${encodeURIComponent(projectId)}/reverse-analyses`),
      fetch('/api/models/reverse-analysis'),
    ]);
    if (!analysisResponse.ok) throw new Error(await responseError(analysisResponse));
    const analysisData = await analysisResponse.json();
    const modelData = modelResponse.ok ? await modelResponse.json() : {};
    const next = Array.isArray(analysisData.analyses) ? analysisData.analyses as Analysis[] : [];
    setAnalyses(next);
    setModelStatus(modelData);
    if (initialAnalysisId && !next.some(item => item.id === initialAnalysisId)) setNotice('来源拆解已不可用，当前显示其他参考；分镜来源记录仍保留。');
    setActiveId(current => next.some(item => item.id === current) ? current : (next[0]?.id ?? ''));
  }

  useEffect(() => { void load().catch(reason => setError(String(reason))).finally(() => setLoading(false)); }, [projectId]);
  const artifactDirty = useRef(false);
  useEffect(() => { artifactDirty.current = false; setArtifactDraft(selected?.summary ?? ''); }, [projectId, selectedArtifact, active?.id]);
  useEffect(() => { if (!artifactDirty.current) setArtifactDraft(selected?.summary ?? ''); }, [selected?.summary]);

  useEffect(() => {
    if (!active?.import_task_id || ['succeeded', 'failed', 'stopped', 'needs_reconcile'].includes(active.import_task?.status ?? active.import_status ?? '')) return;
    const analysisId = active.id;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function pollImport() {
      let terminal = false;
      try {
        const response = await fetch(`/api/projects/${encodeURIComponent(projectId)}/reverse-analyses/${encodeURIComponent(analysisId)}`, { signal: controller.signal });
        if (!response.ok) throw new Error(await responseError(response));
        const result = await response.json() as Analysis;
        if (controller.signal.aborted) return;
        setAnalyses(previous => previous.map(item => item.id === analysisId ? result : item));
        const status = result.import_task?.status ?? result.import_status ?? (result.timeline.length ? 'succeeded' : 'queued');
        terminal = ['succeeded', 'failed', 'stopped', 'needs_reconcile'].includes(status);
        if (status === 'succeeded') { setError(''); setNotice('参考素材拆分已完成，等待人工校订；不会自动提交内容分析。'); }
        else if (terminal) setError(result.import_task?.error || `参考素材任务${status === 'needs_reconcile' ? '待核对' : status === 'stopped' ? '已停止' : '失败'}，素材与回执已保留。`);
      } catch (reason) { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : '参考任务读取失败，请刷新重试。'); }
      finally { if (!controller.signal.aborted && !terminal) timer = setTimeout(() => void pollImport(), 2000); }
    }
    void pollImport();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [projectId, active?.id, active?.import_task_id, active?.import_status, active?.import_task?.status]);

  async function controlImport(action: 'stop' | 'resume' | 'reconcile') {
    if (!active?.import_task_id || importControlling) return;
    setImportControlling(true); setError('');
    const analysisId = active.id;
    try {
      const response = await fetch(`/api/projects/${encodeURIComponent(projectId)}/tasks/${encodeURIComponent(active.import_task_id)}/${action}`, { method: 'POST' });
      if (!response.ok) throw new Error(await responseError(response));
      const latest = await fetch(`/api/projects/${encodeURIComponent(projectId)}/reverse-analyses/${encodeURIComponent(analysisId)}`);
      if (!latest.ok) throw new Error(await responseError(latest));
      const result = await latest.json() as Analysis;
      setAnalyses(previous => previous.map(item => item.id === analysisId ? result : item));
      setNotice(action === 'stop' ? '已请求停止参考拆分，等待服务器确认。' : action === 'reconcile' ? '已核对原拆分任务，没有自动重新提交。' : '已请求继续原拆分任务。');
    } catch (reason) { setError(reason instanceof Error ? reason.message : '参考任务操作失败。'); }
    finally { setImportControlling(false); }
  }

  useEffect(() => {
    if (!active?.id) return;
    const analysisId = active.id;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    setSemanticJob(null); observedJob.current = '';
    async function poll() {
      try {
        const response = await fetch(`/api/projects/${encodeURIComponent(projectId)}/reverse-analyses/${encodeURIComponent(analysisId)}/semantic-jobs/current`);
        if (!response.ok) throw new Error(await responseError(response));
        const { job } = await response.json();
        if (cancelled) return;
        setSemanticJob(job);
        const key = job ? `${job.id}:${job.status}` : '';
        if (key && key !== observedJob.current) {
          if (job.status === 'succeeded') {
            const result = await fetch(`/api/projects/${encodeURIComponent(projectId)}/reverse-analyses/${encodeURIComponent(analysisId)}`);
            if (!result.ok) throw new Error(await responseError(result));
            const analysis = await result.json() as Analysis;
            if (cancelled) return;
            setAnalyses(previous => previous.map(item => item.id === analysisId ? analysis : item));
            setError('');
            setNotice('后台内容分析已完成，结果已保存，等待人工校订。');
          } else if (job.status === 'failed' || job.status === 'needs_reconcile') setError(job.error || '分析未完成，请检查任务状态。');
          observedJob.current = key;
        }
      } catch (reason) { if (!cancelled) setError(reason instanceof Error ? reason.message : String(reason)); }
      finally { if (!cancelled) timer = setTimeout(() => void poll(), 2000); }
    }
    void poll();
    return () => { cancelled = true; clearTimeout(timer); };
  }, [projectId, active?.id]);

  async function uploadReference(file?: File) {
    if (!file || !projectId) return;
    setUploading(true); setError(''); setNotice('');
    try {
      const form = new FormData(); form.append('file', file);
      const response = await fetch(`/api/projects/${encodeURIComponent(projectId)}/reverse-analyses`, { method: 'POST', body: form });
      if (!response.ok) throw new Error(await responseError(response));
      const analysis = await response.json() as Analysis;
      setAnalyses(previous => [analysis, ...previous.filter(item => item.id !== analysis.id)]);
      setActiveId(analysis.id);
      setNotice(analysis.import_task_id ? '参考素材已上传，拆分任务已接受；可等待队列回执。' : '媒体探测、关键帧抽取和音轨提取已完成。');
    } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setUploading(false); if (fileRef.current) fileRef.current.value = ''; }
  }

  async function saveArtifact() {
    if (!active || !selected || saving) return;
    const context = { ...artifactContext.current };
    const isCurrent = () => artifactContext.current.key === context.key && artifactContext.current.epoch === context.epoch;
    const analysisId = active.id;
    const submittedDraft = artifactDraft;
    const url = `/api/projects/${encodeURIComponent(projectId)}/reverse-analyses/${encodeURIComponent(analysisId)}`;
    setSaving(true); setError(''); setNotice('');
    try {
      const response = await fetch(`${url}/artifacts/${encodeURIComponent(selectedArtifact)}`, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ expected_revision: active.revision ?? 0, summary: submittedDraft, user_status: 'reviewed', confidence: selected.confidence }),
      });
      if (!isCurrent()) return;
      if (response.status === 409) {
        const latestResponse = await fetch(url);
        if (!latestResponse.ok) throw new Error('版本已变化，读取最新记录失败；本地草稿仍保留，请稍后重试。');
        const latest = await latestResponse.json() as Analysis;
        if (!isCurrent()) return;
        artifactDirty.current = true;
        setAnalyses(previous => previous.map(item => item.id === analysisId ? latest : item));
        setError('版本已变化，已读取最新记录并保留本地草稿。请对照当前保存版本，核对后再保存。');
        return;
      }
      if (!response.ok) throw new Error(await responseError(response));
      const payload = await response.json();
      if (!isCurrent()) return;
      artifactDirty.current = artifactDraftRef.current !== submittedDraft;
      setAnalyses(previous => previous.map(item => item.id === analysisId ? payload.analysis : item));
      setNotice(`${selected.title}已保存为人工校订版本。${artifactDirty.current ? '后续输入仍保留在本地草稿。' : ''}`);
    } catch (reason) { if (isCurrent()) setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { if (isCurrent()) setSaving(false); }
  }

  async function convertToProduction() {
    if (!active) return;
    setConverting(true); setError('');
    try {
      const response = await fetch(`/api/projects/${encodeURIComponent(projectId)}/reverse-analyses/${encodeURIComponent(active.id)}/convert`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ expected_revision: active.revision ?? 0 }) });
      if (response.status === 409) setConversionConflict(true);
      if (!response.ok) throw new Error(await responseError(response));
      setConversionConflict(false);
      const payload = await response.json();
      setAnalyses(previous => previous.map(item => item.id === active.id ? payload.analysis : item));
      onConverted(Array.isArray(payload.created_segments) ? payload.created_segments.length : 0);
    } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setConverting(false); }
  }

  async function saveTimeline(segmentId: string, userNote: string) {
    if (!active) return false;
    setTimelineSaving(true); setError('');
    try {
      const response = await fetch(`/api/projects/${encodeURIComponent(projectId)}/reverse-analyses/${encodeURIComponent(active.id)}/timeline/${encodeURIComponent(segmentId)}`, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ expected_revision: active.revision ?? 0, user_note: userNote, status: 'reviewed' }),
      });
      if (!response.ok) throw new Error(await responseError(response));
      const payload = await response.json();
      setAnalyses(previous => previous.map(item => item.id === active.id ? payload.analysis : item));
      setNotice(`${segmentId} 校订已保存${active.production_seed ? '；已有制作分镜保持原样，请在创作与复刻中单独修改。' : '，转入制作时会带入分镜草案。'}`);
      return true;
    } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); return false; }
    finally { setTimelineSaving(false); }
  }

  function exportAnalysis() {
    if (!active) return;
    const url = URL.createObjectURL(new Blob([JSON.stringify(active, null, 2)], { type: 'application/json' }));
    const link = document.createElement('a'); link.href = url; link.download = `${active.id}-analysis.json`; link.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  async function runSemanticAnalysis() {
    if (!active || !primary?.ready) return;
    setAnalyzing(true); setError(''); setNotice('');
    try {
      const response = await fetch(`/api/projects/${encodeURIComponent(projectId)}/reverse-analyses/${encodeURIComponent(active.id)}/semantic-jobs`, { method: 'POST' });
      if (!response.ok) throw new Error(await responseError(response));
      const payload = await response.json();
      setSemanticJob(payload.job);
      setNotice('已提交后台分析，可以离开本页；返回或刷新后会继续显示任务状态。');
    } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setAnalyzing(false); }
  }

  async function reconcileAnalysis() {
    if (!active) return;
    setAnalyzing(true); setError(''); setNotice('');
    try {
      const response = await fetch(`/api/projects/${encodeURIComponent(projectId)}/reverse-analyses/${encodeURIComponent(active.id)}/semantic-jobs/reconcile`, { method: 'POST' });
      if (!response.ok) throw new Error(await responseError(response));
      const payload = await response.json();
      setSemanticJob(payload.job);
      setNotice('已找回原分析结果，没有重新运行模型。');
    } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setAnalyzing(false); }
  }
  async function stopSemanticAnalysis() {
    if (!active || !semanticJob?.task_id || startingAnalysis) return;
    setAnalyzing(true); setError('');
    try {
      const response = await fetch(`/api/projects/${encodeURIComponent(projectId)}/tasks/${encodeURIComponent(semanticJob.task_id)}/stop`, { method: 'POST' });
      if (!response.ok) throw new Error(await responseError(response));
      const result = await fetch(`/api/projects/${encodeURIComponent(projectId)}/reverse-analyses/${encodeURIComponent(active.id)}/semantic-jobs/current`);
      if (!result.ok) throw new Error(await responseError(result));
      setSemanticJob((await result.json()).job);
      setNotice('已请求停止内容分析，等待服务器确认；原参考和人工校订保留。');
    } catch (reason) { setError(reason instanceof Error ? reason.message : '停止请求失败。'); }
    finally { setAnalyzing(false); }
  }

  const readyArtifacts = useMemo(() => active ? Object.values(active.artifacts).filter(item => item.status === 'ready').length : 0, [active]);
  const primary = modelStatus.primary;
  const fast = modelStatus.fast_optional;
  const busy = uploading || importing || analyzing || converting || saving || timelineSaving;

  if (loading) return <section className="reverse-empty" role="status"><LoaderCircle className="spin" size={24} /><p>正在读取参考拆解…</p></section>;

  if (!active) return <section className="reverse-empty"><div className="reverse-empty-mark"><Search size={28} /></div><span className="director-kicker">参考素材逆向拆解</span><h1>从一段参考开始</h1><p>导入音视频，逐段观察与校订。拆解结果可以转为自己的制作草案。</p><input ref={fileRef} type="file" accept="video/*,audio/*" hidden onChange={event => void uploadReference(event.target.files?.[0])} /><button className="button primary" disabled={uploading} onClick={() => fileRef.current?.click()}>{uploading ? <LoaderCircle className="spin" size={16} /> : <Upload size={16} />}{uploading ? '正在拆分素材…' : '导入参考素材'}</button><div className="reverse-empty-facts"><span>保留原始参考</span><span>每项可校订</span><span>可转为制作草案</span></div>{error && <p className="director-error">{error}</p>}</section>;

  return <section className="reverse-workbench" data-reference-page={referencePage}>
    <header className="reverse-heading"><div><span className="director-kicker">参考素材逆向拆解</span><h1>{active.title}</h1><p>{active.source.original_name} · {active.source.width ?? '?'}x{active.source.height ?? '?'} · {active.source.duration_seconds.toFixed(1)} 秒</p></div><div className="reverse-heading-actions"><select aria-label="选择参考拆解" disabled={busy} value={active.id} onChange={event => setActiveId(event.target.value)}>{analyses.map(item => <option key={item.id} value={item.id}>{item.title}</option>)}</select><input ref={fileRef} type="file" accept="video/*,audio/*" hidden onChange={event => void uploadReference(event.target.files?.[0])} /><button className="button secondary" disabled={busy} onClick={() => fileRef.current?.click()}><Upload size={15} />新增参考</button>{semanticJob?.status === 'needs_reconcile' && <button className="button secondary" disabled={busy} onClick={() => void reconcileAnalysis()}>核对分析结果</button>}<button className="button secondary" disabled={busy || semanticJob?.status === 'needs_reconcile' || !primary?.ready || !active.timeline.some(segment => segment.frame)} onClick={() => void runSemanticAnalysis()}>{analyzing ? <LoaderCircle className="spin" size={15} /> : <BrainCircuit size={15} />}{semanticJob?.status === 'needs_reconcile' ? '分析任务待核对' : analyzing ? analysisLabel : active.stages.some(stage => stage.id === 'semantic' && stage.status === 'done') ? '重新分析内容' : '分析内容与分镜'}</button><button className="button primary" disabled={busy || !active.timeline.length} onClick={() => void convertToProduction()}>{converting ? <LoaderCircle className="spin" size={15} /> : <ArrowRight size={15} />}{active.production_seed ? '打开制作草案' : '转为制作草案'}</button><button className="button secondary" onClick={exportAnalysis}><ArrowDownToLine size={15} />导出拆解</button></div></header>
    {active.import_task && <TaskQueueStatus task={{ ...active.import_task, asset_id: active.import_task.asset_id || active.id }} />}
    {semanticJob && <TaskQueueStatus task={{ ...semanticJob, asset_id: active.id }} />}
    {semanticJob?.task_id && analyzing && <button className="button secondary compact" disabled={startingAnalysis || ['stop_requested', 'stopping'].includes(semanticJob.status)} onClick={() => void stopSemanticAnalysis()}>停止内容分析</button>}
    {importing && <p role="status">参考拆分任务已登记，等待处理完成。素材探测结果将在完成后显示。</p>}
    {active.import_task_id && <div className="reverse-heading-actions">{importing && <button className="button secondary compact" disabled={importControlling} onClick={() => void controlImport('stop')}>停止参考拆分</button>}{['failed', 'stopped'].includes(active.import_task?.status ?? active.import_status ?? '') && <button className="button secondary compact" disabled={importControlling} onClick={() => void controlImport('resume')}>继续参考拆分</button>}{(active.import_task?.status ?? active.import_status) === 'needs_reconcile' && <button className="button secondary compact" disabled={importControlling} onClick={() => void controlImport('reconcile')}>核对参考拆分</button>}</div>}
    <ClassificationEditor key={`${projectId}:${active.id}`} analysis={active} projectId={projectId} disabled={busy} onSaved={analysis => setAnalyses(previous => previous.map(item => item.id === analysis.id ? analysis : item))} />

    {conversionConflict && <section role="alert" className="director-creative-conflict"><p>参考已更新，本次未转换。重新读取后请先核对内容，再决定是否转为制作草案。</p><button className="button secondary compact" disabled={busy} onClick={async () => { try { await load(); setConversionConflict(false); setError(''); setNotice('已读取最新参考；请核对后再转换。'); } catch (reason) { setError(String(reason)); } }}>读取最新参考</button></section>}
    <details className="reverse-model-details"><summary>分析能力与模型状态</summary><div className="reverse-model-strip"><BrainCircuit size={17} /><div><strong>{primary?.label ?? 'Qwen3.8-27B Uncensored Q4_K_M'}</strong><span>主模型 · 统一完成视觉理解、剧情分析、传播机制与提示词写作</span></div><span className={primary?.ready ? 'ready' : 'waiting'}>{primary?.ready ? '模型文件齐备' : primary?.model_ready ? '缺少视觉投影' : '主模型缺失'}</span><div className="reverse-model-divider" /><div><strong>{fast?.label ?? 'Qwen3-VL-8B-Instruct Q4_K_M'}</strong><span>可选快速预检，不是主流程必经步骤</span></div><span className={fast?.ready ? 'ready' : 'waiting'}>{fast?.ready ? '快速模型可用' : '快速模型缺失'}</span><button className="icon-button" aria-label="刷新模型状态" title="刷新模型状态" disabled={busy} onClick={() => void load().catch(reason => setError(String(reason)))}><RefreshCw size={15} /></button></div></details>

    <p className="creation-route-hint">{active.production_seed ? '此参考已有制作草案。再次打开不会重复添加，也不会覆盖你在制作页的修改。' : '转入后按参考时间段创建草案，抽样间隔仍需调整为真实分镜；参考画面不会自动作为本作首帧。'}{!primary?.ready && ' 当前自动内容分析尚未就绪，可以先手工校订并转入制作。'}</p>
    <details className="director-progress-details"><summary>查看拆解进度</summary><div className="reverse-stage-rail" aria-label="参考拆解阶段">{active.stages.map((stage, index) => <div key={stage.id} className={`reverse-stage ${stage.status}`}><span>{stage.status === 'done' ? <Check size={13} /> : String(index + 1).padStart(2, '0')}</span><div><strong>{stage.label}</strong><small>{stage.status === 'done' ? '已完成' : stage.status === 'ready' ? '可以开始' : '待准备'}</small></div></div>)}</div></details>
    <nav className="director-kind-tabs reverse-page-tabs" aria-label="参考拆解页面"><button aria-pressed={referencePage === 'timeline'} onClick={() => setReferencePage('timeline')}>视频与逐段校订</button><button aria-pressed={referencePage === 'analysis'} onClick={() => setReferencePage('analysis')}>结构分析 <span>{readyArtifacts}/{artifactOrder.length}</span></button></nav>

    <div className="reverse-layout">
      <div className="reverse-main">
        <section className="reverse-source"><video src={mediaUrl(active.source.path)} controls preload="metadata" /><div><span>源素材</span><strong>{active.source.original_name}</strong><small>{active.source.has_audio ? '包含音轨' : '无音轨'} · 已生成 {active.timeline.length} 个分析段</small></div></section>
        <section className="reverse-timeline-section"><div className="reverse-section-head"><div><h2>镜头时间轴</h2><p>当前为抽样段，可逐段校订；实际剪辑点仍需确认。</p></div><span>{active.timeline.length} 段</span></div><div className="reverse-timeline">{active.timeline.map(segment => <TimelineCard key={`${active.id}:${segment.id}`} segment={segment} busy={busy} onSave={saveTimeline} />)}</div></section>
      </div>

      <aside className="reverse-inspector"><div className="reverse-inspector-head"><div><span className="director-kicker">结构化工件</span><h2>{readyArtifacts}/{artifactOrder.length} 项已有结果</h2></div></div><div className="reverse-artifact-list">{artifactOrder.map(id => { const artifact = active.artifacts[id]; if (!artifact) return null; const Icon = artifactIcons[id] ?? FileText; return <button key={id} className={selectedArtifact === id ? 'active' : ''} onClick={() => setSelectedArtifact(id)}><Icon size={15} /><span><strong>{artifact.title}</strong><small>{provenanceLabels[artifact.provenance] ?? '待归类'} · {artifact.user_status === 'reviewed' ? '已校订' : artifact.status === 'ready' ? '已有结果' : '待分析'}</small></span></button>; })}</div>{selected && <div className="reverse-artifact-editor"><div className="reverse-provenance"><span>{provenanceLabels[selected.provenance] ?? selected.provenance}</span>{selected.provenance !== 'user_confirmed' && selected.confidence != null && <span>模型置信度 {Math.round(selected.confidence * 100)}%</span>}</div><label htmlFor="reverse-artifact-summary">分析与人工校订</label><textarea id="reverse-artifact-summary" rows={7} value={artifactDraft} onChange={event => { artifactDirty.current = true; setArtifactDraft(event.target.value); }} /><p>{selectedArtifact === 'mechanisms' ? '“爆火原因”必须写成假设，并记录对应镜头、声音或文案证据。' : '模型输出不会覆盖人工版本；保存后标记为用户已校订。'}</p><button className="button secondary" disabled={busy || !artifactDraft.trim()} onClick={() => void saveArtifact()}>{saving ? <LoaderCircle className="spin" size={14} /> : <Check size={14} />}保存人工校订</button>{selected.evidence?.length ? <details><summary>查看依据</summary><ul>{selected.evidence.map((item, index) => <li key={index}>{item}</li>)}</ul></details> : null}{artifactDraft !== selected.summary && <details><summary>查看当前保存版本</summary><p>{selected.summary}</p></details>}{!!selected.review_history?.length && <details><summary>校订历史（{selected.review_history.length}）</summary>{[...selected.review_history].reverse().map((entry, index) => <article key={`${entry.revision ?? 'unknown'}-${index}`}><small>校订前版本 {entry.revision ?? '未知'} · {reviewTime(entry.recorded_at)} · {entry.user_status === 'reviewed' ? '已校订' : '待校订'} · {provenanceLabels[entry.provenance ?? ''] ?? entry.provenance ?? '来源未记录'}</small><p>{entry.summary ?? '无文本记录'}</p></article>)}</details>}{(selected.model_candidate || selected.original_candidate) && <details><summary>查看模型 / 原始候选</summary><p>{selected.model_candidate?.summary ?? selected.original_candidate?.summary}</p></details>}</div>}</aside>
    </div>
    {(notice || error) && <div className={`reverse-notice ${error ? 'error' : ''}`}>{error || notice}</div>}
  </section>;
}
