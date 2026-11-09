import { useEffect, useRef, useState } from 'react';
import TaskQueueStatus from './TaskQueueStatus';
import type { MaterialOption } from './MaterialSelect';
import { ApiError, mediaUrl, request as apiRequest } from './api/client';

type Facts = { width?: number; height?: number; duration_seconds?: number; frames?: number; fps?: number; audio_streams?: number };
type Recipe = { source: string; source_facts: Facts & { kind: 'image' | 'video' }; model: string; scale: number };
type Task = { id: string; asset_id: string; status: string; error?: string; created_at?: number; queue?: { state: string; position?: number | null; reason?: { code: string; message?: string } | null }; payload?: { enhancement?: Recipe }; result?: { published_outputs?: string[]; outputs?: string[]; output_facts?: Facts; comfy_timing?: { execution_seconds?: number } } };
type Pending = { source: string; idempotency_key: string };
type BatchItem = { name: string; taskId: string; status: string; archived?: string; error?: string };
type Capability = { model: string; limits: string; engine_offline?: boolean; kinds: Record<'image' | 'video', { available: boolean; missing: string[] }> };
type LibraryItem = { name: string; path: string; size: number; modified: number; width?: number | null; height?: number | null };
const IMAGE_EXTENSIONS = /\.(png|jpe?g|webp|gif|bmp)$/i;
// 与 backend/enhancement.py 的输入上限一致：静态图片 840 万像素，视频每帧 420 万。
const inputLimit = (name: string) => IMAGE_EXTENSIONS.test(name) ? 8_400_000 : 4_200_000;
const terminal = ['succeeded', 'failed', 'stopped'];
const statusLabel: Record<string, string> = { succeeded: '已完成 · 待比较', failed: '处理失败', stopped: '已停止', scheduler_waiting: '等待资源', preparing: '准备中', submitting: '正在提交', queued: '引擎排队中', running: '正在处理', stop_requested: '正在停止', stopping: '正在停止', needs_reconcile: '需要核对回执' };
const dimensions = (facts?: Facts) => facts?.width && facts.height ? `${facts.width} × ${facts.height}` : '尺寸待核验';
class RequestError extends Error {
  constructor(message: string, readonly status: number, readonly code?: string) { super(message); }
}

