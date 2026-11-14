import { useEffect, useRef, useState } from 'react';
import type { PointerEvent as ReactPointerEvent } from 'react';
import { Eraser, LoaderCircle, Paintbrush, Trash2, X } from 'lucide-react';
import { mediaUrl, request } from './api/client';
import ProgressBar, { type TaskProgress } from './TaskProgress';
import TranslateButton from './TranslateButton';

/** 首帧局部重绘。
 *
 * 之前「继续改这张」是把整张图塞回底图槽重画，脸和背景都会漂。这里改成
 * 在图上直接涂：涂到的地方才重画，没涂的地方用原图像素回贴。后端走
 * `SetLatentNoiseMask + ImageCompositeMasked`，不是把整图重新生成一遍。
 */

type Task = { id: string; status: string; error?: string; progress?: TaskProgress; result?: unknown };

const statusLabels: Record<string, string> = {
  scheduler_waiting: '等待调度', batch_waiting: '批次等待', preparing: '准备中', submitting: '提交中',
  queued: '排队中', running: '重绘中', succeeded: '已完成', failed: '重绘失败', stopped: '已停止',
  needs_reconcile: '需要核对结果',
};
const STRENGTHS = [
  { value: 0.55, label: '轻微', note: '换表情、补细节' },
  { value: 0.75, label: '中等', note: '换衣服、换发型' },
  { value: 0.9, label: '大幅', note: '整块重画' },
];

function readOutput(result: unknown): string {
  let value = result;
  if (typeof value === 'string') {
    try { value = JSON.parse(value); } catch { return ''; }
  }
  if (!value || typeof value !== 'object') return '';
  const record = value as Record<string, unknown>;
  for (const key of ['output', 'native_output', 'published_native_output']) {
    if (typeof record[key] === 'string' && record[key]) return record[key] as string;
  }
  return '';
}

