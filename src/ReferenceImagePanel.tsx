import { useEffect, useRef, useState } from 'react';
import { Bookmark, Download, ImagePlus, LoaderCircle, Sparkles, Trash2, Wand2, X } from 'lucide-react';
import { mediaUrl, request } from './api/client';
import { invalidateMaterialIndex } from './ReferenceSlotField';
import { notifyLibraryChanged } from './ReferenceLibrary';
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

type Task = {
  id: string;
  status: string;
  error?: string;
  result?: { output?: string };
};

type Item = { id: string; status: string; output?: string; error?: string };

const TERMINAL = ['succeeded', 'failed', 'stopped'];

const randomSeed = () => crypto.getRandomValues(new Uint32Array(1))[0];

const SIZES = {
  portrait: { width: 768, height: 1344 },
  landscape: { width: 1344, height: 768 },
  square: { width: 1024, height: 1024 },
} as const;

export default function ReferenceImagePanel({ projectId, onComplete }: {
  projectId?: string;
  onComplete?: () => void;
}) {
  const [workspaceId, setWorkspaceId] = useState(projectId || '');
  const [image, setImage] = useState('');
  const [prompt, setPrompt] = useState('');
  const [count, setCount] = useState(2);
  // 参考图可能是横的、也可能是竖的：固定成竖版会把构图切掉，所以给三档。
  const [aspect, setAspect] = useState<'portrait' | 'landscape' | 'square'>('portrait');
  const [refine, setRefine] = useState(true);
  const [describing, setDescribing] = useState(false);
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
  const [deleting, setDeleting] = useState(false);
  const aliveRef = useRef(true);

  useEffect(() => () => { aliveRef.current = false; }, []);
  useEffect(() => { if (projectId && !workspaceId) setWorkspaceId(projectId); }, [projectId, workspaceId]);

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
      setImage(path); setPrompt(''); setItems([]); setMessage('');
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
      setMessage(`识别完成（${data.provider === 'online' ? '在线' : '本地'} · ${(data.elapsed_ms / 1000).toFixed(1)} 秒），可以直接改，再点生成`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '识别失败');
      setMessage('');
    } finally { setDescribing(false); }
  };

  /** 提交完之后才轮询：否则「items 还是空的」会被当成「全部完成」，直接收工。 */
  const poll = async (pending: string[]) => {
    let ids = [...pending];
    while (ids.length && aliveRef.current) {
      await new Promise(resolve => setTimeout(resolve, 2500));
      if (!aliveRef.current) return;
      const settled = await Promise.all(ids.map(async id => {
        try {
          const task = await request<Task>(`/api/tasks/${encodeURIComponent(id)}`);
          return { id, status: task.status, output: task.result?.output, error: task.error };
        } catch { return { id, status: '', output: undefined, error: undefined }; }
      }));
      if (!aliveRef.current) return;
      setItems(current => current.map(item => {
        const next = settled.find(entry => entry.id === item.id);
        return next && next.status ? { ...item, ...next } : item;
      }));
      ids = settled.filter(entry => entry.status && !TERMINAL.includes(entry.status)).map(entry => entry.id);
    }
    if (!aliveRef.current) return;
    setBusy(false);
    setMessage('全部生成完成');
    onComplete?.();
  };

  const generate = async () => {
    if (!image) { setError('先选一张图'); return; }
    if (!prompt.trim()) { setError('先写或识别一段提示词'); return; }
    const total = Math.min(8, Math.max(1, Math.round(count)));
    setBusy(true); setError(''); setMessage(`提交 ${total} 个任务…`);
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
        created.push({ id: task.id, status: task.status });
        setItems([...created]);
      }
      setMessage(`已提交 ${total} 个，排队生成中…`);
      await poll(created.map(item => item.id));
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
  const removeItem = async () => {
    if (!deleteTarget) return;
    const target = deleteTarget;
    const index = items.findIndex(entry => entry.id === target.id);
    setDeleting(true); setError('');
    // 先把卡片摘掉再发删除请求：Windows 上 <img> 还挂着这张图时文件是打开的，
    // 回收站会报「共享冲突」，重试也过不去。摘掉 DOM 后句柄就放开了。
    setItems(current => current.filter(entry => entry.id !== target.id));
    if (previewId === target.id) setPreviewId('');
    setDeleteTarget(null);
    try {
      // 等一帧：React 的更新是批量刷的，fetch 抢在渲染前面发出去的话，
      // <img> 还挂在 DOM 上，Windows 就一直报共享冲突。
      await new Promise<void>(resolve => requestAnimationFrame(() => resolve()));
      const plan = await request<{ plan_id: string; entries: { task_id: string }[] }>(
        '/api/tasks/deletion-plan', { json: { task_ids: [target.id] } });
      const result = await request<{ failed?: string[] }>('/api/tasks/deletion', {
        json: { plan_id: plan.plan_id, task_ids: plan.entries.map(entry => entry.task_id), confirm: true },
      });
      setMessage(result.failed?.length
        ? '任务已删除，但有文件没能移入回收站（多半刚写完还被占用），稍后再删一次'
        : '已删除，产物已移入回收站，可从回收站还原');
      onComplete?.();
    } catch (reason) {
      // 删失败就把卡片放回去——不要让人以为已经删掉了。
      setItems(current => {
        const next = [...current];
        next.splice(Math.max(0, index), 0, target);
        return next;
      });
      setError(reason instanceof Error ? reason.message : '删除失败');
      onComplete?.();
    } finally { setDeleting(false); }
  };

  const done = items.filter(item => item.status === 'succeeded').length;
  const previewed = items.find(item => item.id === previewId && item.output) || null;

  return <section className="ri-panel" aria-label="参考图生成">
    <div className="ri-grid">
      <div className="ri-source">
        <span className="ri-label">参考图</span>
        {image
          ? <div className="ri-preview">
            <img src={mediaUrl(image)} alt="" />
            <button type="button" className="ri-remove" aria-label="移除参考图"
              onClick={() => { setImage(''); setPrompt(''); setItems([]); }}><X size={12} /></button>
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
            <div className="ri-vision-tabs" role="tablist" aria-label="识图通道">
              <button type="button" role="tab" aria-selected={visionProvider === 'local'}
                onClick={() => setVisionProvider('local')}>本地</button>
              <button type="button" role="tab" aria-selected={visionProvider === 'online'}
                onClick={() => setVisionProvider('online')}>在线</button>
            </div>
            <button type="button" className="ri-identify" disabled={!image || describing || busy}
              title={visionProvider === 'online' ? '在线识图走你配的接口；平台规则可能拒答成人向图片' : '本机模型识图，不联网、不拒答成人向'}
              onClick={() => void describe()}>
              {describing ? <><LoaderCircle size={12} className="sf-spin" /> 识别中…</> : <><Sparkles size={12} /> 识别内容</>}
            </button>
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
          {busy ? <><LoaderCircle size={13} className="sf-spin" /> 生成中 {done}/{items.length}</> : <><Wand2 size={13} /> 生成 {count} 张</>}
        </button>
        <p className="ri-note">这就是「图生图」：模型把参考图当条件——服装、脸型、风格会跟着走，
          但不是逐像素复刻，构图和姿态仍按提示词变。想照着这张只改局部，用首帧工作台的「局部重绘」。</p>
      </div>
    </div>

    {error && <p className="sf-error" role="alert">{error}</p>}
    {message && <p className="sf-fine ri-message">{message}</p>}

    {items.length > 0 && <div className="ri-results">
      {items.map((item, index) => <figure className={`ri-card is-${item.status}`} key={item.id}>
        {item.output
          ? <button type="button" className="ri-open" aria-label={`看大图 · 第 ${index + 1} 张`}
              onClick={() => setPreviewId(item.id)}>
            <img src={mediaUrl(item.output)} alt="" />
            <span className="ri-open-hint">点开看大图</span>
          </button>
          : <div className="ri-placeholder">
            {TERMINAL.includes(item.status)
              ? <span>{item.status === 'failed' ? '失败' : '已停止'}</span>
              : <><LoaderCircle size={16} className="sf-spin" /><span>{item.status === 'queued' ? '排队' : '生成中'}</span></>}
          </div>}
        <figcaption>
          <span>第 {index + 1} 张</span>
          {item.output && <span className="ri-card-actions">
            <button type="button" className="ri-card-action" title="存入素材库（首帧图参考）"
              aria-label={`存入素材库 · 第 ${index + 1} 张`} disabled={savingId === item.id}
              onClick={() => void saveToLibrary(item)}>
              {savingId === item.id ? <LoaderCircle size={12} className="sf-spin" /> : <Bookmark size={12} />}
            </button>
            <button type="button" className="ri-card-action is-danger" title="删除这张（进回收站）"
              aria-label={`删除 · 第 ${index + 1} 张`} onClick={() => setDeleteTarget(item)}>
              <Trash2 size={12} />
            </button>
          </span>}
        </figcaption>
      </figure>)}
    </div>}

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
  </section>;
}
