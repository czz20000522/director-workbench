import { useEffect, useRef, useState } from 'react';
import TaskQueueStatus from './TaskQueueStatus';
import type { MaterialOption } from './MaterialSelect';
import { ApiError, mediaUrl, request as apiRequest } from './api/client';
import './enhancement.css';

type Facts = { width?: number; height?: number; duration_seconds?: number; frames?: number; fps?: number; audio_streams?: number };
type Recipe = { source: string; source_facts: Facts & { kind: 'image' | 'video' }; model: string; scale: number };
type Task = { id: string; asset_id: string; status: string; error?: string; created_at?: number; queue?: { state: string; position?: number | null; reason?: { code: string; message?: string } | null }; payload?: { enhancement?: Recipe }; result?: { published_outputs?: string[]; outputs?: string[]; output_facts?: Facts; comfy_timing?: { execution_seconds?: number } } };
type Pending = { source: string; idempotency_key: string };
type Capability = { model: string; limits: string; kinds: Record<'image' | 'video', { available: boolean; missing: string[] }> };
const terminal = ['succeeded', 'failed', 'stopped'];
const statusLabel: Record<string, string> = { succeeded: '已完成 · 待比较', failed: '处理失败', stopped: '已停止', scheduler_waiting: '等待资源', preparing: '准备中', submitting: '正在提交', queued: '引擎排队中', running: '正在处理', stop_requested: '正在停止', stopping: '正在停止', needs_reconcile: '需要核对回执' };
const dimensions = (facts?: Facts) => facts?.width && facts.height ? `${facts.width} × ${facts.height}` : '尺寸待核验';
class RequestError extends Error {
  constructor(message: string, readonly status: number, readonly code?: string) { super(message); }
}