export default function InpaintDialog({ projectId, imagePath, profile, initialStrength, onClose, onDone }: {
  projectId: string;
  imagePath: string;
  profile: 'general' | 'adult-realism' | 'adult-anime';
  /** 「精修五官」这类低强度场景：降噪给低一点，只补质感不换人；默认 0.75。 */
  initialStrength?: number;
  onClose: () => void;
  onDone: (path: string, prompt: string, taskId?: string) => void;
}) {
  const baseRef = useRef<HTMLCanvasElement | null>(null);
  const maskRef = useRef<HTMLCanvasElement | null>(null);
  const drawing = useRef(false);
  const last = useRef<{ x: number; y: number } | null>(null);
  const [size, setSize] = useState({ width: 0, height: 0 });
  const [brush, setBrush] = useState(72);
  const [erase, setErase] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [prompt, setPrompt] = useState('');
  const [strength, setStrength] = useState(initialStrength ?? 0.75);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState('');
  const [progress, setProgress] = useState<TaskProgress | null>(null);
  const [error, setError] = useState('');

  useEffect(() => {
    const base = baseRef.current;
    const mask = maskRef.current;
    if (!base || !mask) return;
    let cancelled = false;
    const image = new Image();
    image.onload = () => {
      if (cancelled) return;
      const width = image.naturalWidth;
      const height = image.naturalHeight;
      base.width = width; base.height = height; mask.width = width; mask.height = height;
      const context = base.getContext('2d');
      if (context) context.drawImage(image, 0, 0);
      setSize({ width, height });
    };
    image.onerror = () => { if (!cancelled) setError('底图读取失败，关掉重试一次。'); };
    const source = mediaUrl(imagePath);
    if (!source) { setError('底图路径为空'); return () => { cancelled = true; }; }
    image.src = source;
    return () => { cancelled = true; };
  }, [imagePath]);

  useEffect(() => {
    const close = (event: KeyboardEvent) => { if (event.key === 'Escape' && !busy) onClose(); };
    window.addEventListener('keydown', close);
    return () => window.removeEventListener('keydown', close);
  }, [busy, onClose]);

  const point = (event: ReactPointerEvent<HTMLCanvasElement>) => {
    const canvas = maskRef.current;
    if (!canvas) return { x: 0, y: 0 };
    const rect = canvas.getBoundingClientRect();
    return {
      x: (event.clientX - rect.left) * (canvas.width / rect.width),
      y: (event.clientY - rect.top) * (canvas.height / rect.height),
    };
  };

  const stroke = (from: { x: number; y: number }, to: { x: number; y: number }) => {
    const context = maskRef.current?.getContext('2d');
    if (!context) return;
    context.globalCompositeOperation = erase ? 'destination-out' : 'source-over';
    context.strokeStyle = 'rgba(255, 62, 62, .55)';
    context.fillStyle = 'rgba(255, 62, 62, .55)';
    context.lineWidth = brush;
    context.lineCap = 'round';
    context.lineJoin = 'round';
    context.beginPath();
    context.moveTo(from.x, from.y);
    context.lineTo(to.x, to.y);
    context.stroke();
    context.beginPath();
    context.arc(to.x, to.y, brush / 2, 0, Math.PI * 2);
    context.fill();
    setDirty(true);
  };

  const down = (event: ReactPointerEvent<HTMLCanvasElement>) => {
    if (busy) return;
    event.currentTarget.setPointerCapture(event.pointerId);
    drawing.current = true;
    const start = point(event);
    last.current = start;
    stroke(start, start);
  };
  const move = (event: ReactPointerEvent<HTMLCanvasElement>) => {
    if (!drawing.current) return;
    const now = point(event);
    stroke(last.current || now, now);
    last.current = now;
  };
  const up = () => { drawing.current = false; last.current = null; };

  const clear = () => {
    const canvas = maskRef.current;
    const context = canvas?.getContext('2d');
    if (canvas && context) context.clearRect(0, 0, canvas.width, canvas.height);
    setDirty(false);
  };

  const exportMask = (): string => {
    const canvas = maskRef.current;
    if (!canvas) return '';
    const source = canvas.getContext('2d')?.getImageData(0, 0, canvas.width, canvas.height);
    if (!source) return '';
    // 画布上是半透明红；导出成纯黑白，白=要重画。灰色会被当成半强度加噪。
    const output = new ImageData(canvas.width, canvas.height);
    for (let index = 0; index < source.data.length; index += 4) {
      const painted = source.data[index + 3] > 12;
      output.data[index] = painted ? 255 : 0;
      output.data[index + 1] = painted ? 255 : 0;
      output.data[index + 2] = painted ? 255 : 0;
      output.data[index + 3] = 255;
    }
    const out = document.createElement('canvas');
    out.width = canvas.width; out.height = canvas.height;
    out.getContext('2d')?.putImageData(output, 0, 0);
    return out.toDataURL('image/png');
  };

  const submit = async () => {
    if (!prompt.trim()) { setError('写一句这里要改成什么'); return; }
    if (!dirty) { setError('先在图上涂出要改的区域'); return; }
    setBusy(true); setError(''); setProgress(null); setStatus('提交重绘任务…');
    try {
      const mask = exportMask();
      if (!mask) throw new Error('蒙版导出失败');
      const seed = Math.floor(Math.random() * 4294967295);
      const created = await request<Task & { task?: Task }>(`/api/projects/${encodeURIComponent(projectId)}/keyframe-tasks`, {
        json: {
          mode: 'inpaint', prompt: prompt.trim(), creative_profile: profile,
          reference_image: imagePath, mask_image: mask,
          width: size.width, height: size.height, steps: 8, denoise: strength,
          seed, idempotency_key: `inpaint-${crypto.randomUUID()}`,
        },
      });
      const taskId = created.id || created.task?.id;
      if (!taskId) throw new Error('任务未返回编号');
      let output = '';
      for (let attempt = 0; attempt < 150; attempt += 1) {
        const detail = await request<Task & { task?: Task }>(`/api/tasks/${encodeURIComponent(taskId)}`);
        const current = detail.task || detail;
        setStatus(statusLabels[current.status] || current.status);
        setProgress(current.progress || null);
        if (['succeeded', 'failed', 'stopped', 'needs_reconcile'].includes(current.status)) {
          if (current.status !== 'succeeded') throw new Error(current.error || `局部重绘${statusLabels[current.status] || current.status}`);
          output = readOutput(current.result);
          break;
        }
        await new Promise(resolve => setTimeout(resolve, 2000));
      }
      if (!output) throw new Error('任务成功但没有拿到图片路径');
      onDone(output, prompt.trim(), String(taskId));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '局部重绘失败');
      setStatus('');
    } finally { setBusy(false); }
  };

  return <div className="ffs-inpaint-backdrop" role="dialog" aria-modal="true" aria-label="局部重绘">
    <div className="ffs-inpaint">
      <header className="ffs-inpaint-head">
        <div><strong>局部重绘</strong><span>涂到哪里改哪里；没涂到的地方按原图像素保留。整件换掉就把整片涂满，只画一条细线就只会改那一道。</span></div>
        <button type="button" className="rl-icon-button" aria-label="关闭局部重绘" disabled={busy} onClick={onClose}><X size={15} /></button>
      </header>
      <div className="ffs-inpaint-stage">
        <canvas ref={baseRef} className="ffs-inpaint-base" aria-hidden="true" />
        <canvas ref={maskRef} className="ffs-inpaint-mask" aria-label="蒙版画布"
          onPointerDown={down} onPointerMove={move} onPointerUp={up} onPointerCancel={up} />
        {!size.width && <p className="ffs-inpaint-loading"><LoaderCircle size={16} className="sf-spin" /> 正在读取底图…</p>}
      </div>
      <div className="ffs-inpaint-tools">
        <label className="ffs-inpaint-brush">笔刷 <input type="range" min={20} max={180} step={10} value={brush}
          disabled={busy} onChange={event => setBrush(Number(event.target.value))} /><span>{brush}px</span></label>
        <button type="button" aria-pressed={!erase} className={!erase ? 'selected' : ''} disabled={busy}
          onClick={() => setErase(false)}><Paintbrush size={13} /> 画笔</button>
        <button type="button" aria-pressed={erase} className={erase ? 'selected' : ''} disabled={busy}
          onClick={() => setErase(true)}><Eraser size={13} /> 橡皮</button>
        <button type="button" disabled={busy || !dirty} onClick={clear}><Trash2 size={13} /> 清空蒙版</button>
      </div>
      <label className="sf-field"><span className="tb-row">这里改成什么
        <TranslateButton text={prompt} disabled={busy} onTranslated={setPrompt} />
      </span>
        <textarea rows={3} value={prompt} disabled={busy} onChange={event => setPrompt(event.target.value)}
          placeholder="只写被涂区域的新内容，例如：换成白色高领毛衣，领口有细针织纹理。" />
      </label>
      <div className="sf-field"><span>改动幅度</span>
        <div className="clarity-levels" role="group" aria-label="改动幅度">
          {STRENGTHS.map(option => <button key={option.value} type="button" aria-pressed={strength === option.value}
            disabled={busy} onClick={() => setStrength(option.value)}>
            <strong>{option.label}</strong><small>{option.note}</small>
          </button>)}
        </div>
        <small className="sf-fine">幅度越大越偏离原图；0.75 能换衣服和发型，同时保住脸与背景。</small>
      </div>
      {error && <p className="sf-error" role="alert">{error}</p>}
      {status && <p className="qt-status">{busy && <LoaderCircle size={14} className="sf-spin" />}{status}</p>}
      <ProgressBar progress={busy ? progress : null} />
      <footer className="ffs-inpaint-foot">
        <button type="button" className="sf-secondary" disabled={busy} onClick={onClose}>取消</button>
        <button type="button" className="sf-primary" disabled={busy || !dirty || !prompt.trim()} onClick={() => void submit()}>
          {busy ? '重绘中…' : '开始重绘'}
        </button>
      </footer>
    </div>
  </div>;
}
