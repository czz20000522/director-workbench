import { useEffect, useRef, useState } from 'react';
import { Bookmark, CheckSquare, Download, ImagePlus, LoaderCircle, PenLine, Scaling, Sparkles, Square, Trash2, Wand2, X } from 'lucide-react';
import { mediaUrl, request } from './api/client';
import { formatDuration } from './TaskProgress';
import { invalidateMaterialIndex } from './ReferenceSlotField';
import { notifyLibraryChanged } from './ReferenceLibrary';
import TranslateButton from './TranslateButton';
import InpaintDialog from './InpaintDialog';
import LiquifyDialog from './LiquifyDialog';
import './reference-image.css';

/** 参考图生成：给一张图，写/识别一段提示词，批量出几张相似但不重样的图。
 *
 * 三步，都是显式动作：
 *  1. 选图（上传到当前单步工作区）；
 *  2. 「识别内容」→ 本地多模态模型（qwen3.5-abliterated 自带 vision）看图写提示词，
 *     结果放在可编辑的框里，改完再生成；
 *  3. 填张数，一次提交 N 个任务，每张换一个新种子。
 *
 * 走的是现成的 keyframe-tasks 参考图模式（TextEncodeQwenImage21 的 images 输入），
 * 不是严格的 img2img：模型把它当语义参考，所以出的是"同一类画面"，不是像素级复刻。
 */

type Progress = { percent: number; value: number; max: number; eta_seconds?: number | null };

type Task = {
  id: string;
  status: string;
  error?: string;
  progress?: Progress | null;
  created_at?: number;
  started_at?: number | null;
  updated_at?: number;
  result?: { output?: string; comfy_timing?: { comfy_execution_seconds?: number | null } };
};

type Item = {
  id: string;
  /** 第几张：跨批次连续编号，后出的不会把前一批的号顶掉。 */
  no: number;
  status: string;
  output?: string;
  error?: string;
  /** ComfyUI 的步级进度；排队或已完成时为 null。 */
  progress?: Progress | null;
  /** 这张实际跑了多久：用服务端 started_at → updated_at 算，比前端轮询间隔准。 */
  seconds?: number;
  /** 完成时刻（秒级时间戳），鼠标悬停看具体几点。 */
  finishedAt?: number;
  /** 本地产物（液化塑形上传的图）：没有任务记录，删除只从列表移除、不删文件。 */
  ephemeral?: boolean;
};

const TERMINAL = ['succeeded', 'failed', 'stopped'];

/** 耗时优先用 ComfyUI 回执里的真实执行时长：排队等 GPU、事后核对回执的时间都不算生成。
 *  老任务没有这个字段时，才退回服务端 started_at → updated_at。 */
const spentSeconds = (task: Task) => {
  const engine = task.result?.comfy_timing?.comfy_execution_seconds;
  if (typeof engine === 'number' && engine >= 0.05) return engine;
  const start = task.started_at || task.created_at;
  const end = task.updated_at;
  if (typeof start !== 'number' || typeof end !== 'number') return undefined;
  const value = end - start;
  return value > 0.5 ? value : undefined;
};

const randomSeed = () => crypto.getRandomValues(new Uint32Array(1))[0];

const SIZES = {
  portrait: { width: 768, height: 1344 },
  landscape: { width: 1344, height: 768 },
  square: { width: 1024, height: 1024 },
} as const;

