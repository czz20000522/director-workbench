import { useEffect, useMemo, useRef, useState } from 'react';
import { Activity, ArrowDownToLine, ArrowRight, Boxes, BrainCircuit, Check, Film, FileText, LoaderCircle, MapPinned, Mic2, RefreshCw, Search, Upload, Users } from 'lucide-react';
import TaskQueueStatus, { type QueueReceipt } from './TaskQueueStatus';
import { mediaUrl } from './api/client';

type Stage = { id: string; label: string; status: string; executor: string };
type TimelineSegment = { id: string; type?: string; start_seconds?: number; end_seconds?: number; frame?: string | null; frame_path?: string | null; frames?: { time_seconds: number; path: string }[]; status: string; facts?: string[]; inference?: string; model_candidate?: { observation: string; user_status: string }; user_note?: string; review_label?: string };
type ArtifactReviewHistory = { summary: string | null; user_status: string | null; status: string | null; provenance: string | null; confidence: number | null; revision: number | null; recorded_at: number | string | null };
type Artifact = { review_history?: ArtifactReviewHistory[]; title: string; status: string; provenance: string; summary: string; evidence?: string[]; confidence?: number | null; user_status: string; model_candidate?: { summary: string }; original_candidate?: { summary: string } };
type Analysis = {
  content_mode?: string;
  content_mode_reason?: string;
  content_mode_model_candidate?: { content_mode?: string; content_mode_reason?: string; user_status?: string; status?: string };
  content_mode_user_status?: string;
  revision?: number;
  id: string;
  title: string;
  status: string;
  created_at: string;
  source: { original_name: string; path: string; duration_seconds: number; width?: number; height?: number; fps?: number; has_audio?: boolean; has_video?: boolean; registered?: boolean };
  stages: Stage[];
  timeline: TimelineSegment[];
  observations?: Record<string, unknown>[];
  artifacts: Record<string, Artifact>;
  production_seed?: string;
};
type ModelStatus = {
  primary?: { label: string; ready: boolean; model_ready: boolean; projector_ready: boolean; model_size_bytes: number; projector_size_bytes: number };
  fast_optional?: { label: string; role: string; ready: boolean; model_size_bytes: number };
};

const artifactOrder = ['source', 'timeline', 'transcript', 'audio', 'story', 'shots', 'characters', 'scenes', 'actions', 'mechanisms', 'adaptation', 'production_seed'];
const artifactIcons: Record<string, typeof Film> = { source: Film, timeline: Activity, transcript: FileText, audio: Mic2, story: FileText, shots: Film, characters: Users, scenes: MapPinned, actions: Activity, mechanisms: BrainCircuit, adaptation: Boxes, production_seed: ArrowRight };
const provenanceLabels: Record<string, string> = { observable_fact: '可观测事实', asr_candidate: 'ASR 候选', model_inference: '模型候选', user_confirmed: '用户确认' };