export default function EnhancementPanel({ projectId, initialSource = '', onBack }: { projectId: string; initialSource?: string; onBack: () => void }) {
  // The outer 后期 shell owns the page heading and tab navigation; the prop stays
  // accepted for the legacy workspace entrypoint but no longer renders a back button.
  void onBack;
  const base = `/api/projects/${encodeURIComponent(projectId)}`;
  const key = `director-workbench:${projectId}:enhancement:v1`;
  const [initial] = useState(() => {
    try { return JSON.parse(localStorage.getItem(key) || '{}') as { pending?: Pending; taskId?: string }; }
    catch { return {}; }
  });
  const [pending, setPending] = useState<Pending | undefined>(initial.pending);
  const [selectedId, setSelectedId] = useState(initial.taskId || '');
  const [selectedTask, setSelectedTask] = useState<Task | null>(null);
  const [overwrite, setOverwrite] = useState(false);
  const [archiving, setArchiving] = useState(false);
  const [archiveMessage, setArchiveMessage] = useState('');
  const [library, setLibrary] = useState<LibraryItem[]>([]);
  const [librarySource, setLibrarySource] = useState<MaterialOption | null>(null);
  const [confirming, setConfirming] = useState('');
  const [removing, setRemoving] = useState('');
  const [clearing, setClearing] = useState(false);
  const [confirmClear, setConfirmClear] = useState(false);
  const [source, setSource] = useState(initialSource || initial.pending?.source || '');
  const [sources, setSources] = useState<MaterialOption[]>([]);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [offset, setOffset] = useState(0);
  const [nextOffset, setNextOffset] = useState<number | null>(null);
  const [capability, setCapability] = useState<Capability | null>(null);
  // 批量清晰化：一次选多个文件，逐条上传提交（引擎队列自然排队），全部完成后自动归档。
  const [batch, setBatch] = useState<BatchItem[]>([]);
  const [batchBusy, setBatchBusy] = useState(false);
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
  // 清掉「上次看的是哪条」：否则每次进入清晰化都会自动展开同一条结果。
  // 在途任务的提交凭据（pending）要留着，不然重试时找不到原请求。
  function clearResult() {
    if (pending) persist({ pending });
    else localStorage.removeItem(key);
    setSelectedId('');
    setSelectedTask(null);
    setArchiveMessage('');
    setMessage('已清除当前结果；下次进来不会自动展开这一条，记录仍留在下面的列表里。');
  }
  function accept(task: Task) {
    if (!mounted.current) return;
    persist({ taskId: task.id }); setMessage(''); setPending(undefined); setSelectedId(task.id); setSelectedTask(task);
  }
  // 清除记录 = 真删任务，并连带删掉它在 ComfyUI 输出目录里的产物文件。
  // 用的是和首页单步任务同一套两阶段删除：先冻结计划，再确认执行。
  async function deleteTaskRow(task: Task) {
    if (removing || clearing) return;
    setRemoving(task.id); setMessage('');
    try {
      const plan = await request<{ plan_id: string }>('/api/tasks/deletion-plan', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ task_ids: [task.id] }),
      });
      await request('/api/tasks/deletion', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ plan_id: plan.plan_id, task_ids: [task.id], archive_asset_ids: [], confirm: true }),
      });
      if (selectedId === task.id) {
        if (pending) persist({ pending }); else localStorage.removeItem(key);
        setSelectedId(''); setSelectedTask(null);
      }
      setConfirming('');
      setMessage(`已彻底删除记录 ${task.id.slice(0, 8)}，产物文件一并删除。`);
    } catch (reason) {
      setMessage(`删除未完成：${reason instanceof Error ? reason.message : '原因未知'}`);
    } finally { setRemoving(''); await refresh(); }
  }

  async function deleteAllTasks() {
    // 只清终态记录：还在排队或执行中的记录不能删，后端同样会拦。
    const ids = tasks.filter(item => terminal.includes(item.status)).map(item => item.id);
    if (!ids.length) { setMessage('没有可清除的记录。'); return; }
    setClearing(true); setMessage('');
    try {
      const plan = await request<{ plan_id: string }>('/api/tasks/deletion-plan', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ task_ids: ids }),
      });
      await request('/api/tasks/deletion', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ plan_id: plan.plan_id, task_ids: ids, archive_asset_ids: [], confirm: true }),
      });
      if (pending) persist({ pending }); else localStorage.removeItem(key);
      setSelectedId(''); setSelectedTask(null); setConfirmClear(false);
      setMessage(`已彻底删除 ${ids.length} 条记录及其产物文件。`);
    } catch (reason) {
      setMessage(`清除未完成：${reason instanceof Error ? reason.message : '原因未知'}`);
    } finally { setClearing(false); await refresh(); }
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
      if (mounted.current) { await refresh(); setSource(data.uploaded[0] || ''); setLibrarySource(null); }
    } catch (error) { if (mounted.current) setMessage(error instanceof Error ? error.message : '上传失败'); }
    finally { working.current = false; if (mounted.current) setBusy(false); }
  }

  /** 批量清晰化：上传 + 提交全在前台逐条做，处理交给引擎队列；
   * 每条到终态就立刻归档，所以这一轮跑完，清晰化库里就是全部成品。 */
  async function runBatch(fileList: FileList | File[]) {
    const files = Array.from(fileList);
    if (!files.length || batchBusy) return;
    setBatchBusy(true); setMessage('');
    const items: BatchItem[] = files.map(file => ({ name: file.name, taskId: '', status: '上传中', error: '' }));
    setBatch([...items]);
    let succeeded = 0;
    let failed = 0;
    try {
      for (let index = 0; index < files.length; index += 1) {
        try {
          const form = new FormData(); form.append('files', files[index], files[index].name);
          const data = await request<{ uploaded: string[] }>(`${base}/upload`, { method: 'POST', body: form });
          const source = data.uploaded[0] || '';
          if (!source) throw new Error('上传返回为空');
          const task = await request<Task>(`${base}/enhancement-tasks`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ source, idempotency_key: `enhance-batch-${crypto.randomUUID()}` }),
          });
          items[index] = { ...items[index], taskId: task.id, status: statusLabel[task.status] || task.status };
        } catch (reason) {
          items[index] = { ...items[index], status: 'failed', error: reason instanceof Error ? reason.message : '提交失败' };
          failed += 1;
        }
        setBatch([...items]);
      }
      // 轮询：一次把所有未终态的问一遍；20 分钟封顶，避免坏任务把界面吊死。
      for (let round = 0; round < 400; round += 1) {
        const open = items.filter(item => item.taskId && !terminal.includes(item.status));
        if (!open.length) break;
        for (const item of open) {
          const index = items.findIndex(row => row.taskId === item.taskId);
          try {
            const current = await request<Task>(`${base}/tasks/${encodeURIComponent(item.taskId)}`);
            if (current.status === 'succeeded') {
              const archived = await apiRequest<{ path: string }>(
                `${base}/enhancements/${encodeURIComponent(current.id)}/archive`, { init: { method: 'POST' }, json: {} });
              items[index] = { ...items[index], status: 'succeeded', archived: String(archived.path).split(/[\\/]/).pop() };
              succeeded += 1;
            } else if (terminal.includes(current.status)) {
              items[index] = { ...items[index], status: current.status, error: current.error || statusLabel[current.status] || current.status };
              failed += 1;
            } else {
              items[index] = { ...items[index], status: statusLabel[current.status] || current.status };
            }
          } catch (reason) {
            items[index] = { ...items[index], status: 'failed', error: reason instanceof Error ? reason.message : '查询失败' };
            failed += 1;
          }
          setBatch([...items]);
        }
        if (items.some(item => item.taskId && !terminal.includes(item.status))) await new Promise(resolve => setTimeout(resolve, 3000));
      }
      const unfinished = items.filter(item => item.taskId && !terminal.includes(item.status)).length;
      setMessage(unfinished
        ? `批量超时：${unfinished} 条仍在处理，记录都在下面的列表里，可稍后回来核对。成功 ${succeeded}、失败 ${failed}。`
        : `批量完成：${succeeded} 条已存入清晰化库${failed ? `，${failed} 条失败` : ''}。`);
    } finally {
      setBatchBusy(false);
      setBatch([...items]);
      await refresh();
      await loadLibrary();
    }
  }
  const recipe = selectedTask?.payload?.enhancement;
  const output = selectedTask?.result?.published_outputs?.[0] || (selectedTask?.result?.outputs?.[0] ? `output/${selectedTask.result.outputs[0]}` : '');
  const chosen = sources.find(item => item.value === source)
    || (librarySource?.value === source ? librarySource : undefined);
  const available = chosen && (capability?.kinds[chosen.kind as 'image' | 'video']?.available || capability?.engine_offline);
  function preview(path: string, kind: string, label: string) {
    return kind === 'image' ? <img src={mediaUrl(path)} alt={label} /> : <video aria-label={label} src={mediaUrl(path)} controls preload="metadata" playsInline />;
  }
  async function loadLibrary() {
    try {
      const data = await apiRequest<{ items: LibraryItem[] }>('/api/enhance-library');
      setLibrary(data.items || []);
    } catch { setLibrary([]); }
  }
  useEffect(() => { void loadLibrary(); }, []);

  async function archiveEnhancement() {
    if (!selectedTask || archiving) return;
    setArchiving(true); setArchiveMessage('');
    try {
      const result = await apiRequest<{ path: string; overwritten: boolean; deleted_original?: string | null }>(
        `${base}/enhancements/${selectedTask.id}/archive`, { init: { method: 'POST' }, json: { overwrite } });
      setArchiveMessage(`已存入清晰化库：${result.path}${result.overwritten
        ? (result.deleted_original ? '；原文件已删除，素材登记改指向清晰化版本。' : '；素材登记已指向清晰化版本。')
        : ''}`);
    } catch (reason) {
      // 失败也要说清原因：一句「归档未完成」会让人以为库坏了，其实是请求没发出去。
      setArchiveMessage(`归档未完成：${reason instanceof Error ? reason.message : '原因未知，请重试或核对记录'}`);
    } finally { setArchiving(false); await loadLibrary(); }
  }

  // 库里的成品可以再走一遍清晰化，但那是**新任务**：只把这份文件带回「原素材」，
  // 库里这一份一个字节都不动。删除则是直接删文件，删前在行内确认一次。
  function reuseArchived(item: LibraryItem) {
    setPending(undefined);
    setSource(item.path);
    // 库文件不在作品素材候选里，单独挂一个选项，否则下拉框回落到空白、用户看不出已选。
    setLibrarySource({ value: item.path, label: `清晰化库 · ${item.name}`,
      kind: IMAGE_EXTENSIONS.test(item.name) ? 'image' : 'video' });
    setMessage(`已把「${item.name}」设为原素材；点「开始 AI 清晰化」会另起一条新任务，库里这份不会被改动。`);
  }

  async function removeArchived(name: string) {
    if (removing) return;
    setRemoving(name); setMessage('');
    try {
      await apiRequest<{ deleted: string }>('/api/enhance-library/delete', { json: { name } });
      setMessage(`已删除库里的成品：${name}`);
      setConfirming('');
    } catch (reason) {
      setMessage(`删除未完成：${reason instanceof Error ? reason.message : '原因未知，请重试'}`);
    } finally { setRemoving(''); await loadLibrary(); }
  }
  return <section className="sf-post-tools" aria-label="图像／视频清晰化">
    <section className="sf-post-section" aria-label="选择原素材">
      <div className="sf-post-section-head"><h2 className="sf-post-title">选择原素材</h2><p className="sf-post-meta">AI 超分辨率 · 宽高各放大 2 倍 · 保持画幅</p></div>
      <div className="sf-post-fields">
        <label className="sf-post-field sf-post-field--wide">当前作品的图片或视频<select aria-label="清晰化原素材" value={source} disabled={busy || !!pending} onChange={event => { setSource(event.target.value); setLibrarySource(null); }}><option value="">选择已有候选或上传素材</option>{sources.map(item => <option key={item.value} value={item.value}>{item.kind === 'image' ? '图片' : '视频'} · {item.label}</option>)}{librarySource && !sources.some(item => item.value === librarySource.value) && <option value={librarySource.value}>{librarySource.label}</option>}</select></label>
        <label className="sf-post-field sf-post-field--wide">上传图片或视频<input className="sf-post-file" aria-label="上传清晰化素材" type="file" accept=".png,.jpg,.jpeg,.webp,.heic,.heif,.mp4,.mov,.webm,.mkv" disabled={busy || !!pending} onChange={event => { const file = event.target.files?.[0]; if (file) void upload(file); event.target.value = ''; }} /></label>
      </div>
      {chosen && <div className="sf-post-source">{preview(source, chosen.kind, '所选原素材')}</div>}
      <p className="sf-post-hint">RealESRGAN 2× 增强；图片输出 PNG（不保留透明通道），视频保留原帧率与音频。AI 可能改变纹理，请对照原素材检查；逐帧增强不保证消除模糊或闪烁。</p>
      <p className="sf-post-hint">来源尽量选原生输出（文件名不带 <code>-output-2k</code>／<code>-output-4k</code>）：2K、4K 是生成后用 Lanczos 放大的，细节仍停在原生分辨率，对着放大件再做 2× 只会更软。</p>
      <p className="sf-post-hint">{capability?.limits || '正在检查处理环境…'}</p>
      {chosen && capability && !available && !capability.engine_offline && <p className="sf-post-alert" role="alert">环境尚未就绪：{capability.kinds[chosen.kind as 'image' | 'video']?.missing.join('、')}</p>}
      {capability?.engine_offline && <p className="sf-post-hint">生成引擎当前未运行；点开始处理会自动拉起，首次约 10–30 秒。</p>}
      <div className="sf-post-actions"><button className="button primary" disabled={busy || !pending && (!source || !available)} onClick={() => void submit()}>{pending ? '沿用原请求重试' : '开始 AI 清晰化 · 2×'}</button>{pending && <button className="button secondary" disabled={busy} onClick={() => { void request<Task>(`${base}/submission-receipt?key=${encodeURIComponent(pending.idempotency_key)}`).then(accept).catch(error => setMessage(error.message)); }}>核对提交回执</button>}</div>
      {message && <p className="sf-post-message" role="status" aria-live="polite">{message}</p>}
    </section>
    <section className="sf-post-section" aria-label="批量清晰化">
      <div className="sf-post-section-head"><h2 className="sf-post-title">批量清晰化</h2><p className="sf-post-meta">一次多选 · 自动排队 · 成品自动入库</p></div>
      <label className="sf-post-field sf-post-field--wide">一次选择多个文件（图片 / 视频），选完即开始排队
        <input className="sf-post-file" aria-label="批量选择清晰化素材" type="file" multiple
          accept=".png,.jpg,.jpeg,.webp,.heic,.heif,.mp4,.mov,.webm,.mkv" disabled={batchBusy}
          onChange={event => { const files = event.target.files; if (files?.length) void runBatch(files); event.target.value = ''; }} />
      </label>
      {batch.length > 0 && <ul className="sf-post-history sf-post-history--library" aria-label="批量队列">
        {batch.map(item => <li key={`${item.name}-${item.taskId || 'pending'}`}>
          <span className={item.status === 'failed' ? 'danger' : undefined}>{item.name}</span>
          <span className="sf-lib-meta"><b>{statusLabel[item.status] || item.status}</b>
            {item.archived && <small> → 已入库：{item.archived}</small>}
            {item.error && <small className="warn"> {item.error}</small>}
          </span>
        </li>)}
      </ul>}
      <p className="sf-post-hint">一次提交多条任务，引擎按队列逐条处理（同一时间 GPU 只跑一条，后面的自动等）；每条完成后自动存进清晰化库，不需要手动归档。</p>
    </section>
    {selectedTask && <section className="sf-post-section" aria-label="处理结果">
      <div className="sf-post-section-head"><h2 className="sf-post-title">处理结果</h2><p className="sf-post-status">{statusLabel[selectedTask.status] || selectedTask.status} · {selectedTask.id.slice(0, 8)}</p><button type="button" className="sf-post-clear" onClick={clearResult}>清除</button></div>
      <TaskQueueStatus task={selectedTask} />
      {selectedTask.error && <p className="sf-post-alert" role="alert">{selectedTask.error}</p>}
      {!terminal.includes(selectedTask.status) && <div className="sf-post-actions"><button className="button secondary" disabled={busy || selectedTask.status === 'needs_reconcile'} onClick={() => void operate('stop')}>停止处理</button><button className="button secondary" disabled={busy} onClick={() => void operate('reconcile')}>核对处理回执</button></div>}
      {recipe && <div className="sf-post-compare"><figure className="sf-post-figure"><figcaption>原素材 · {dimensions(recipe.source_facts)}</figcaption>{preview(recipe.source, recipe.source_facts.kind, '清晰化原素材预览')}<a href={mediaUrl(recipe.source)} download>下载原素材</a></figure><figure className="sf-post-figure"><figcaption>AI 清晰化候选 · {dimensions(selectedTask.result?.output_facts)}</figcaption>{output ? <>{preview(output, recipe.source_facts.kind, '清晰化结果预览')}<a className="button primary" href={mediaUrl(output)} download>下载清晰化候选</a></> : <p className="sf-post-hint">完成后在这里比较和下载。</p>}</figure></div>}
      {selectedTask.status === 'succeeded' && output && <div className="sf-post-subblock" aria-label="清晰化归档">
        <div className="sf-post-section-head"><h3 className="sf-post-subtitle">归档到清晰化库</h3><p className="sf-post-meta">成品名固定带「清晰化」标签</p></div>
        <label className="sf-post-check"><input type="checkbox" checked={overwrite} disabled={archiving} onChange={event => setOverwrite(event.target.checked)} />同时用清晰化结果覆盖原素材，并删除原文件（不可恢复）</label>
        <div className="sf-post-actions"><button className="button primary compact" type="button" disabled={archiving} onClick={() => void archiveEnhancement()}>{archiving ? '正在归档…' : '存入清晰化库'}</button></div>
        {archiveMessage && <p className="sf-post-message" role="status" aria-live="polite">{archiveMessage}</p>}
      </div>}
      <p className="sf-post-hint">不勾覆盖时原素材和历次结果都保留；清晰化结果不会自动替换已采用的片段或成片。</p>
    </section>}
    <section className="sf-post-section" aria-label="清晰化库">
      <div className="sf-post-section-head"><h2 className="sf-post-title">清晰化库</h2><p className="sf-post-meta">{library.length} 个成品 · 素材库\清晰化</p></div>
      {!library.length ? <p className="sf-post-hint">还没有归档的清晰化成品。完成后点「存入清晰化库」。</p> : <ul className="sf-post-history sf-post-history--library">{library.map(item => {
        const limit = inputLimit(item.name);
        const pixels = (item.width ?? 0) * (item.height ?? 0);
        const tooLarge = pixels > limit;
        return <li key={item.path}>
          <a href={mediaUrl(item.path)} target="_blank" rel="noreferrer">{item.name}</a>
          <span className="sf-lib-meta">
            <b className={tooLarge ? 'warn' : undefined}>
              {item.width && item.height ? `${item.width} × ${item.height}` : '尺寸未知'}{tooLarge ? ` · 超过 ${limit / 10000} 万上限` : ''}
            </b>
            <small>{(item.size / 1048576).toFixed(1)} MB · {new Date(item.modified * 1000).toLocaleString('zh-CN')}</small>
          </span>
          <span className="sf-lib-actions">
            <button type="button" disabled={!!removing || tooLarge}
              title={tooLarge ? '超过清晰化输入上限，不能再走一遍；要重做请用更小的原素材' : '设为原素材，另起一条新任务；库里这份不会被改动'}
              onClick={() => reuseArchived(item)}>再次清晰化</button>
            {confirming === item.name
              ? <>
                <button type="button" className="danger" disabled={!!removing} onClick={() => void removeArchived(item.name)}>{removing === item.name ? '删除中…' : '确认删除'}</button>
                <button type="button" disabled={!!removing} onClick={() => setConfirming('')}>取消</button>
              </>
              : <button type="button" disabled={!!removing} onClick={() => setConfirming(item.name)}>删除</button>}
          </span>
        </li>;
      })}</ul>}
    </section>
    <section className="sf-post-section" aria-label="清晰化记录">
      <div className="sf-post-section-head">
        <h2 className="sf-post-title">清晰化记录</h2>
        <p className="sf-post-meta">选择记录可比较历次结果</p>
        {tasks.length > 0 && (confirmClear
          ? <span className="sf-lib-actions">
            <button type="button" className="sf-post-clear danger" disabled={clearing} onClick={() => void deleteAllTasks()}>{clearing ? '清除中…' : `确认清除 ${tasks.filter(item => terminal.includes(item.status)).length} 条`}</button>
            <button type="button" className="sf-post-clear" disabled={clearing} onClick={() => setConfirmClear(false)}>取消</button>
          </span>
          : <button type="button" className="sf-post-clear" disabled={clearing} onClick={() => setConfirmClear(true)}>清除全部</button>)}
      </div>
      {!tasks.length && <p className="sf-post-hint">还没有处理记录。</p>}
      <ul className="sf-post-history sf-post-history--library">{tasks.map(task => <li key={task.id}><button aria-pressed={selectedId === task.id} disabled={busy || !!pending} onClick={() => { persist({ taskId: task.id, pending }); setSelectedId(task.id); setSelectedTask(task); }}>{task.payload?.enhancement?.source_facts.kind === 'image' ? '图片' : '视频'} · {task.id.slice(0, 8)} · {statusLabel[task.status] || task.status}</button><time>{task.created_at ? new Date(task.created_at * 1000).toLocaleString('zh-CN') : ''}</time>
        <span className="sf-lib-actions">
          {confirming === task.id
            ? <>
              <button type="button" className="danger" disabled={!!removing || clearing} onClick={() => void deleteTaskRow(task)}>{removing === task.id ? '删除中…' : '确认'}</button>
              <button type="button" disabled={!!removing || clearing} onClick={() => setConfirming('')}>取消</button>
            </>
            : <button type="button" disabled={!!removing || clearing || !terminal.includes(task.status)}
              title={terminal.includes(task.status) ? '删除这条记录，并同时删除它的产物文件' : '任务还在处理中，先停止再删'}
              onClick={() => setConfirming(task.id)}>删除</button>}
        </span>
      </li>)}</ul>
      <div className="sf-post-actions"><button className="button secondary" disabled={!offset} onClick={() => setOffset(value => Math.max(0, value - 20))}>较新记录</button><button className="button secondary" disabled={nextOffset === null} onClick={() => setOffset(nextOffset!)}>更早记录</button></div>
    </section>
  </section>;
}
