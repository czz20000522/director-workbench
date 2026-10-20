import { useEffect, useId, useRef, useState } from 'react';
import TaskQueueStatus, { type QueueReceipt } from './TaskQueueStatus';
import { ApiError, mediaUrl, request as apiRequest } from './api/client';

type Props = { projectId: string; audioAssets: Array<{ id: string; title: string }>; onComplete: () => void; onBrowse: () => void; defaultOpen?: boolean };
type Draft = { source_asset_id: string; background_asset_id?: string; start_seconds: number; duration_seconds: number | null; pad_silence: boolean;
  gain: number; fade_in_seconds: number; fade_out_seconds: number; background_gain: number; background_offset_seconds: number };
type Submission = Draft & { idempotency_key: string };
type Saved = { draft: Draft; pending?: Submission; completedTaskId?: string };
type Task = { id: string; status: string; queue?: QueueReceipt['queue']; error?: string | null; result?: unknown };
type MasterDraft = { source_asset_id: string; background_asset_id?: string; target_lufs: number; true_peak_dbtp: number;
  fade_in_seconds: number; fade_out_seconds: number; trim_start_seconds: number; trim_duration_seconds: number | null;
  background_gain_db: number; pad_silence: boolean; ducking?: { threshold_db: number; ratio: number; attack_seconds: number; release_seconds: number } };
type MasterSubmission = MasterDraft & { idempotency_key: string };
type SavedMaster = { draft: MasterDraft; pending?: MasterSubmission; completedTaskId?: string };
const masterInitial: MasterDraft = { source_asset_id: '', target_lufs: -16, true_peak_dbtp: -1, fade_in_seconds: 0, fade_out_seconds: 0,
  trim_start_seconds: 0, trim_duration_seconds: null, background_gain_db: -12, pad_silence: false };
const initial: Draft = { source_asset_id: '', start_seconds: 0, duration_seconds: null, pad_silence: false, gain: 1, fade_in_seconds: 0,
  fade_out_seconds: 0, background_gain: .25, background_offset_seconds: 0 };
const terminal = ['succeeded', 'failed', 'stopped'];
const pollTerminal = [...terminal, 'needs_reconcile'];
const labels: Record<string, string> = { scheduler_waiting: '已接受 · 等待资源', batch_waiting: '已接受 · 等待依赖', submitting: '正在处理', queued: '等待处理', running: '正在处理', succeeded: '已完成 · 待试听',
  failed: '处理失败', stopped: '已停止', needs_reconcile: '需要核对结果', stop_requested: '等待处理结束', stopping: '等待处理结束' };
function valid(d: Draft): boolean {
  const finite = (v: unknown, max: number) => typeof v === 'number' && Number.isFinite(v) && v >= 0 && v <= max;
  return typeof d?.source_asset_id === 'string' && Boolean(d.source_asset_id)
    && (d.background_asset_id === undefined || typeof d.background_asset_id === 'string')
    && (d.pad_silence === undefined || typeof d.pad_silence === 'boolean') && finite(d.start_seconds, 86400) && (d.duration_seconds === null || (finite(d.duration_seconds, 120) && d.duration_seconds > 0))
    && finite(d.gain, 8) && finite(d.background_gain, 8) && finite(d.fade_in_seconds, 120) && finite(d.fade_out_seconds, 120)
    && finite(d.background_offset_seconds, 120) && (Boolean(d.background_asset_id) || d.background_offset_seconds === 0)
    && (d.duration_seconds === null || (d.fade_in_seconds + d.fade_out_seconds <= d.duration_seconds && d.background_offset_seconds < d.duration_seconds));
}
function validMaster(d: MasterDraft): boolean {
  const finite = (v: unknown, low: number, high: number) => typeof v === 'number' && Number.isFinite(v) && v >= low && v <= high;
  const ducking = d.ducking;
  return Boolean(d.source_asset_id) && (d.background_asset_id === undefined || typeof d.background_asset_id === 'string')
    && finite(d.target_lufs, -24, -8) && finite(d.true_peak_dbtp, -3, 0) && finite(d.fade_in_seconds, 0, 10)
    && finite(d.fade_out_seconds, 0, 10) && finite(d.trim_start_seconds, 0, 3600)
    && (d.trim_duration_seconds === null || finite(d.trim_duration_seconds, .001, 1800))
    && finite(d.background_gain_db, -60, 12) && typeof d.pad_silence === 'boolean'
    && (!ducking || (finite(ducking.threshold_db, -60, 0) && finite(ducking.ratio, 1, 20)
      && finite(ducking.attack_seconds, .001, 1) && finite(ducking.release_seconds, .01, 2)));
}
class RequestError extends Error { constructor(message: string, readonly status: number) { super(message); } }