export default function EnhancementPanel({ projectId, initialSource = '', onBack }: { projectId: string; initialSource?: string; onBack: () => void }) {
  const base = `/api/projects/${encodeURIComponent(projectId)}`;
  const key = `director-workbench:${projectId}:enhancement:v1`;
  const [initial] = useState(() => {
    try { return JSON.parse(localStorage.getItem(key) || '{}') as { pending?: Pending; taskId?: string }; }
    catch { return {}; }
  });
  const [pending, setPending] = useState<Pending | undefined>(initial.pending);
  const [selectedId, setSelectedId] = useState(initial.taskId || '');
  const [selectedTask, setSelectedTask] = useState<Task | null>(null);
  const [source, setSource] = useState(initialSource || initial.pending?.source || '');
  const [sources, setSources] = useState<MaterialOption[]>([]);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [offset, setOffset] = useState(0);
  const [nextOffset, setNextOffset] = useState<number | null>(null);
  const [capability, setCapability] = useState<Capability | null>(null);
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);
  const mounted = useRef(true);
  const working = useRef(false);
  const requestSequence = useRef(0);
  const listSequence = useRef(0);
  async function request<T>(url: string, init?: RequestInit): Promise<T> {
    try {
      return await apiRequest<T>(url, { init });
    } catch (cause) {
      if (cause instanceof ApiError) throw new RequestError(cause.message, cause.status, cause.code);
      throw cause;
    }
  }
  function persist(value: { pending?: Pending; taskId?: string }) {
    localStorage.setItem(key, JSON.stringify(value));
  }
  function accept(task: Task) {
    if (!mounted.current) return;
    persist({ taskId: task.id }); setMessage(''); setPending(undefined); setSelectedId(task.id); setSelectedTask(task);
  }
  async function refresh() {
    const seq = ++listSequence.current;
    const data = await request<{ sources: MaterialOption[]; tasks: Task[]; next_offset: number | null }>(`${base}/enhancements?offset=${offset}`);
    if (!mounted.current || seq !== listSequence.current) return;
    setSources(data.sources); setTasks(data.tasks); setNextOffset(data.next_offset);
  }
  useEffect(() => {
    mounted.current = true;
    request<Capability>(`${base}/enhancement-capability`).then(data => { if (mounted.current) setCapability(data); }).catch(error => { if (mounted.current) setMessage(error.message); });
    return () => { mounted.current = false; requestSequence.current++; listSequence.current++; };
  }, []);
  useEffect(() => {
    let active = true, polling = false;
    async function poll() {
      if (polling) return;
      polling = true;
      try { await refresh(); } catch (error) { if (active) setMessage(error instanceof Error ? error.message : '读取处理记录失败'); }
      finally { polling = false; }
    }
    void poll(); const timer = window.setInterval(() => void poll(), 4000);
    return () => { active = false; listSequence.current++; window.clearInterval(timer); };
  }, [offset]);
  useEffect(() => {
    const seq = ++requestSequence.current;
    let active = true, polling = false;
    if (!selectedId && !pending) return;
    async function poll() {
      if (polling) return;
      polling = true;
      try {
        const task = await request<Task>(pending ? `${base}/submission-receipt?key=${encodeURIComponent(pending.idempotency_key)}` : `${base}/tasks/${encodeURIComponent(selectedId)}`);
        if (active && mounted.current && seq === requestSequence.current) {
          if (!task.payload?.enhancement) throw new Error('回执不是清晰化任务，请检查记录');
          if (pending) accept(task); else setSelectedTask(task);
        }
      } catch (error) { if (active && seq === requestSequence.current) setMessage(error instanceof Error ? error.message : '尚未确认提交回执'); }
      finally { polling = false; }
    }
    void poll(); const timer = window.setInterval(() => void poll(), 3000);
    return () => { active = false; window.clearInterval(timer); };
  }, [selectedId, pending?.idempotency_key]);
  async function submit() {
    if (working.current) return;
    working.current = true; setBusy(true); setMessage('');
    try {
      const body = pending || { source, idempotency_key: `enhance-${crypto.randomUUID()}` };
      persist({ pending: body }); setPending(body);
      const task = await request<Task>(`${base}/enhancement-tasks`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      accept(task); await refresh();
    } catch (error) {
      if (mounted.current) {
        const rejected = error instanceof RequestError && [400, 409, 422].includes(error.status) && error.code !== 'idempotency_key_conflict';
        if (rejected) { persist({ taskId: selectedId }); setPending(undefined); }
        setMessage(`${error instanceof Error ? error.message : '提交未确认'}${rejected ? '。请修改素材后重试。' : '。原请求已保留，可核对回执或沿用原请求重试。'}`);
      }
    }
    finally { working.current = false; if (mounted.current) setBusy(false); }
  }
  async function operate(action: 'stop' | 'reconcile') {
    if (!selectedId || working.current) return;
    working.current = true; setBusy(true);
    try { const task = await request<Task>(`${base}/tasks/${encodeURIComponent(selectedId)}/${action}`, { method: 'POST' }); if (mounted.current) setSelectedTask(task); }
    catch (error) { if (mounted.current) setMessage(error instanceof Error ? error.message : '操作失败'); }
    finally { working.current = false; if (mounted.current) setBusy(false); }
  }
  async function upload(file: File) {
    if (working.current) return;
    working.current = true; setBusy(true); setMessage('');
    try {
      const form = new FormData(); form.append('files', file, file.name);
      const data = await request<{ uploaded: string[] }>(`${base}/upload`, { method: 'POST', body: form });
      if (mounted.current) { await refresh(); setSource(data.uploaded[0] || ''); }
    } catch (error) { if (mounted.current) setMessage(error instanceof Error ? error.message : '上传失败'); }
    finally { working.current = false; if (mounted.current) setBusy(false); }
  }
  const recipe = selectedTask?.payload?.enhancement;
  const output = selectedTask?.result?.published_outputs?.[0] || (selectedTask?.result?.outputs?.[0] ? `output/${selectedTask.result.outputs[0]}` : '');
  const chosen = sources.find(item => item.value === source);
  const available = chosen && capability?.kinds[chosen.kind as 'image' | 'video']?.available;
  function preview(path: string, kind: string, label: string) {
    return kind === 'image' ? <img src={mediaUrl(path)} alt={label} /> : <video aria-label={label} src={mediaUrl(path)} controls preload="metadata" playsInline />;
  }
  return <section className="enhancement-panel" aria-label="图像／视频清晰化">
    <header><div><span className="director-kicker">先快速生成，再提高分辨率</span><h1>图像／视频清晰化</h1><p>AI 超分辨率 · 宽高各放大 2 倍 · 保持画幅</p></div><button className="button secondary" onClick={onBack}>返回快速生成</button></header>
    <section className="enhancement-input"><h2>选择原素材</h2><label>当前作品的图片或视频<select aria-label="清晰化原素材" value={source} disabled={busy || !!pending} onChange={event => setSource(event.target.value)}><option value="">选择已有候选或上传素材</option>{sources.map(item => <option key={item.value} value={item.value}>{item.kind === 'image' ? '图片' : '视频'} · {item.label}</option>)}</select></label>
      <label className="enhancement-upload">上传图片或视频<input aria-label="上传清晰化素材" type="file" accept=".png,.jpg,.jpeg,.webp,.mp4,.mov,.webm,.mkv" disabled={busy || !!pending} onChange={event => { const file = event.target.files?.[0]; if (file) void upload(file); event.target.value = ''; }} /></label>
      {chosen && <div className="enhancement-source">{preview(source, chosen.kind, '所选原素材')}</div>}
      <p>RealESRGAN 2× 增强；图片输出 PNG（不保留透明通道），视频保留原帧率与音频。AI 可能改变纹理，请对照原素材检查；逐帧增强不保证消除模糊或闪烁。</p>
      <p className="enhancement-limits">{capability?.limits || '正在检查处理环境…'}</p>
      {chosen && capability && !available && <p role="alert">环境尚未就绪：{capability.kinds[chosen.kind as 'image' | 'video']?.missing.join('、')}</p>}
      <div className="enhancement-actions"><button className="button primary" disabled={busy || !pending && (!source || !available)} onClick={() => void submit()}>{pending ? '沿用原请求重试' : '开始 AI 清晰化 · 2×'}</button>{pending && <button className="button secondary" disabled={busy} onClick={() => { void request<Task>(`${base}/submission-receipt?key=${encodeURIComponent(pending.idempotency_key)}`).then(accept).catch(error => setMessage(error.message)); }}>核对提交回执</button>}</div>
      {message && <p role="status">{message}</p>}
    </section>
    {selectedTask && <section className="enhancement-result"><h2>处理结果</h2><p>{statusLabel[selectedTask.status] || selectedTask.status} · {selectedTask.id.slice(0, 8)}</p><TaskQueueStatus task={selectedTask} />{selectedTask.error && <p role="alert">{selectedTask.error}</p>}
      {!terminal.includes(selectedTask.status) && <div className="enhancement-actions"><button className="button secondary" disabled={busy || selectedTask.status === 'needs_reconcile'} onClick={() => void operate('stop')}>停止处理</button><button className="button secondary" disabled={busy} onClick={() => void operate('reconcile')}>核对处理回执</button></div>}
      {recipe && <div className="enhancement-compare"><figure><figcaption>原素材 · {dimensions(recipe.source_facts)}</figcaption>{preview(recipe.source, recipe.source_facts.kind, '清晰化原素材预览')}<a href={mediaUrl(recipe.source)} download>下载原素材</a></figure><figure><figcaption>AI 清晰化候选 · {dimensions(selectedTask.result?.output_facts)}</figcaption>{output ? <>{preview(output, recipe.source_facts.kind, '清晰化结果预览')}<a className="button primary" href={mediaUrl(output)} download>下载清晰化候选</a></> : <p>完成后在这里比较和下载。</p>}</figure></div>}
      <p>原素材和历次结果分别保留。清晰化结果不会自动替换已采用的片段或成片。</p>
    </section>}
    <section className="enhancement-history"><h2>清晰化记录</h2>{!tasks.length && <p>还没有处理记录。</p>}<ul>{tasks.map(task => <li key={task.id}><button aria-pressed={selectedId === task.id} disabled={busy || !!pending} onClick={() => { persist({ taskId: task.id, pending }); setSelectedId(task.id); setSelectedTask(task); }}>{task.payload?.enhancement?.source_facts.kind === 'image' ? '图片' : '视频'} · {task.id.slice(0, 8)} · {statusLabel[task.status] || task.status}</button><time>{task.created_at ? new Date(task.created_at * 1000).toLocaleString('zh-CN') : ''}</time></li>)}</ul><div className="enhancement-actions"><button className="button secondary" disabled={!offset} onClick={() => setOffset(value => Math.max(0, value - 20))}>较新记录</button><button className="button secondary" disabled={nextOffset === null} onClick={() => setOffset(nextOffset!)}>更早记录</button></div></section>
  </section>;
}
