export type QueueReceipt = { id: string; asset_id: string; status: string; queue?: { state: string; reason?: { code: string; message?: string } | null; position?: number | null } };
export default function TaskQueueStatus({ task }: { task: QueueReceipt }) {
  if (!task.queue || ['terminal', 'executing'].includes(task.queue.state)) return null;
  return <div className="director-readiness has-gaps" role="status" aria-label={`任务排队 ${task.asset_id}`}><strong>{task.asset_id} · 已接受任务</strong><span>{task.queue.reason?.message || `正在等待调度（${task.queue.reason?.code ?? task.queue.state}）`}{task.queue.position != null ? ` · 队列位置 ${task.queue.position}` : ''}</span><small>任务边界按用户轮换；位置随新任务和依赖变化，不提供确定耗时。</small></div>;
}