// Keyed inner state prevents a pending request from following a project switch.
export default function AudioEditPanel(props: Props) { return <ProjectAudioEditPanel key={props.projectId} {...props} />; }

function ProjectAudioEditPanel({ projectId, audioAssets, onComplete, onBrowse, defaultOpen }: Props) {
  // The shell now renders every post tool expanded; keep accepting the legacy
  // prop without letting it collapse the form again.
  void defaultOpen;
  const storageKey = `director-workbench:${projectId}:audio-edit:v1`;
  const prefix = useId();
  const [saved, setSaved] = useState<Saved>(() => {
    try {
      const value = JSON.parse(localStorage.getItem(storageKey) || '{}');
      const draft = value.draft && typeof value.draft === 'object' ? { ...initial, ...value.draft } : { ...initial };
      const pending = value.pending && typeof value.pending.idempotency_key === 'string' ? value.pending : undefined;
      return { draft, pending, completedTaskId: typeof value.completedTaskId === 'string' ? value.completedTaskId : undefined };
    } catch { return { draft: { ...initial } }; }
  });
  const current = useRef(saved);
  const alive = useRef(true);
  const working = useRef(false);
  const completion = useRef(onComplete);
  completion.current = onComplete;
  const [task, setTask] = useState<Task | null>(null);
  const masterStorageKey = `director-workbench:${projectId}:audio-master:v1`;
  const [masterSaved, setMasterSaved] = useState<SavedMaster>(() => {
    try {
      const value = JSON.parse(localStorage.getItem(masterStorageKey) || '{}');
      const draft = value.draft && typeof value.draft === 'object' ? { ...masterInitial, ...value.draft } : { ...masterInitial };
      const pending = value.pending && typeof value.pending.idempotency_key === 'string' ? value.pending : undefined;
      return { draft, pending, completedTaskId: typeof value.completedTaskId === 'string' ? value.completedTaskId : undefined };
    } catch { return { draft: { ...masterInitial } }; }
  });
  const masterCurrent = useRef(masterSaved);
  const masterDraft = masterSaved.pending || masterSaved.draft;
  const masterPendingKey = masterSaved.pending?.idempotency_key;
  const [masterTask, setMasterTask] = useState<Task | null>(null);
  const [masterBusy, setMasterBusy] = useState(false);
  const [masterMessage, setMasterMessage] = useState('');
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const base = `/api/projects/${encodeURIComponent(projectId)}`;
  const pendingKey = saved.pending?.idempotency_key;
  const draft = saved.pending || saved.draft;
  const locked = Boolean(saved.pending);
  const result = task?.status === 'succeeded' && task.result && typeof task.result === 'object' ? task.result as { output?: string; duration_seconds?: number; report?: { peak?: number; clipped_samples?: number } } : null;
  const masterResult = masterTask?.status === 'succeeded' && masterTask.result && typeof masterTask.result === 'object' ? masterTask.result as { output?: string; duration_seconds?: number; candidate_asset_id?: string; report?: { output_true_peak_dbtp?: number; loudness_lufs_i?: number; loudness_lra?: number; clipped_samples?: number } } : null;
  function persist(next: Saved) {
    if (!alive.current) return false;
    try { localStorage.setItem(storageKey, JSON.stringify(next)); current.current = next; setSaved(next); return true; }
    catch { setMessage('无法保存提交凭据，请恢复浏览器存储后重试。'); return false; }
  }
  function persistMaster(next: SavedMaster) {
    if (!alive.current) return false;
    try { localStorage.setItem(masterStorageKey, JSON.stringify(next)); masterCurrent.current = next; setMasterSaved(next); return true; }
    catch { setMasterMessage('无法保存全片母版提交凭据，请恢复浏览器存储后重试。'); return false; }
  }
  function accept(next: Task, key: string) {
    if (!alive.current || current.current.pending?.idempotency_key !== key) return;
    setTask(next); setMessage('');
    if (next.status === 'succeeded' && current.current.completedTaskId !== next.id) {
      if (persist({ ...current.current, completedTaskId: next.id })) completion.current();
    }
  }
  async function request(url: string, init?: RequestInit): Promise<Task> {
    let data: Task;
    try {
      data = await apiRequest<Task>(url, { init });
    } catch (cause) {
      if (cause instanceof ApiError) throw new RequestError(cause.message, cause.status);
      throw cause;
    }
    if (typeof data.id !== 'string' || typeof data.status !== 'string') throw new Error('任务回执不完整，请核对原提交。');
    return data;
  }
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  useEffect(() => {
    if (!pendingKey) return;
    let active = true; let polling = false;
    async function poll() {
      if (polling) return;
      polling = true;
      try {
        const result = await request(task ? `${base}/tasks/${encodeURIComponent(task.id)}` : `${base}/submission-receipt?key=${encodeURIComponent(pendingKey!)}`);
        if (active) accept(result, pendingKey!);
      } catch (error) {
        if (active && !working.current && current.current.pending?.idempotency_key === pendingKey) setMessage(error instanceof Error ? error.message : '暂时无法核对任务');
      } finally { polling = false; }
    }
    void poll();
    const timer = window.setInterval(() => { if (!task || !pollTerminal.includes(task.status)) void poll(); }, 3000);
    return () => { active = false; window.clearInterval(timer); };
  }, [pendingKey, task?.id, task?.status, base]);
  useEffect(() => {
    if (!masterPendingKey) return;
    let active = true; let polling = false;
    async function pollMaster() {
      if (polling) return;
      polling = true;
      try {
        const result = await request(`${base}/submission-receipt?key=${encodeURIComponent(masterPendingKey!)}`);
        if (active) {
          setMasterTask(result);
          if (result.status === 'succeeded' && masterCurrent.current.completedTaskId !== result.id) {
            if (persistMaster({ ...masterCurrent.current, completedTaskId: result.id })) completion.current();
          }
        }
      } catch (error) {
        if (active && !masterBusy && masterCurrent.current.pending?.idempotency_key === masterPendingKey) setMasterMessage(error instanceof Error ? error.message : '暂时无法核对全片母版');
      } finally { polling = false; }
    }
    void pollMaster();
    const timer = window.setInterval(() => { if (!masterTask || !pollTerminal.includes(masterTask.status)) void pollMaster(); }, 3000);
    return () => { active = false; window.clearInterval(timer); };
  }, [masterPendingKey, masterTask?.id, masterTask?.status, base]);
  useEffect(() => {
    if (!masterTask || pollTerminal.includes(masterTask.status)) return;
    let active = true;
    const poll = async () => {
      try { const next = await request(`${base}/tasks/${encodeURIComponent(masterTask.id)}`); if (active) setMasterTask(next); }
      catch (error) { if (active) setMasterMessage(error instanceof Error ? error.message : '暂时无法核对全片母版后期'); }
    };
    void poll(); const timer = window.setInterval(() => void poll(), 3000);
    return () => { active = false; window.clearInterval(timer); };
  }, [masterTask?.id, masterTask?.status, base]);
  async function act(action: 'submit' | 'check' | 'reconcile' | 'resolve-missing' | 'stop' | 'resume') {
    if (working.current) return;
    working.current = true; setBusy(true);
    let key = current.current.pending?.idempotency_key;
    try {
      let next: Task;
      if (action === 'submit') {
        const submission = current.current.pending || { ...current.current.draft, idempotency_key: `audio-edit-${crypto.randomUUID()}` };
        if (!valid(submission)) throw new Error('请检查素材和编辑参数；已有提交请先核对。');
        if (!persist({ ...current.current, pending: submission })) return;
        key = submission.idempotency_key;
        next = await request(`${base}/audio-edit-tasks`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(submission) });
      } else if (action === 'check') {
        next = await request(`${base}/submission-receipt?key=${encodeURIComponent(key!)}`);
      } else if (action === 'resolve-missing') {
        const note = window.prompt('请填写已确认原执行结束并检查输出的说明：')?.trim();
        if (!note) return;
        next = await request(`${base}/tasks/${encodeURIComponent(task!.id)}/resolve-missing`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ confirm_execution_ended: true, note }),
        });
      } else {
        next = await request(`${base}/tasks/${encodeURIComponent(task!.id)}/${action}`, { method: 'POST' });
      }
      accept(next, key!);
    } catch (error) {
      if (alive.current && (!key || current.current.pending?.idempotency_key === key)) {
        if (action === 'submit' && error instanceof RequestError && error.status === 422) persist({ ...current.current, pending: undefined });
        setMessage(error instanceof Error ? error.message : '请求未获确认，请核对原任务。');
      }
    } finally { working.current = false; if (alive.current) setBusy(false); }
  }
  function change<K extends keyof Draft>(name: K, value: Draft[K]) { persist({ ...current.current, draft: { ...current.current.draft, [name]: value } }); }
  function changeMaster<K extends keyof MasterDraft>(name: K, value: MasterDraft[K]) {
    if (masterCurrent.current.pending) return;
    persistMaster({ ...masterCurrent.current, draft: { ...masterCurrent.current.draft, [name]: value } });
  }
  async function submitMaster() {
    if (masterBusy || masterCurrent.current.pending || !validMaster(masterDraft) || !audioAssets.some(asset => asset.id === masterDraft.source_asset_id)) return;
    setMasterBusy(true); setMasterMessage('');
    const key = `audio-master-${crypto.randomUUID()}`;
    const submission = { ...masterCurrent.current.draft, idempotency_key: key };
    if (!persistMaster({ ...masterCurrent.current, pending: submission })) { setMasterBusy(false); return; }
    try {
      const next = await request(`${base}/audio-master-tasks`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(submission) });
      setMasterTask(next);
      if (next.status === 'succeeded' && masterCurrent.current.completedTaskId !== next.id) {
        persistMaster({ ...masterCurrent.current, completedTaskId: next.id }); completion.current();
      }
    } catch (error) {
      if (error instanceof RequestError && error.status === 422) persistMaster({ ...masterCurrent.current, pending: undefined });
      setMasterMessage(error instanceof Error ? error.message : '全片母版后期提交失败');
    }
    finally { setMasterBusy(false); }
  }
  async function masterAct(action: 'reconcile' | 'resolve-missing') {
    if (masterBusy || !masterTask?.id || masterTask.status !== 'needs_reconcile') return;
    let note: string | undefined;
    if (action === 'resolve-missing') {
      note = window.prompt('请填写已确认原执行结束并检查输出的说明：')?.trim();
      if (!note) return;
    }
    setMasterBusy(true); setMasterMessage('');
    try {
      const init: RequestInit = { method: 'POST' };
      if (note) {
        init.headers = { 'Content-Type': 'application/json' };
        init.body = JSON.stringify({ confirm_execution_ended: true, note });
      }
      const next = await request(`${base}/tasks/${encodeURIComponent(masterTask.id)}/${action}`, init);
      setMasterTask(next);
      if (terminal.includes(next.status)) {
        persistMaster({ ...masterCurrent.current, pending: undefined });
        if (next.status === 'succeeded') completion.current();
      }
    } catch (error) {
      setMasterMessage(error instanceof Error ? error.message : '暂时无法核对全片母版后期');
    } finally { setMasterBusy(false); }
  }
  async function chooseMaster() {
    if (!masterResult?.candidate_asset_id || masterBusy) return;
    setMasterBusy(true); setMasterMessage('');
    try {
      const edit = await apiRequest<{ revision: number }>(`${base}/assembly/edit`);
      await apiRequest(`${base}/assembly/sound`, { init: { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ expected_revision: edit.revision, policy: 'complete_master', audio_asset_id: masterResult.candidate_asset_id }) } });
      setMasterMessage('已选择为装配完整母版，请继续进行装配预检。'); completion.current();
    } catch (error) { setMasterMessage(error instanceof Error ? error.message : '选择完整母版失败，请刷新后重试'); }
    finally { setMasterBusy(false); }
  }
  const numberInput = (name: Exclude<keyof Draft, 'source_asset_id' | 'background_asset_id' | 'pad_silence'>, label: string, max: number) =>
    <div key={name} className="sf-post-field"><label htmlFor={`${prefix}-${name}`}>{label}</label><input id={`${prefix}-${name}`} type="number" min={0} max={max} step="any" disabled={locked} value={draft[name] ?? ''}
      placeholder={name === 'duration_seconds' ? '留空：到结尾' : undefined}
      onChange={event => change(name, event.target.value === '' && name === 'duration_seconds' ? null : Number(event.target.value))} /></div>;
  return <section className="sf-post-tools" aria-label="编辑音频">
    <section className="sf-post-section" aria-label="全片母版后期">
      <div className="sf-post-section-head"><h2 className="sf-post-title">全片母版后期</h2><p className="sf-post-meta">最长 30 分钟 · CPU 响度处理</p></div>
      <p className="sf-post-hint">对当前作品已登记的完整母版做 CPU 响度归一化、真峰值限制、淡入淡出和可选背景混音。支持 WAV、MP3、M4A、FLAC；WAV 保留原采样率和声道，其他格式由 FFmpeg 归一化为 48kHz 双声道 PCM16。输出为新候选，不会自动替换音轨。</p>
      <div className="sf-post-fields">
        <div className="sf-post-field"><label htmlFor={`${prefix}-master-source`}>完整母版</label><select id={`${prefix}-master-source`} value={masterDraft.source_asset_id} disabled={Boolean(masterTask) || Boolean(masterPendingKey)} onChange={event => changeMaster('source_asset_id', event.target.value)}>
          <option value="">选择音频</option>{audioAssets.map(asset => <option key={asset.id} value={asset.id}>{asset.title}</option>)}</select></div>
        <div className="sf-post-field"><label htmlFor={`${prefix}-master-background`}>背景音（可选）</label><select id={`${prefix}-master-background`} value={masterDraft.background_asset_id || ''} disabled={Boolean(masterTask) || Boolean(masterPendingKey)} onChange={event => changeMaster('background_asset_id', event.target.value || undefined)}>
          <option value="">不混入背景音</option>{audioAssets.map(asset => <option key={asset.id} value={asset.id}>{asset.title}</option>)}</select></div>
        <label className="sf-post-check sf-post-field--wide"><input type="checkbox" checked={masterDraft.pad_silence} disabled={Boolean(masterTask) || Boolean(masterPendingKey)} onChange={event => changeMaster('pad_silence', event.target.checked)} />明确允许源或背景尾部补静音</label>
        <label className="sf-post-field">目标响度（LUFS-I）<input type="number" min={-24} max={-8} step="any" disabled={Boolean(masterTask) || Boolean(masterPendingKey)} value={masterDraft.target_lufs} onChange={event => changeMaster('target_lufs', Number(event.target.value))} /></label>
        <label className="sf-post-field">真峰值（dBTP）<input type="number" min={-3} max={0} step="any" disabled={Boolean(masterTask) || Boolean(masterPendingKey)} value={masterDraft.true_peak_dbtp} onChange={event => changeMaster('true_peak_dbtp', Number(event.target.value))} /></label>
        <label className="sf-post-field">裁切起点（秒）<input type="number" min={0} max={3600} step="any" disabled={Boolean(masterTask) || Boolean(masterPendingKey)} value={masterDraft.trim_start_seconds} onChange={event => changeMaster('trim_start_seconds', Number(event.target.value))} /></label>
        <label className="sf-post-field">裁切时长（秒）<input type="number" min={0.001} max={1800} step="any" placeholder="留空：到结尾" disabled={Boolean(masterTask) || Boolean(masterPendingKey)} value={masterDraft.trim_duration_seconds ?? ''} onChange={event => changeMaster('trim_duration_seconds', event.target.value === '' ? null : Number(event.target.value))} /></label>
        <label className="sf-post-check sf-post-field--wide"><input type="checkbox" checked={Boolean(masterDraft.ducking)} disabled={Boolean(masterTask) || Boolean(masterPendingKey)} onChange={event => changeMaster('ducking', event.target.checked ? { threshold_db: -24, ratio: 4, attack_seconds: .01, release_seconds: .2 } : undefined)} />背景 ducking（带 attack/release 包络）</label>
      </div>
      {masterDraft.ducking && <div className="sf-post-subblock"><h3 className="sf-post-subtitle">Ducking 参数</h3><div className="sf-post-fields">
        <label className="sf-post-field">阈值（dB）<input type="number" min={-60} max={0} step="any" disabled={Boolean(masterTask) || Boolean(masterPendingKey)} value={masterDraft.ducking.threshold_db} onChange={event => changeMaster('ducking', { ...masterDraft.ducking!, threshold_db: Number(event.target.value) })} /></label>
        <label className="sf-post-field">压缩比<input type="number" min={1} max={20} step="any" disabled={Boolean(masterTask) || Boolean(masterPendingKey)} value={masterDraft.ducking.ratio} onChange={event => changeMaster('ducking', { ...masterDraft.ducking!, ratio: Number(event.target.value) })} /></label>
        <label className="sf-post-field">attack（秒）<input type="number" min={.001} max={1} step="any" disabled={Boolean(masterTask) || Boolean(masterPendingKey)} value={masterDraft.ducking.attack_seconds} onChange={event => changeMaster('ducking', { ...masterDraft.ducking!, attack_seconds: Number(event.target.value) })} /></label>
        <label className="sf-post-field">release（秒）<input type="number" min={.01} max={2} step="any" disabled={Boolean(masterTask) || Boolean(masterPendingKey)} value={masterDraft.ducking.release_seconds} onChange={event => changeMaster('ducking', { ...masterDraft.ducking!, release_seconds: Number(event.target.value) })} /></label>
      </div></div>}
      {masterMessage && <p className="sf-post-message" role="status" aria-live="polite">{masterMessage}</p>}{masterTask?.error && <p className="sf-post-alert" role="alert">{masterTask.error}</p>}
      {masterResult?.output && <div className="sf-post-subblock"><audio controls preload="metadata" aria-label="全片母版后期候选试听" src={mediaUrl(masterResult.output)} /><small>实测 {masterResult.duration_seconds} 秒 · {masterResult.report?.loudness_lufs_i} LUFS-I · {masterResult.report?.output_true_peak_dbtp} dBTP · LRA {masterResult.report?.loudness_lra} · 削波样本 {masterResult.report?.clipped_samples}</small></div>}
      <div className="sf-post-actions"><button className="button primary compact" type="button" disabled={masterBusy || Boolean(masterTask) || Boolean(masterPendingKey) || !validMaster(masterDraft)} onClick={() => void submitMaster()}>提交全片后期</button>
        {masterResult?.candidate_asset_id && <button className="button secondary compact" type="button" disabled={masterBusy} onClick={() => void chooseMaster()}>选择为完整母版</button>}
        {masterTask?.status === 'needs_reconcile' && <><button className="button secondary compact" type="button" disabled={masterBusy} onClick={() => void masterAct('reconcile')}>核对母版结果</button><button className="button secondary compact" type="button" disabled={masterBusy} onClick={() => void masterAct('resolve-missing')}>确认执行已结束</button></>}
        {masterTask && terminal.includes(masterTask.status) && <button className="button secondary compact" type="button" disabled={masterBusy} onClick={() => { if (persistMaster({ ...masterCurrent.current, pending: undefined })) setMasterTask(null); }}>继续编辑</button>}
        {masterTask && <span className="sf-post-status" role="status" aria-live="polite">{labels[masterTask.status] || '处理中'}</span>}</div>
    </section>
    <section className="sf-post-section" aria-label="音频编辑">
      <div className="sf-post-section-head"><h2 className="sf-post-title">音频编辑</h2><p className="sf-post-meta">最长 120 秒 · 生成新候选</p></div>
      <p className="sf-post-hint">PCM16 WAV 或 MP3，MP3 转为 48kHz 双声道。增益和淡变产生新候选，不自动替换母版或采用。短源默认拒绝，可明确补静音；长源用取片时长裁切。完整母版应包含需要保留的对白和音效。</p>
      <div className="sf-post-fields">
        <div className="sf-post-field"><label htmlFor={`${prefix}-source`}>原音频</label><select id={`${prefix}-source`} value={draft.source_asset_id} disabled={locked} onChange={event => change('source_asset_id', event.target.value)}>
          <option value="">选择音频</option>{draft.source_asset_id && !audioAssets.some(a => a.id === draft.source_asset_id) && <option value={draft.source_asset_id}>原提交素材（当前不可选）</option>}
          {audioAssets.map(asset => <option key={asset.id} value={asset.id}>{asset.title}</option>)}</select></div>
        <div className="sf-post-field"><label htmlFor={`${prefix}-background`}>背景音（可选）</label><select id={`${prefix}-background`} value={draft.background_asset_id || ''} disabled={locked} onChange={event => persist({ ...current.current, draft: { ...current.current.draft, background_asset_id: event.target.value || undefined, background_offset_seconds: 0 } })}>
          <option value="">不混入背景音</option>{draft.background_asset_id && !audioAssets.some(a => a.id === draft.background_asset_id) && <option value={draft.background_asset_id}>原提交背景音（当前不可选）</option>}
          {audioAssets.map(asset => <option key={asset.id} value={asset.id}>{asset.title}</option>)}</select></div>
        <label className="sf-post-check sf-post-field--wide"><input type="checkbox" checked={draft.pad_silence ?? false} disabled={locked} onChange={event => change('pad_silence', event.target.checked)} />源不足所选时长时，明确在尾部补静音</label>
      </div>
      <div className="sf-post-subblock"><h3 className="sf-post-subtitle">裁切、音量与淡变</h3><div className="sf-post-fields">
        {numberInput('start_seconds', '起点（秒）', 86400)}{numberInput('duration_seconds', '保留时长（秒）', 120)}
        {numberInput('gain', '原音频音量（倍）', 8)}{numberInput('fade_in_seconds', '淡入（秒）', 120)}{numberInput('fade_out_seconds', '淡出（秒）', 120)}
        {draft.background_asset_id && <>{numberInput('background_gain', '背景音音量（倍）', 8)}{numberInput('background_offset_seconds', '背景音开始位置（秒）', 120)}</>}
      </div></div>
      <p className="sf-post-hint">PCM16 WAV，最多 120 秒；混音须同采样率、同声道。保存为新候选，原音频保留。</p>
      {message && <p className="sf-post-message" role="status" aria-live="polite">{message}</p>}{task?.error && <p className="sf-post-message" role="status" aria-live="polite">{task.error}</p>}
      {result?.output && <div className="sf-post-subblock"><audio controls preload="metadata" aria-label="音频编辑候选试听" src={mediaUrl(result.output)} /><p className="sf-post-hint">新候选实测 {result.duration_seconds ?? '未知'} 秒；试听后再选择为完整母版，不会自动替换原音。</p></div>}
      <div className="sf-post-actions">
        <button className="button secondary compact" onClick={onBrowse}>添加素材</button>
        {!locked && <button className="button primary compact" disabled={busy || !valid(draft) || !audioAssets.some(a => a.id === draft.source_asset_id)} onClick={() => void act('submit')}>保存编辑候选</button>}
        {locked && !task && <><button className="button secondary compact" disabled={busy} onClick={() => void act('check')}>核对原提交</button><button className="button secondary compact" disabled={busy} onClick={() => void act('submit')}>使用原凭据重试</button></>}
        {task?.status === 'needs_reconcile' && <><button className="button secondary compact" disabled={busy} onClick={() => void act('reconcile')}>核对编辑结果</button><button className="button secondary compact" disabled={busy} onClick={() => void act('resolve-missing')}>确认执行已结束</button></>}
        {task && !terminal.includes(task.status) && task.status !== 'needs_reconcile' && <button className="button secondary compact" disabled={busy} onClick={() => void act('stop')}>停止音频编辑</button>}
        {task && ['stopped', 'failed'].includes(task.status) && <button className="button secondary compact" disabled={busy} onClick={() => void act('resume')}>恢复原编辑任务</button>}
        {task && terminal.includes(task.status) && <button className="button secondary compact" disabled={busy} onClick={() => { if (persist({ ...current.current, pending: undefined })) { setTask(null); setMessage(''); } }}>继续编辑</button>}
        {task && <TaskQueueStatus task={{ ...task, asset_id: '音频编辑' }} />}
        {task && <span className="sf-post-status" role="status" aria-live="polite">{labels[task.status] || '处理中'}</span>}
      </div>
    </section>
  </section>;
}
