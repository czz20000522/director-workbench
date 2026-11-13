import { useEffect, useRef, useState } from 'react';
import { LoaderCircle, Plus, RotateCcw, Trash2, X } from 'lucide-react';
import { mediaUrl, request } from './api/client';
import './liquify.css';

/** 液化塑形：在图上点一个「调整中心」，用横向/纵向缩放微调五官和身形。
 *
 * 为什么不做自由笔刷：整张脸的「宽一点 / 鼻梁高一点」用参数化缩放更可控，也更容易
 * 撤销；缩放只在圆内生效、边缘用 smoothstep 收敛，所以不会出现硬接缝。
 * 输出是新的 PNG（原图不动），存进当前作品的 assets/liquify/ 下并登记成资产。
 */
type WarpPoint = { id: string; x: number; y: number; radius: number; sx: number; sy: number };

const clamp = (value: number, min: number, max: number) => Math.min(max, Math.max(min, value));

function sampleBilinear(src: Uint8ClampedArray, width: number, height: number, x: number, y: number, out: Uint8ClampedArray, index: number) {
  const x0 = clamp(Math.floor(x), 0, width - 1);
  const y0 = clamp(Math.floor(y), 0, height - 1);
  const x1 = clamp(x0 + 1, 0, width - 1);
  const y1 = clamp(y0 + 1, 0, height - 1);
  const fx = clamp(x - x0, 0, 1);
  const fy = clamp(y - y0, 0, 1);
  const at = (px: number, py: number) => (py * width + px) * 4;
  for (let channel = 0; channel < 4; channel += 1) {
    const a = src[at(x0, y0) + channel];
    const b = src[at(x1, y0) + channel];
    const c = src[at(x0, y1) + channel];
    const d = src[at(x1, y1) + channel];
    out[index + channel] = a * (1 - fx) * (1 - fy) + b * fx * (1 - fy) + c * (1 - fx) * fy + d * fx * fy;
  }
}

function warpOnce(image: ImageData, point: WarpPoint) {
  const { width, height, data } = image;
  const source = new Uint8ClampedArray(data);
  const radius = point.radius;
  const minX = clamp(Math.floor(point.x - radius), 0, width - 1);
  const maxX = clamp(Math.ceil(point.x + radius), 0, width - 1);
  const minY = clamp(Math.floor(point.y - radius), 0, height - 1);
  const maxY = clamp(Math.ceil(point.y + radius), 0, height - 1);
  const radiusSquared = radius * radius;
  for (let y = minY; y <= maxY; y += 1) {
    for (let x = minX; x <= maxX; x += 1) {
      const dx = x - point.x;
      const dy = y - point.y;
      const distanceSquared = dx * dx + dy * dy;
      if (distanceSquared > radiusSquared) continue;
      const distance = Math.sqrt(distanceSquared);
      const t = 1 - distance / radius;
      const weight = t * t * (3 - 2 * t);
      const sourceX = point.x + dx / (1 + (point.sx - 1) * weight);
      const sourceY = point.y + dy / (1 + (point.sy - 1) * weight);
      sampleBilinear(source, width, height, sourceX, sourceY, data, (y * width + x) * 4);
    }
  }
}