async function responseError(response: Response) {
  const text = await response.text();
  try {
    const body = JSON.parse(text) as { detail?: unknown };
    if (typeof body.detail === 'string') return body.detail;
    if (body.detail && typeof body.detail === 'object' && 'message' in body.detail) return String(body.detail.message);
  } catch { /* A plain-text backend error is already suitable for display. */ }
  return text || `请求失败（${response.status}）`;
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
  const [note, setNote] = useState(segment.user_note || '');
  const [editing, setEditing] = useState(false);
  const frame = segment.frame || segment.frame_path || segment.frames?.[0]?.path;
  const label = segment.status === 'reviewed'
    ? '已人工校订'
    : segment.review_label === '模型候选/待校订' || segment.model_candidate
      ? '模型候选/待校订'
      : segment.review_label || '待确认范围';
  return <article>
    <div className="reverse-frame">{frame ? <img src={mediaUrl(frame)} alt={`${segment.id} 观察帧`} /> : <Film size={23} />}<b>{segment.id}</b></div>
    <strong>{clock(segment.start_seconds ?? 0)} - {clock(segment.end_seconds ?? 0)}</strong>
    <p>{segment.user_note || segment.model_candidate?.observation || segment.inference || '只读观察，等待人工校订。'}</p>
    <span>{label}</span>
    {editing ? <div className="reverse-timeline-editor"><label>这一段的观察与人工校订<textarea aria-label={`${segment.id} 人工校订`} rows={4} value={note} onChange={event => setNote(event.target.value)} /></label><button className="button secondary compact" disabled={busy || !note.trim()} onClick={async () => { if (await onSave(segment.id, note)) setEditing(false); }}>{busy ? '保存中…' : '保存校订'}</button><button className="text-button" disabled={busy} onClick={() => { setNote(segment.user_note || ''); setEditing(false); }}>取消</button></div> : <button className="button secondary compact" disabled={busy} onClick={() => { setNote(segment.user_note || segment.model_candidate?.observation || ''); setEditing(true); }}>校订这一观察</button>}
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
  const modelCandidate = analysis.content_mode_model_candidate;
  return <div className="reverse-classification">
    <span>内容类型 · {analysis.content_mode_user_status === 'reviewed' ? `${contentModes[analysis.content_mode || ''] || '已校订'} · 已校订` : modelCandidate ? `模型候选/待校订 · ${contentModes[modelCandidate.content_mode || ''] || '待判断'}` : '待判断'}</span>
    <button className="text-button" disabled={disabled || saving} onClick={() => editing ? setEditing(false) : begin()}>{editing ? '收起校订' : '校订类型'}</button>
    {editing && <div className="reverse-timeline-editor"><label>内容类型<select aria-label="内容类型" value={mode} disabled={saving} onChange={event => setMode(event.target.value)}>{Object.entries(contentModes).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label><label>判断理由<textarea aria-label="内容类型判断理由" rows={2} maxLength={2000} value={reason} disabled={saving} onChange={event => setReason(event.target.value)} /></label><button className="button secondary compact" disabled={disabled || saving || !reason.trim() || reason.length > 2000} onClick={() => void save()}>{saving ? '保存中…' : '保存内容类型'}</button></div>}
    {message && <p role="status" aria-live="polite">{message}</p>}
  </div>;
}

export default function ReverseWorkbench({ projectId, initialAnalysisId = '', onConverted }: { projectId: string; initialAnalysisId?: string; onConverted: (count: number) => void }) {
  const [referencePage, setReferencePage] = useState<'timeline' | 'analysis'>('timeline');
  const [analyses, setAnalyses] = useState<Analysis[]>([]);
  const [activeId, setActiveId] = useState(initialAnalysisId);
  const [modelStatus, setModelStatus] = useState<ModelStatus>({});
  const [uploading, setUploading] = useState(false);
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
  const selected = active?.artifacts?.[selectedArtifact];
  const [rangeStart, setRangeStart] = useState('0');
  const [rangeEnd, setRangeEnd] = useState('1');
  const [frameTime, setFrameTime] = useState('');
  const [sampleTimes, setSampleTimes] = useState('');
  const [shotPrompt, setShotPrompt] = useState('');
  const [shotPerformance, setShotPerformance] = useState('');
  const [shotDrafts, setShotDrafts] = useState<{ segment_id: string; start_seconds: number; end_seconds: number; fields: Record<string, string | null> }[]>([]);
  const [observing, setObserving] = useState(false);
  const artifactContext = useRef({ key: '', epoch: 0 });
  const artifactContextKey = JSON.stringify([projectId, active?.id, selectedArtifact]);
  if (artifactContext.current.key !== artifactContextKey) artifactContext.current = { key: artifactContextKey, epoch: artifactContext.current.epoch + 1 };
  const artifactDraftRef = useRef(artifactDraft);
  artifactDraftRef.current = artifactDraft;
  useEffect(() => { setSaving(false); }, [artifactContextKey]);
  useEffect(() => { setShotDrafts([]); }, [active?.id]);
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
      setNotice('参考素材已登记；只保存媒体事实，等待你显式选择范围、采样或 ASR。');
    } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setUploading(false); if (fileRef.current) fileRef.current.value = ''; }
  }

  async function explicitObservation(kind: 'range' | 'sample' | 'asr' | 'audio') {
    if (!active || observing) return;
    setObserving(true); setError('');
    try {
      let body: Record<string, unknown> = {};
      if (kind === 'range' || kind === 'audio') {
        const start = Number(rangeStart); const end = Number(rangeEnd);
        if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) throw new Error('请输入有效的显式起止时间。');
        body = { start_seconds: start, end_seconds: end };
        if (kind === 'range' && frameTime.trim()) {
          const frame = Number(frameTime);
          if (!Number.isFinite(frame) || frame < start || frame > end) throw new Error('帧时间必须位于显式参考时间段内。');
          body.frame_seconds = frame;
        }
      } else if (kind === 'sample') {
        const raw = sampleTimes.split(',').map(value => value.trim()).filter(Boolean);
        const times = raw.map(value => Number(value));
        if (!times.length || times.some(value => !Number.isFinite(value) || value < 0)) throw new Error('请输入有限的非负采样时间点。');
        body = { times_seconds: times };
      }
      const suffix = kind === 'range' ? 'range' : kind === 'audio' ? 'audio' : kind === 'sample' ? 'sample' : 'asr';
      const response = await fetch(`/api/projects/${encodeURIComponent(projectId)}/reverse-analyses/${encodeURIComponent(active.id)}/observations/${suffix}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      if (!response.ok) throw new Error(await responseError(response));
      const payload = await response.json() as { analysis: Analysis };
      setAnalyses(previous => previous.map(item => item.id === active.id ? payload.analysis : item));
      setNotice(kind === 'range' ? '已按明确时间范围提取观察；它仍需人工确认。' : kind === 'audio' ? '已按明确时间范围提取音频观察；它仍需人工确认。' : kind === 'sample' ? '已保存显式采样观察；等距采样/待确认范围不能直接转为分镜。' : '已保存模型 ASR 候选；请人工校订后再使用。');
    } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setObserving(false); }
  }

  function addShotDraft() {
    if (!active) return;
    const start = Number(rangeStart); const end = Number(rangeEnd);
    if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) { setError('制作分镜必须填写有效的参考起止时间。'); return; }
    if (!shotPrompt.trim() && !shotPerformance.trim()) { setError('制作分镜字段至少填写提示词或动作表演。'); return; }
    const id = `shot-${shotDrafts.length + 1}`;
    setShotDrafts(previous => [...previous, { segment_id: id, start_seconds: start, end_seconds: end, fields: { prompt: shotPrompt.trim() || null, performance: shotPerformance.trim() || null } }]);
    setNotice('已加入一条明确制作分镜；只有这些调用方字段会进入制作计划。');
    setShotPrompt(''); setShotPerformance('');
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
    if (!active || !shotDrafts.length) return;
    setConverting(true); setError('');
    try {
      const response = await fetch(`/api/projects/${encodeURIComponent(projectId)}/reverse-analyses/${encodeURIComponent(active.id)}/convert`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ expected_revision: active.revision ?? 0, shots: shotDrafts }) });
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
      setNotice(`${segmentId} 校订已保存；转为制作草案只使用你显式加入的分镜列表，观察不会自动写入制作字段。`);
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
  const busy = uploading || analyzing || converting || saving || timelineSaving || observing;

  if (loading) return <section className="rw-tool" role="status" aria-live="polite">
    <div className="rw-loading"><LoaderCircle className="spin" size={20} /><span>正在读取参考拆解…</span></div>
  </section>;

  if (!active) return <section className="rw-tool rw-empty">
    <div className="rw-empty-card">
      <div className="rw-empty-icon"><Search size={20} /></div>
      <div className="rw-empty-copy">
        <h2>从一段参考开始</h2>
        <p>导入音视频只登记媒体事实；范围、帧和 ASR 都由你显式调用。</p>
        <ul className="rw-facts"><li>保留原始参考</li><li>只保存媒体事实</li><li>观察与分镜分开</li></ul>
      </div>
      <div className="rw-empty-actions">
        <input ref={fileRef} type="file" accept="video/*,audio/*" hidden onChange={event => void uploadReference(event.target.files?.[0])} />
        <button className="button primary" disabled={uploading} onClick={() => fileRef.current?.click()}>{uploading ? <LoaderCircle className="spin" size={16} /> : <Upload size={16} />}{uploading ? '正在登记媒体…' : '导入参考素材'}</button>
      </div>
      {error && <p className="director-error rw-empty-error" role="alert">{error}</p>}
    </div>
  </section>;

  return <section className="reverse-workbench rw-tool" data-reference-page={referencePage}>
    <div className="rw-toolbar">
      <div className="rw-current">
        <span className="rw-current-label">当前参考</span>
        <strong title={active.title}>{active.title}</strong>
        <small>{active.source.original_name} · {active.source.width ?? '?'}×{active.source.height ?? '?'} · {Number(active.source.duration_seconds ?? 0).toFixed(1)} 秒 · 已登记媒体事实</small>
      </div>
      <div className="rw-toolbar-actions">
        <label className="rw-select">选择当前参考
          <select aria-label="选择参考拆解" disabled={busy} value={active.id} onChange={event => setActiveId(event.target.value)}>{analyses.map(item => <option key={item.id} value={item.id}>{item.title}</option>)}</select>
        </label>
        <input ref={fileRef} type="file" accept="video/*,audio/*" hidden onChange={event => void uploadReference(event.target.files?.[0])} />
        <button className="button secondary" disabled={busy} onClick={() => fileRef.current?.click()}><Upload size={15} />新增参考</button>
        {semanticJob?.status === 'needs_reconcile' && <button className="button secondary" disabled={busy} onClick={() => void reconcileAnalysis()}>核对分析结果</button>}
        <button className="button secondary" disabled={busy || semanticJob?.status === 'needs_reconcile' || !primary?.ready || !active.timeline.some(segment => segment.frame || segment.frame_path || segment.frames?.length)} onClick={() => void runSemanticAnalysis()}>{analyzing ? <LoaderCircle className="spin" size={15} /> : <BrainCircuit size={15} />}{semanticJob?.status === 'needs_reconcile' ? '分析任务待核对' : analyzing ? analysisLabel : active.stages.some(stage => stage.id === 'semantic' && stage.status === 'done') ? '重新分析内容' : '运行模型观察'}</button>
        <button className="button primary" disabled={busy || !shotDrafts.length} onClick={() => void convertToProduction()}>{converting ? <LoaderCircle className="spin" size={15} /> : <ArrowRight size={15} />}{'转为制作草案（需明确分镜）'}</button>
        <button className="button secondary" onClick={exportAnalysis}><ArrowDownToLine size={15} />导出拆解</button>
      </div>
    </div>

    {semanticJob && <div className="rw-job">
      <TaskQueueStatus task={{ ...semanticJob, asset_id: active.id }} />
      {semanticJob.task_id && analyzing && <button className="button secondary compact" disabled={startingAnalysis || ['stop_requested', 'stopping'].includes(semanticJob.status)} onClick={() => void stopSemanticAnalysis()}>停止内容分析</button>}
    </div>}

    <div className="rw-status-row">
      <ClassificationEditor key={`${projectId}:${active.id}`} analysis={active} projectId={projectId} disabled={busy} onSaved={analysis => setAnalyses(previous => previous.map(item => item.id === analysis.id ? analysis : item))} />
      <details className="reverse-model-details rw-models">
        <summary>分析能力与模型状态</summary>
        <div className="reverse-model-strip">
          <BrainCircuit size={16} />
          <div><strong>{primary?.label ?? 'Qwen3.8-27B Uncensored Q4_K_M'}</strong><span>主模型 · 仅在你显式运行时输出逐帧观察与结构化模型候选，需人工校订</span></div>
          <span className={primary?.ready ? 'ready' : 'waiting'}>{primary?.ready ? '模型文件齐备' : primary?.model_ready ? '缺少视觉投影' : '主模型缺失'}</span>
          <div className="reverse-model-divider" />
          <div><strong>{fast?.label ?? 'Qwen3-VL-8B-Instruct Q4_K_M'}</strong><span>可选快速预检，不是主流程必经步骤</span></div>
          <span className={fast?.ready ? 'ready' : 'waiting'}>{fast?.ready ? '快速模型可用' : '快速模型缺失'}</span>
          <button className="icon-button" aria-label="刷新模型状态" title="刷新模型状态" disabled={busy} onClick={() => void load().catch(reason => setError(String(reason)))}><RefreshCw size={15} /></button>
        </div>
      </details>
    </div>

    <section className="rw-card rw-observation" aria-label="显式观察工具">
      <div className="rw-card-head">
        <div><h2>显式观察工具</h2><p>上传只登记媒体事实。每次提取、采样、音频或 ASR 都必须由调用方明确触发，结果保存为只读观察。</p></div>
        <span className="rw-chip">结果只读 · 等待人工校订</span>
      </div>
      <div className="rw-field-grid">
        <label>开始时间（秒）<input type="number" min="0" step="0.1" value={rangeStart} onChange={event => setRangeStart(event.target.value)} /></label>
        <label>结束时间（秒）<input type="number" min="0" step="0.1" value={rangeEnd} onChange={event => setRangeEnd(event.target.value)} /></label>
        <label>帧时间（可选）<input type="number" min="0" step="0.1" value={frameTime} onChange={event => setFrameTime(event.target.value)} /></label>
      </div>
      <div className="rw-inline-actions">
        <button className="button secondary compact" disabled={busy} onClick={() => void explicitObservation('range')}>提取明确范围/帧</button>
        <button className="button secondary compact" disabled={busy || !active.source.has_audio} onClick={() => void explicitObservation('audio')}>提取明确范围音频</button>
      </div>
      <div className="rw-inline-actions">
        <label className="rw-inline-field">采样时间点（逗号分隔）<input value={sampleTimes} onChange={event => setSampleTimes(event.target.value)} placeholder="0, 2.5, 5" /></label>
        <button className="button secondary compact" disabled={busy} onClick={() => void explicitObservation('sample')}>显式采样</button>
        <button className="button secondary compact" disabled={busy || !active.source.has_audio} onClick={() => void explicitObservation('asr')}>运行 ASR（模型候选）</button>
      </div>
    </section>

    <section className="rw-card rw-shots" aria-label="明确制作分镜">
      <div className="rw-card-head">
        <div><h2>明确制作分镜</h2><p>等距采样/待确认范围和模型观察不能直接转为分镜。先填写参考时间段与制作字段，再加入非空列表。</p></div>
      </div>
      <div className="rw-field-grid">
        <label>分镜提示词<textarea rows={2} value={shotPrompt} onChange={event => setShotPrompt(event.target.value)} placeholder="调用方明确填写" /></label>
        <label>动作与表演<textarea rows={2} value={shotPerformance} onChange={event => setShotPerformance(event.target.value)} placeholder="调用方明确填写" /></label>
      </div>
      <div className="rw-shot-actions">
        <button className="button secondary compact" disabled={busy} onClick={addShotDraft}>加入制作分镜</button>
        {!shotDrafts.length && <span className="rw-muted">尚未提交明确制作分镜。</span>}
      </div>
      {!!shotDrafts.length && <ul className="rw-shot-list">
        {shotDrafts.map(shot => <li key={shot.segment_id}><span>{shot.segment_id} · {shot.start_seconds}s–{shot.end_seconds}s · {shot.fields.prompt || shot.fields.performance}</span><button className="text-button" onClick={() => setShotDrafts(previous => previous.filter(item => item.segment_id !== shot.segment_id))}>移除</button></li>)}
      </ul>}
    </section>

    {conversionConflict && <section role="alert" className="rw-alert">
      <p>参考已更新，本次未转换。重新读取后请先核对内容，再决定是否转为制作草案。</p>
      <button className="button secondary compact" disabled={busy} onClick={async () => { try { await load(); setConversionConflict(false); setError(''); setNotice('已读取最新参考；请核对后再转换。'); } catch (reason) { setError(String(reason)); } }}>读取最新参考</button>
    </section>}

    <p className="rw-hint">{active.production_seed ? '此参考已登记制作草案种子。再次转换只处理你显式加入的分镜，不会覆盖你在制作页的修改。' : '只有明确提交的分镜列表、参考时间段和字段会进入制作；观察与模型候选保持只读，不会自动写入。'}{!primary?.ready && ' 当前模型观察尚未就绪，可以继续使用显式媒体工具。'}</p>

    <details className="rw-details">
      <summary>查看拆解进度</summary>
      <div className="reverse-stage-rail" aria-label="参考拆解阶段">{active.stages.map((stage, index) => <div key={stage.id} className={`reverse-stage ${stage.status}`}><span>{stage.status === 'done' ? <Check size={13} /> : String(index + 1).padStart(2, '0')}</span><div><strong>{stage.label}</strong><small>{stage.status === 'done' ? '已完成' : stage.status === 'ready' ? '可以开始' : '待准备'}</small></div></div>)}</div>
    </details>

    <nav className="rw-tabs" aria-label="参考拆解页面">
      <button aria-pressed={referencePage === 'timeline'} onClick={() => setReferencePage('timeline')}>视频与逐段校订</button>
      <button aria-pressed={referencePage === 'analysis'} onClick={() => setReferencePage('analysis')}>观察与模型候选 <span>{readyArtifacts}/{artifactOrder.length}</span></button>
    </nav>

    <div className="rw-layout">
      <div className="rw-main">
        <section className="rw-card rw-source">
          <video src={mediaUrl(active.source.path)} controls preload="metadata" />
          <div className="rw-source-meta"><span>源素材</span><strong>{active.source.original_name}</strong><small>{active.source.has_audio ? '包含音轨' : '无音轨'} · 已生成 {active.timeline.length} 个分析段</small></div>
        </section>
        <section className="rw-card rw-timeline-section">
          <div className="rw-section-head"><div><h2>观察时间轴</h2><p>这里展示显式范围、采样和模型观察；它们不是自动镜头切点。</p></div><span>{active.timeline.length} 条观察</span></div>
          <div className="reverse-timeline">{active.timeline.map(segment => <TimelineCard key={`${active.id}:${segment.id}`} segment={segment} busy={busy} onSave={saveTimeline} />)}</div>
        </section>
      </div>

      <aside className="rw-card rw-inspector">
        <div className="rw-inspector-head"><div><span className="rw-eyebrow">观察与候选工件</span><h2>{readyArtifacts}/{artifactOrder.length} 项已有结果</h2></div></div>
        <div className="reverse-artifact-list">{artifactOrder.map(id => { const artifact = active.artifacts[id]; if (!artifact) return null; const Icon = artifactIcons[id] ?? FileText; return <button key={id} className={selectedArtifact === id ? 'active' : ''} onClick={() => setSelectedArtifact(id)}><Icon size={15} /><span><strong>{artifact.title}</strong><small>{provenanceLabels[artifact.provenance] ?? '待归类'} · {artifact.user_status === 'reviewed' ? '已校订' : artifact.status === 'ready' ? '已有结果' : '待分析'}</small></span></button>; })}</div>{selected && <div className="reverse-artifact-editor"><div className="reverse-provenance"><span>{provenanceLabels[selected.provenance] ?? selected.provenance}</span>{selected.provenance !== 'user_confirmed' && selected.confidence != null && <span>模型置信度 {Math.round(selected.confidence * 100)}%</span>}</div><label htmlFor="reverse-artifact-summary">模型候选与人工校订</label><textarea id="reverse-artifact-summary" rows={7} value={artifactDraft} onChange={event => { artifactDirty.current = true; setArtifactDraft(event.target.value); }} /><p>{selectedArtifact === 'mechanisms' ? '“爆火原因”必须写成假设，并记录对应镜头、声音或文案证据。' : '模型输出不会覆盖人工版本；保存后标记为用户已校订。'}</p><button className="button secondary" disabled={busy || !artifactDraft.trim()} onClick={() => void saveArtifact()}>{saving ? <LoaderCircle className="spin" size={14} /> : <Check size={14} />}保存人工校订</button>{selected.evidence?.length ? <details><summary>查看依据</summary><ul>{selected.evidence.map((item, index) => <li key={index}>{item}</li>)}</ul></details> : null}{artifactDraft !== selected.summary && <details><summary>查看当前保存版本</summary><p>{selected.summary}</p></details>}{!!selected.review_history?.length && <details><summary>校订历史（{selected.review_history.length}）</summary>{[...selected.review_history].reverse().map((entry, index) => <article key={`${entry.revision ?? 'unknown'}-${index}`}><small>校订前版本 {entry.revision ?? '未知'} · {reviewTime(entry.recorded_at)} · {entry.user_status === 'reviewed' ? '已校订' : '待校订'} · {provenanceLabels[entry.provenance ?? ''] ?? entry.provenance ?? '来源未记录'}</small><p>{entry.summary ?? '无文本记录'}</p></article>)}</details>}{(selected.model_candidate || selected.original_candidate) && <details><summary>查看模型 / 原始候选</summary><p>{selected.model_candidate?.summary ?? selected.original_candidate?.summary}</p></details>}</div>}
      </aside>
    </div>

    {(notice || error) && <div className={`rw-notice ${error ? 'error' : ''}`} role={error ? 'alert' : 'status'} aria-live="polite">{error || notice}</div>}
  </section>;
}