export default function ReferenceImagePanel({ projectId, onComplete, restore }: {
  projectId?: string;
  onComplete?: () => void;
  /** 首页「编辑重生成」带回来的那一版：参考图、提示词、画幅、降噪。 */
  restore?: { image?: string; prompt?: string; aspect?: '16:9' | '9:16' | '1:1'; refine?: boolean };
}) {
  const [workspaceId, setWorkspaceId] = useState(projectId || '');
  const [image, setImage] = useState(restore?.image || '');
  const [prompt, setPrompt] = useState(restore?.prompt || '');
  // 默认只出一张：先看这张对不对，再决定要不要批量。一次出两张容易白跑一遍算力。
  const [count, setCount] = useState(1);
  // 参考图可能是横的、也可能是竖的：固定成竖版会把构图切掉，所以给三档。
  const [aspect, setAspect] = useState<'portrait' | 'landscape' | 'square'>(
    restore?.aspect === '16:9' ? 'landscape' : restore?.aspect === '1:1' ? 'square' : 'portrait');
  const [refine, setRefine] = useState(restore?.refine ?? true);
  const [describing, setDescribing] = useState(false);
  // 润色：识别出来的词往往只是一段"看见什么"，语序和可拍的细节得再顺一遍。
  const [polishing, setPolishing] = useState(false);
  // 润色是整段重写，留一份原文，改坏了一点就能退回去。
  const [polishBackup, setPolishBackup] = useState<string | null>(null);
  // 识图通道：本地多模态 / 在线 vision。和生成通道分开选，互不影响。
  const [visionProvider, setVisionProvider] = useState<'local' | 'online'>('local');
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const [items, setItems] = useState<Item[]>([]);
  // 生成结果的三个后续动作：点开看大图、存进素材库、删掉（进回收站）。
  const [previewId, setPreviewId] = useState('');
  const [savingId, setSavingId] = useState('');
  const [deleteTarget, setDeleteTarget] = useState<Item | null>(null);
  // 精修：复用局部重绘，但降噪给低（0.35），只改涂出来的五官/肤色区域，尽量不换人。
  const [refineTarget, setRefineTarget] = useState<Item | null>(null);
  const [liquifyTarget, setLiquifyTarget] = useState<Item | null>(null);
  const [deleting, setDeleting] = useState(false);
  // 批量删除：结果不封顶地累积，所以得能一次勾一堆再删。
  const [pickMode, setPickMode] = useState(false);
  const [picked, setPicked] = useState<string[]>([]);
  const [confirmBatch, setConfirmBatch] = useState(false);
  /** 卡片编号计数器：换一批生成接着往下排，不从头开始。 */
  const seqRef = useRef(0);
  /** 轮询代号：新的一次生成或一次删除都会推进它，旧轮询据此作废。 */
  const pollSeq = useRef(0);
  /** 这一批的 id：结果区是累加的，进度只能按本批算，不能拿总数当分母。 */
  const [activeBatch, setActiveBatch] = useState<string[]>([]);
  const aliveRef = useRef(true);
  // 只在「换了另一条任务」时铺一次：外层每次渲染都会新建 restore 对象，
  // 不设这道闸，你改到一半的提示词会被打回原样。
  const appliedRestore = useRef('');

  useEffect(() => () => { aliveRef.current = false; }, []);
  useEffect(() => { if (projectId && !workspaceId) setWorkspaceId(projectId); }, [projectId, workspaceId]);
  useEffect(() => {
    if (!restore?.image) return;
    const ticket = [restore.image, restore.prompt || '', restore.aspect || '', String(restore.refine)].join('|');
    if (appliedRestore.current === ticket) return;
    appliedRestore.current = ticket;
    setImage(restore.image);
    setPrompt(restore.prompt || '');
    if (restore.aspect === '16:9') setAspect('landscape');
    else if (restore.aspect === '1:1') setAspect('square');
    else if (restore.aspect === '9:16') setAspect('portrait');
    if (typeof restore.refine === 'boolean') setRefine(restore.refine);
  }, [restore]);

  const ensureWorkspace = async () => {
    if (workspaceId) return workspaceId;
    const data = await request<{ project_id: string }>('/api/quick-workspace');
    setWorkspaceId(data.project_id);
    return data.project_id;
  };

  const upload = async (file: File) => {
    setError(''); setMessage('正在上传…');
    try {
      const id = await ensureWorkspace();
      const form = new FormData();
      form.append('files', file, `assets/${file.name}`);
      const data = await request<{ uploaded?: string[] }>(`/api/projects/${id}/upload`,
        { init: { method: 'POST', body: form } });
      const path = data.uploaded?.[0] || '';
      if (!path) throw new Error('上传没有返回路径');
      setImage(path); setPrompt(''); setPolishBackup(null); setItems([]); setMessage('');
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '上传失败');
      setMessage('');
    }
  };

  const describe = async () => {
    if (!image) { setError('先选一张图'); return; }
    setDescribing(true); setError(''); setMessage('正在看图…');
    try {
      const data = await request<{ prompt: string; elapsed_ms: number; model: string; provider: string }>(
        '/api/prompt-writer/describe-image',
        { json: { image_path: image, provider: visionProvider }, timeoutMs: 180_000 });
      setPrompt(data.prompt);
      setPolishBackup(null);
      setMessage(`识别完成（${data.provider === 'online' ? '在线' : '本地'} · ${(data.elapsed_ms / 1000).toFixed(1)} 秒），可以直接改，再点生成`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '识别失败');
      setMessage('');
    } finally { setDescribing(false); }
  };

  /** 润色：把框里这段交给同一个通道改写。后端锁了"不删细节、不换设定、只补能拍的"。 */
  const polish = async () => {
    const current = prompt.trim();
    if (!current) { setError('先识别或写一段提示词，再点润色'); return; }
    setPolishing(true); setError('');
    setMessage(visionProvider === 'local'
      ? '正在润色…（本机模型没运行时会先拉起，首次约 20 秒，别重复点）'
      : '正在润色…');
    try {
      const data = await request<{ prompt: string; provider?: string; started_service?: boolean }>(
        '/api/prompt-writer/draft',
        { json: { idea: current, provider: visionProvider, mode: 'polish' }, timeoutMs: 300_000 });
      const next = String(data.prompt || '').trim();
      if (!next) throw new Error('模型没返回内容，稍后再试');
      setPolishBackup(current);
      setPrompt(next);
      setMessage(`润色完成（${data.provider === 'online' ? '在线' : '本地'}${data.started_service ? ' · 本机模型刚拉起' : ''}），可以直接改，再点生成`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '润色失败');
      setMessage('');
    } finally { setPolishing(false); }
  };

  const undoPolish = () => {
    if (polishBackup === null) return;
    setPrompt(polishBackup);
    setPolishBackup(null);
    setMessage('已退回润色前的版本');
  };

  /** 提交完之后才轮询：否则「items 还是空的」会被当成「全部完成」，直接收工。
   *  带上 seq：删除或又点了一次生成时，上一次的轮询要作废，不能让它的收尾盖掉新提示。 */
  const poll = async (pending: string[], seq: number) => {
    let ids = [...pending];
    while (ids.length && aliveRef.current && pollSeq.current === seq) {
      await new Promise(resolve => setTimeout(resolve, 2500));
      if (!aliveRef.current || pollSeq.current !== seq) return;
      const settled = await Promise.all(ids.map(async id => {
        try {
          const task = await request<Task>(`/api/tasks/${encodeURIComponent(id)}`);
          const done = TERMINAL.includes(task.status);
          return { id, status: task.status, output: task.result?.output, error: task.error,
                   progress: done ? null : (task.progress || null),
                   seconds: done ? spentSeconds(task) : undefined,
                   finishedAt: done ? task.updated_at : undefined };
        } catch { return { id, status: '', output: undefined, error: undefined,
                            progress: null, seconds: undefined, finishedAt: undefined }; }
      }));
      if (!aliveRef.current) return;
      setItems(current => current.map(item => {
        const next = settled.find(entry => entry.id === item.id);
        if (!next || !next.status) return item;
        return { ...item, status: next.status, output: next.output, error: next.error,
                 progress: next.progress, seconds: next.seconds ?? item.seconds,
                 finishedAt: next.finishedAt ?? item.finishedAt };
      }));
      ids = settled.filter(entry => entry.status && !TERMINAL.includes(entry.status)).map(entry => entry.id);
    }
    if (!aliveRef.current || pollSeq.current !== seq) return;
    setBusy(false);
    setMessage('全部生成完成');
    onComplete?.();
  };

  const generate = async () => {
    if (!image) { setError('先选一张图'); return; }
    if (!prompt.trim()) { setError('先写或识别一段提示词'); return; }
    const total = Math.min(8, Math.max(1, Math.round(count)));
    setBusy(true); setError(''); setMessage(`提交 ${total} 个任务…`);
    const seq = (pollSeq.current += 1);
    try {
      const id = await ensureWorkspace();
      const created: Item[] = [];
      for (let index = 0; index < total; index += 1) {
        const task = await request<Task>(`/api/projects/${id}/keyframe-tasks`, {
          json: {
            mode: 'reference',
            reference_image: image,
            prompt: prompt.trim(),
            seed: randomSeed(),
            ...SIZES[aspect],
            steps: 8,
            refine,
            idempotency_key: `refimg-${crypto.randomUUID()}`,
          },
        });
        const item: Item = { id: task.id, status: task.status, no: (seqRef.current += 1) };
        created.push(item);
        // 累加而不是替换：先出的留着，编号接着往下排，想清掉用结果区右下角的「批量删除」。
        setItems(current => [...current, item]);
        setActiveBatch(created.map(entry => entry.id));
      }
      setMessage(`已提交 ${total} 个，排队生成中…`);
      await poll(created.map(item => item.id), seq);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '提交失败');
      setMessage('');
      setBusy(false);
    }
  };

  /** 存进素材库：和首帧工作台同一个入口（素材库「首帧图参考」），下次能直接选。 */
  const saveToLibrary = async (item: Item) => {
    if (!item.output) return;
    setSavingId(item.id); setError('');
    try {
      const head = prompt.trim().replace(/\s+/g, ' ').slice(0, 16) || '参考图';
      const data = await request<{ material: { title: string; category: string } }>(
        '/api/reference-library/first-frames', { json: { path: item.output, title: head } });
      invalidateMaterialIndex();
      notifyLibraryChanged();
      setMessage(`已存入素材库「${data.material.category}」：${data.material.title}`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '存入素材库失败');
    } finally { setSavingId(''); }
  };

  /** 删除：走和首页单步任务同一套删除流程——记录一起删，产物进回收站可还原。 */
  const removeMany = async (ids: string[]) => {
    if (!ids.length || deleting) return;
    const before = items;
    // 一次删除也算一次「新动作」：把手上的轮询作废，免得它的收尾把删除提示盖成「全部生成完成」。
    pollSeq.current += 1;
    setBusy(false);
    setDeleting(true); setError('');
    setMessage(`正在删除 ${ids.length} 条…`);
    // 先把卡片摘掉再发删除请求：Windows 上 <img> 还挂着这张图时文件是打开的，
    // 回收站会报「共享冲突」，重试也过不去。摘掉 DOM 后句柄就放开了。
    setItems(current => current.filter(entry => !ids.includes(entry.id)));
    setPreviewId(current => (ids.includes(current) ? '' : current));
    setDeleteTarget(null);
    setConfirmBatch(false);
    setPickMode(false);
    setPicked([]);
    try {
      // 等一帧：React 的更新是批量刷的，fetch 抢在渲染前面发出去的话，
      // <img> 还挂在 DOM 上，Windows 就一直报共享冲突。
      await new Promise<void>(resolve => requestAnimationFrame(() => resolve()));
      const plan = await request<{ plan_id: string; entries: { task_id: string }[] }>(
        '/api/tasks/deletion-plan', { json: { task_ids: ids } });
      const result = await request<{ failed?: string[] }>('/api/tasks/deletion', {
        json: { plan_id: plan.plan_id, task_ids: plan.entries.map(entry => entry.task_id), confirm: true },
      });
      setMessage(result.failed?.length
        ? `已删除 ${ids.length} 条，但有文件没能移入回收站（多半刚写完还被占用），稍后再删一次`
        : `已删除 ${ids.length} 条，产物已移入回收站，可从回收站还原`);
      onComplete?.();
    } catch (reason) {
      // 删失败就把卡片放回去——不要让人以为已经删掉了。
      setItems(before);
      setError(reason instanceof Error ? reason.message : '删除失败');
      onComplete?.();
    } finally { setDeleting(false); }
  };

  const removeItem = async () => {
    if (!deleteTarget) return;
    if (deleteTarget.ephemeral) {
      // 液化结果不是任务产物：只从这次列表里移除，文件留在作品 assets 下。
      setItems(current => current.filter(entry => entry.id !== deleteTarget.id));
      setDeleteTarget(null);
      setMessage('已从列表移除（文件仍在作品素材里）');
      return;
    }
    await removeMany([deleteTarget.id]);
  };

  const togglePick = (id: string) => setPicked(current => current.includes(id)
    ? current.filter(entry => entry !== id) : [...current, id]);
  const pickAll = () => setPicked(current => current.length === items.length ? [] : items.map(entry => entry.id));

  const done = items.filter(item => activeBatch.includes(item.id) && item.status === 'succeeded').length;
  const previewed = items.find(item => item.id === previewId && item.output) || null;

  return <section className="ri-panel" aria-label="参考图生成">
    <div className="ri-grid">
      <div className="ri-source">
        <span className="ri-label">参考图</span>
        {image
          ? <div className="ri-preview">
            <img src={mediaUrl(image)} alt="" />
            <button type="button" className="ri-remove" aria-label="移除参考图"
              onClick={() => { setImage(''); setPrompt(''); setPolishBackup(null); setItems([]); }}><X size={12} /></button>
          </div>
          : <label className="ri-drop">
            <ImagePlus size={22} />
            <strong>点这里选一张图</strong>
            <small>PNG / JPG / WebP / HEIC（实况图主图），建议竖版 3:4 以上</small>
            <input type="file" accept="image/png,image/jpeg,image/webp,.heic,.heif" hidden
              onChange={event => { const file = event.target.files?.[0]; if (file) void upload(file); event.target.value = ''; }} />
          </label>}
      </div>

      <div className="ri-write">
        <div className="ri-label-row">
          <span className="ri-label">提示词</span>
          <div className="ri-vision">
            <div className="ri-vision-tabs" role="tablist" aria-label="模型通道（识图与润色）">
              <button type="button" role="tab" aria-selected={visionProvider === 'local'}
                onClick={() => setVisionProvider('local')}>本地</button>
              <button type="button" role="tab" aria-selected={visionProvider === 'online'}
                onClick={() => setVisionProvider('online')}>在线</button>
            </div>
            <button type="button" className="ri-identify" disabled={!image || describing || polishing || busy}
              title={visionProvider === 'online' ? '在线识图走你配的接口；平台规则可能拒答成人向图片' : '本机模型识图，不联网、不拒答成人向'}
              onClick={() => void describe()}>
              {describing ? <><LoaderCircle size={12} className="sf-spin" /> 识别中…</> : <><Sparkles size={12} /> 识别内容</>}
            </button>
            <button type="button" className="ri-polish" disabled={!prompt.trim() || busy || describing || polishing}
              title="不动你写的设定：只顺语序，补上能拍的景别、机位、光线、材质；走上面选的通道"
              onClick={() => void polish()}>
              {polishing ? <><LoaderCircle size={12} className="sf-spin" /> 润色中…</> : <><PenLine size={12} /> 润色</>}
            </button>
            <TranslateButton text={prompt} disabled={busy || describing || polishing}
              provider={visionProvider}
              onTranslated={next => { setPolishBackup(null); setPrompt(next); setMessage('已译成英文，可以直接生成'); }}
              onError={message => { setError(message); setMessage(''); }} />
          </div>
        </div>
        <textarea rows={6} value={prompt} disabled={busy} className="ri-prompt"
          onChange={event => setPrompt(event.target.value)}
          placeholder="点「识别内容」让本机模型看图写一段，或者自己写：主体、服装、姿态、镜头、光线、背景。" />
        <div className="ri-controls">
          <label>张数
            <input type="number" min={1} max={8} value={count} disabled={busy}
              onChange={event => setCount(Number(event.target.value))} />
          </label>
          <label>画幅
            <select value={aspect} disabled={busy} onChange={event => setAspect(event.target.value as typeof aspect)}>
              <option value="portrait">竖版 3:4</option>
              <option value="landscape">横版 4:3</option>
              <option value="square">方图 1:1</option>
            </select>
          </label>
          <label className="ri-check">
            <input type="checkbox" checked={refine} disabled={busy}
              onChange={event => setRefine(event.target.checked)} />
            降噪
          </label>
          <small>每张换一个新种子，所以构图会不一样</small>
        </div>
        <button type="button" className="ri-generate" disabled={!image || !prompt.trim() || busy}
          onClick={() => void generate()}>
          {busy ? <><LoaderCircle size={13} className="sf-spin" /> 生成中 {done}/{activeBatch.length}</> : <><Wand2 size={13} /> 生成 {count} 张</>}
        </button>
        <p className="ri-note">这就是「图生图」：模型把参考图当条件——服装、脸型、风格会跟着走，
          但不是逐像素复刻，构图和姿态仍按提示词变。识别出来的是「看见了什么」，
          点「润色」把语序和能拍的细节补齐再生成。想照着这张只改局部，用首帧工作台的「局部重绘」。</p>
      </div>
    </div>

    {error && <p className="sf-error" role="alert">{error}</p>}
    {message && <p className="sf-fine ri-message">{message}
      {polishBackup !== null && <button type="button" className="ri-undo"
        title="把提示词换回润色前那段" onClick={undoPolish}>撤销润色</button>}
    </p>}

    {items.length > 0 && <section className="ri-results-wrap" aria-label="生成结果">
      <div className="ri-results-head">
        <span className="ri-results-title">生成结果 · {items.length} 张
          <small>一直留着，删掉才消失</small>
        </span>
        <span className="ri-results-actions">
          {pickMode
            ? <>
              <button type="button" className="ri-results-btn" disabled={deleting}
                onClick={pickAll}>{picked.length === items.length ? '取消全选' : '全选'}</button>
              <button type="button" className="ri-results-btn is-danger" disabled={!picked.length || deleting}
                onClick={() => setConfirmBatch(true)}>删除所选（{picked.length}）</button>
              <button type="button" className="ri-results-btn" disabled={deleting}
                onClick={() => { setPickMode(false); setPicked([]); }}>取消</button>
            </>
            : <button type="button" className="ri-results-btn" onClick={() => setPickMode(true)}>批量删除</button>}
        </span>
      </div>
      <div className="ri-results">
        {items.map(item => {
          const marked = picked.includes(item.id);
          const toggle = () => { if (pickMode) togglePick(item.id); };
          return <figure className={`ri-card is-${item.status}${marked ? ' is-picked' : ''}`} key={item.id}>
            {item.output
              ? <button type="button" className="ri-open"
                  aria-label={pickMode ? `选择 · 第 ${item.no} 张` : `看大图 · 第 ${item.no} 张`}
                  onClick={() => (pickMode ? toggle() : setPreviewId(item.id))}>
                <img src={mediaUrl(item.output)} alt="" />
                {pickMode
                  ? <span className={`ri-pick${marked ? ' is-on' : ''}`}>
                    {marked ? <CheckSquare size={15} /> : <Square size={15} />}
                  </span>
                  : <span className="ri-open-hint">点开看大图</span>}
              </button>
              : <div className="ri-placeholder" onClick={toggle}>
                {TERMINAL.includes(item.status)
                  ? <span>{item.status === 'failed' ? '失败' : '已停止'}</span>
                  : <>
                    <LoaderCircle size={16} className="sf-spin" />
                    <span>{item.status === 'queued' ? '排队中' : '生成中'}</span>
                    {item.progress && item.progress.max > 1 && <span className="ri-progress" role="progressbar"
                      aria-valuemin={0} aria-valuemax={Math.round(item.progress.max)}
                      aria-valuenow={Math.round(item.progress.value)} aria-label="生成进度">
                      <span className="ri-progress-bar"><span style={{ width: `${Math.max(4, item.progress.percent)}%` }} /></span>
                      <small>第 {Math.round(item.progress.value)}/{Math.round(item.progress.max)} 步 · {item.progress.percent}%</small>
                    </span>}
                  </>}
                {pickMode && <span className={`ri-pick${marked ? ' is-on' : ''}`}>
                  {marked ? <CheckSquare size={15} /> : <Square size={15} />}
                </span>}
              </div>}
            <figcaption>
              <span title={item.finishedAt ? `生成完成于 ${new Date(item.finishedAt * 1000).toLocaleTimeString('zh-CN')}` : undefined}>
                第 {item.no} 张{item.seconds ? ` · 用时 ${formatDuration(item.seconds)}` : ''}
              </span>
              {!pickMode && item.output && <span className="ri-card-actions">
                <button type="button" className="ri-card-action" title="精修这张：涂出五官/肤色要改的地方，低强度重绘"
                  aria-label={`精修 · 第 ${item.no} 张`} onClick={() => setRefineTarget(item)}>
                  <Wand2 size={12} />
                </button>
                <button type="button" className="ri-card-action" title="液化塑形：拉宽/收窄脸型、微调鼻梁与身形"
                  aria-label={`液化塑形 · 第 ${item.no} 张`} onClick={() => setLiquifyTarget(item)}>
                  <Scaling size={12} />
                </button>
                <button type="button" className="ri-card-action" title="存入素材库（首帧图参考）"
                  aria-label={`存入素材库 · 第 ${item.no} 张`} disabled={savingId === item.id}
                  onClick={() => void saveToLibrary(item)}>
                  {savingId === item.id ? <LoaderCircle size={12} className="sf-spin" /> : <Bookmark size={12} />}
                </button>
                <button type="button" className="ri-card-action is-danger" title="删除这张（进回收站）"
                  aria-label={`删除 · 第 ${item.no} 张`} onClick={() => setDeleteTarget(item)}>
                  <Trash2 size={12} />
                </button>
              </span>}
            </figcaption>
          </figure>;
        })}
      </div>
    </section>}

    {previewed && <div className="sf-modal-backdrop" role="dialog" aria-modal="true" aria-label="参考图结果预览"
      onClick={() => setPreviewId('')}>
      <div className="sf-modal sf-result-modal" onClick={event => event.stopPropagation()}>
        <button className="sf-modal-close" onClick={() => setPreviewId('')} aria-label="关闭"><X size={18} /></button>
        <p className="sf-step-label">参考图生成</p>
        <h2>{prompt.trim().length > 22 ? `${prompt.trim().slice(0, 22)}…` : (prompt.trim() || '生成结果')}</h2>
        <div className="sf-result-stage"><img src={mediaUrl(previewed.output)} alt="" /></div>
        <p className="ri-preview-prompt">{prompt.trim()}</p>
        <div className="sf-result-actions">
          <button type="button" className="sf-secondary" disabled={savingId === previewed.id}
            onClick={() => void saveToLibrary(previewed)}>
            <Bookmark size={13} /> {savingId === previewed.id ? '存入中…' : '存入素材库'}
          </button>
          <a className="sf-secondary" href={mediaUrl(previewed.output)} download={String(previewed.output).split(/[\\/]/).pop()}>
            <Download size={13} /> 下载
          </a>
          <a className="sf-secondary" href={mediaUrl(previewed.output)} target="_blank" rel="noreferrer">在新窗口打开</a>
          <button type="button" className="sf-secondary is-danger" onClick={() => setDeleteTarget(previewed)}>
            <Trash2 size={13} /> 删除
          </button>
        </div>
      </div>
    </div>}

    {confirmBatch && <div className="sf-modal-backdrop" role="dialog" aria-modal="true" aria-label="确认批量删除"
      onClick={() => { if (!deleting) setConfirmBatch(false); }}>
      <div className="sf-modal ri-confirm" onClick={event => event.stopPropagation()}>
        <h2>删除所选 {picked.length} 张？</h2>
        <p>这 {picked.length} 条任务记录会一起删除，产物与快照移入回收站，可以还原。没选中的不受影响。</p>
        <div className="sf-result-actions">
          <button type="button" className="sf-secondary" disabled={deleting}
            onClick={() => setConfirmBatch(false)}>取消</button>
          <button type="button" className="sf-primary" disabled={deleting} onClick={() => void removeMany(picked)}>
            {deleting ? <><LoaderCircle size={13} className="sf-spin" /> 删除中…</> : `删除 ${picked.length} 张`}
          </button>
        </div>
      </div>
    </div>}

    {deleteTarget && <div className="sf-modal-backdrop" role="dialog" aria-modal="true" aria-label="确认删除"
      onClick={() => { if (!deleting) setDeleteTarget(null); }}>
      <div className="sf-modal ri-confirm" onClick={event => event.stopPropagation()}>
        <h2>删除这张结果？</h2>
        <p>任务记录会一起删除，产物与快照移入回收站，可以还原。</p>
        <div className="sf-result-actions">
          <button type="button" className="sf-secondary" disabled={deleting}
            onClick={() => setDeleteTarget(null)}>取消</button>
          <button type="button" className="sf-primary" disabled={deleting} onClick={() => void removeItem()}>
            {deleting ? <><LoaderCircle size={13} className="sf-spin" /> 删除中…</> : '删除'}
          </button>
        </div>
      </div>
    </div>}
    {refineTarget?.output && projectId && <InpaintDialog projectId={projectId} imagePath={refineTarget.output}
      profile="general" initialStrength={0.35}
      onClose={() => setRefineTarget(null)}
      onDone={(path, _prompt, taskId) => {
        // 精修的结果就是一条新的关键帧任务：带上任务号，删除/归档都能正常走。
        setItems(current => [...current, { id: taskId || `refine-${Date.now()}`, status: 'succeeded',
          no: (seqRef.current += 1), output: path }]);
        setMessage('精修完成，已加到结果里');
        setRefineTarget(null);
      }} />}
    {liquifyTarget?.output && projectId && <LiquifyDialog projectId={projectId} imagePath={liquifyTarget.output}
      onClose={() => setLiquifyTarget(null)}
      onDone={(path, count, refine) => {
        const entry: Item = { id: `liquify-${Date.now()}`, status: 'succeeded',
          no: (seqRef.current += 1), output: path, ephemeral: true };
        setItems(current => [...current, entry]);
        setMessage(`液化完成（${count} 个调整中心），已存进作品素材并加到结果里`);
        setLiquifyTarget(null);
        // 「保存并精修」：液化定形后直接接着低强度重绘补质感，两步串成一条链。
        if (refine) setRefineTarget(entry);
      }} />}
  </section>;
}