export default function LiquifyDialog({ projectId, imagePath, materialId, allowRefine = true, onClose, onDone }: {
  /** 有 materialId 时存回素材库（原图不动，新增一张）；否则存进该作品的 assets/liquify。 */
  projectId?: string;
  imagePath: string;
  materialId?: string;
  /** 有局部重绘入口的调用方传 true：保存后可以直接接着精修。 */
  allowRefine?: boolean;
  onClose: () => void;
  onDone: (path: string, points: number, refine?: boolean) => void;
}) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const sourceRef = useRef<ImageData | null>(null);
  const [ready, setReady] = useState(false);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [points, setPoints] = useState<WarpPoint[]>([]);
  const [activeId, setActiveId] = useState('');
  const [dirty, setDirty] = useState(false);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    let cancelled = false;
    const image = new Image();
    image.crossOrigin = 'anonymous';
    image.onload = () => {
      if (cancelled) return;
      canvas.width = image.naturalWidth;
      canvas.height = image.naturalHeight;
      const context = canvas.getContext('2d');
      if (!context) return;
      context.drawImage(image, 0, 0);
      sourceRef.current = context.getImageData(0, 0, canvas.width, canvas.height);
      setReady(true);
    };
    image.onerror = () => { if (!cancelled) setError('图片读取失败'); };
    // 素材库那边给的是接口地址（/api/...），作品里的产物给的是本机路径：
    // 前者直接当 src，后者要经 /media-file 代理。
    image.src = imagePath.startsWith('/api/') || /^https?:/i.test(imagePath) ? imagePath : (mediaUrl(imagePath) || '');
    return () => { cancelled = true; };
  }, [imagePath]);

  const paint = (list: WarpPoint[]) => {
    const canvas = canvasRef.current;
    const source = sourceRef.current;
    const context = canvas?.getContext('2d');
    if (!canvas || !source || !context) return;
    const image = new ImageData(new Uint8ClampedArray(source.data), source.width, source.height);
    for (const point of list) {
      if (Math.abs(point.sx - 1) < 0.005 && Math.abs(point.sy - 1) < 0.005) continue;
      warpOnce(image, point);
    }
    context.putImageData(image, 0, 0);
    setDirty(list.some(point => Math.abs(point.sx - 1) >= 0.005 || Math.abs(point.sy - 1) >= 0.005));
  };

  // 副作用（重绘画布）放在 setState 外面：StrictMode 下 updater 可能被调用两次，
  // 放在里面会把形变叠加两遍。
  const update = (id: string, patch: Partial<WarpPoint>) => {
    const next = points.map(point => point.id === id ? { ...point, ...patch } : point);
    setPoints(next);
    paint(next);
  };

  const addPoint = (event: React.MouseEvent<HTMLCanvasElement>) => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    const x = (event.clientX - rect.left) * (canvas.width / rect.width);
    const y = (event.clientY - rect.top) * (canvas.height / rect.height);
    const point: WarpPoint = { id: crypto.randomUUID(), x, y, radius: Math.round(Math.min(canvas.width, canvas.height) * 0.18), sx: 1, sy: 1 };
    setPoints(current => [...current, point]);
    setActiveId(point.id);
  };

  const removePoint = (id: string) => {
    const next = points.filter(point => point.id !== id);
    setPoints(next);
    paint(next);
    if (activeId === id) setActiveId('');
  };

  const reset = () => {
    setPoints([]); setActiveId('');
    const source = sourceRef.current;
    const context = canvasRef.current?.getContext('2d');
    if (source && context) context.putImageData(new ImageData(new Uint8ClampedArray(source.data), source.width, source.height), 0, 0);
    setDirty(false);
  };

  const save = async (refine = false) => {
    const canvas = canvasRef.current;
    if (!canvas || busy) return;
    setBusy(true); setError('');
    try {
      const blob = await new Promise<Blob | null>(resolve => canvas.toBlob(resolve, 'image/png'));
      if (!blob) throw new Error('导出 PNG 失败');
      const stamp = Date.now();
      if (materialId) {
        // 存回素材库：后端写在原图隔壁的同一个子库里，分类保持不变。
        const form = new FormData();
        form.append('file', blob, `liquify-${stamp}.png`);
        form.append('title', `液化 ${stamp}`);
        const data = await request<{ path: string }>(`/api/reference-library/materials/${encodeURIComponent(materialId)}/variants`,
          { init: { method: 'POST', body: form } });
        if (!data.path) throw new Error('保存没有返回路径');
        onDone(data.path, points.length, refine);
        return;
      }
      if (!projectId) throw new Error('缺少保存位置');
      const form = new FormData();
      form.append('files', new File([blob], `liquify-${stamp}.png`, { type: 'image/png' }), `assets/liquify/liquify-${stamp}.png`);
      const data = await request<{ uploaded: string[] }>(`/api/projects/${encodeURIComponent(projectId)}/upload`,
        { init: { method: 'POST', body: form } });
      const uploaded = data.uploaded?.[0];
      if (!uploaded) throw new Error('上传没有返回路径');
      onDone(uploaded, points.length, refine);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '保存失败');
    } finally { setBusy(false); }
  };

  const active = points.find(point => point.id === activeId) || null;
  const canvas = canvasRef.current;

  return <div className="sf-modal-backdrop" role="dialog" aria-modal="true" aria-label="液化塑形" onClick={busy ? undefined : onClose}>
    <div className="lq-modal" onClick={event => event.stopPropagation()}>
      <button className="sf-modal-close" onClick={onClose} aria-label="关闭" disabled={busy}><X size={18} /></button>
      <p className="sf-step-label">液化塑形</p>
      <h2>微调五官 / 身形</h2>
      <p className="sf-lede">在图上点一下放一个调整中心（比如脸颊、鼻梁、下颌），再拉横向/纵向比例。圆外不动，边缘自动过渡；原图不会被改。</p>
      <div className="lq-body">
        <div className="lq-stage">
          <canvas ref={canvasRef} onClick={addPoint} aria-label="液化画布" />
          {points.map(point => <span key={point.id} className={`lq-marker${point.id === activeId ? ' is-active' : ''}`}
            style={{ left: `${(point.x / Math.max(1, canvas?.width || 1)) * 100}%`, top: `${(point.y / Math.max(1, canvas?.height || 1)) * 100}%` }}
            aria-hidden="true" />)}
          {!ready && <p className="lq-loading"><LoaderCircle size={16} className="sf-spin" /> 正在读取图片…</p>}
        </div>
        <aside className="lq-side">
          <div className="lq-actions">
            <button type="button" className="sf-secondary" onClick={() => setActiveId('')} disabled={!points.length}>
              <Plus size={13} /> 点画面加中心
            </button>
            <button type="button" className="sf-secondary" onClick={reset} disabled={!points.length || busy}>
              <RotateCcw size={13} /> 全部还原
            </button>
          </div>
          {!points.length && <p className="lq-hint">还没有调整中心。点一下画面里的人脸或身体部位即可。</p>}
          {points.map((point, index) => <div key={point.id} className={point.id === activeId ? 'lq-point is-active' : 'lq-point'}
            onClick={() => setActiveId(point.id)}>
            <header><strong>中心 {index + 1}</strong>
              <button type="button" aria-label={`删除中心 ${index + 1}`} onClick={event => { event.stopPropagation(); removePoint(point.id); }}><Trash2 size={12} /></button>
            </header>
            <label>横向 {point.sx.toFixed(2)}×
              <input type="range" min={0.7} max={1.4} step={0.01} value={point.sx}
                onChange={event => update(point.id, { sx: Number(event.target.value) })} />
            </label>
            <label>纵向 {point.sy.toFixed(2)}×
              <input type="range" min={0.7} max={1.4} step={0.01} value={point.sy}
                onChange={event => update(point.id, { sy: Number(event.target.value) })} />
            </label>
            <label>半径 {Math.round(point.radius)} px
              <input type="range" min={20} max={Math.max(40, Math.round((canvas?.width || 800) * 0.6))} step={5} value={point.radius}
                onChange={event => update(point.id, { radius: Number(event.target.value) })} />
            </label>
          </div>)}
          {active && <p className="lq-hint">提示：把横向调到 1.10 左右是「稍微宽一点」；鼻梁想高一点就把中心放在鼻梁上、纵向略增。</p>}
          {error && <p className="sf-error" role="alert">{error}</p>}
          <div className="lq-foot">
            <button type="button" className="sf-ghost" onClick={onClose} disabled={busy}>取消</button>
            {allowRefine && <button type="button" className="sf-secondary" onClick={() => void save(true)} disabled={!ready || busy || !dirty}
              title="保存这张，并立刻涂出要补质感的地方做低强度重绘">
              {busy ? '正在保存…' : '保存并精修'}
            </button>}
            <button type="button" className="sf-primary" onClick={() => void save(false)} disabled={!ready || busy || !dirty}>
              {busy ? <><LoaderCircle size={13} className="sf-spin" /> 正在保存…</> : '保存为新图'}
            </button>
          </div>
        </aside>
      </div>
    </div>
  </div>;
}
