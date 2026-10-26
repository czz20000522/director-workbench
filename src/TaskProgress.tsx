export type TaskProgress = {
  percent: number; value: number; max: number; state: string;
  eta_seconds?: number | null; seconds_per_step?: number | null; node_id?: string;
};

export function formatDuration(seconds?: number | null): string {
  if (seconds === null || seconds === undefined) return '';
  const whole = Math.max(0, Math.round(seconds));
  if (whole < 60) return `${whole} 秒`;
  const minutes = Math.floor(whole / 60);
  return whole % 60 ? `${minutes} 分 ${whole % 60} 秒` : `${minutes} 分`;
}

/** ComfyUI 的步级进度；没有收到 progress_state 时整块不渲染，不编造百分比。 */
export default function TaskProgress({ progress, label }: { progress?: TaskProgress | null; label?: string }) {
  if (!progress || !progress.max) return null;
  // 编码与解码节点只有一步，报"第 0/1 步 · 0%"是假精度；这类阶段只说在做什么。
  const singleStep = progress.max <= 1;
  const detail = singleStep ? '编码或解码中'
    : `第 ${Math.round(progress.value)}/${Math.round(progress.max)} 步 · ${progress.percent}%`
      + (progress.eta_seconds !== null && progress.eta_seconds !== undefined ? ` · 预计还需 ${formatDuration(progress.eta_seconds)}` : '');
  return <div className="sf-task-progress">
    <span className="sf-task-progress-bar" role="progressbar" aria-valuemin={0} aria-valuemax={progress.max}
      aria-valuenow={progress.value} aria-label={label || '生成进度'}>
      <span className={singleStep ? 'sf-tadw-progress-indeterminate' : ''}
        style={singleStep ? undefined : { width: `${Math.max(2, progress.percent)}%` }} />
    </span>
    <small>{label ? `${label} · ` : ''}{detail}</small>
  </div>;
}
