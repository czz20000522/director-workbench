import { X } from 'lucide-react';
import type { ComfyInfo } from '../app/hooks';

export default function ResourceStatusPanel({ comfyOnline, comfyInfo, onClose }: { comfyOnline: boolean; comfyInfo: ComfyInfo; onClose: () => void }) {
  return <section className="director-resource-panel" aria-label="连接与资源" aria-live="polite">
    <div><strong>连接与资源</strong><p>{comfyOnline ? `ComfyUI ${comfyInfo.version ?? ''} · 队列运行 ${comfyInfo.queue_running ?? 0} · 等待 ${comfyInfo.queue_pending ?? 0} · 显存空闲约 ${comfyInfo.gpu_free_mib ?? '?'} MiB。` : '引擎暂时离线。通过预检的请求仍可排队，服务器会在资源恢复后继续处理。'}</p></div>
    <button className="icon-button" aria-label="关闭资源状态" onClick={onClose}><X size={16} /></button>
  </section>;
}
