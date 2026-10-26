import { X } from 'lucide-react';
import type { ComfyInfo } from '../app/hooks';

export default function ResourceStatusPanel({ comfyOnline, comfyInfo, onClose }: { comfyOnline: boolean; comfyInfo: ComfyInfo; onClose: () => void }) {
  return <section className="director-resource-panel" aria-label="连接与资源" aria-live="polite">
    <div><strong>连接与资源</strong><p>{comfyOnline ? `ComfyUI ${comfyInfo.version ?? ''} · 队列运行 ${comfyInfo.queue_running ?? 0} · 等待 ${comfyInfo.queue_pending ?? 0} · 显存空闲约 ${comfyInfo.gpu_free_mib ?? '?'} MiB。` : '引擎待启动。第一次点生成时会自动拉起，约需 1–3 分钟；通过预检的请求可以先排队。'}</p></div>
    <button className="icon-button" aria-label="关闭资源状态" onClick={onClose}><X size={16} /></button>
  </section>;
}
