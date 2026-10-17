import { useEffect, useState } from 'react';

type Target = { series: string; work?: string; projectId?: string; title?: string; resource?: boolean };
type Plan = { plan_id: string; target: string; snapshot: { files: number; directories: number; bytes: number; links_or_reparse_points: number }; expires_at: string };

export default function DeleteDirectoryDialog({ target, onClose, onDeleted }: { target: Target; onClose: () => void; onDeleted: () => void }) {
  const [plan, setPlan] = useState<Plan | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const legacy = Boolean(target.projectId);
  const base = target.projectId ? `/api/projects/${encodeURIComponent(target.projectId)}/registration` : `/api/private/series/${encodeURIComponent(target.series)}${target.work ? `/works/${encodeURIComponent(target.work)}` : ''}`;
  const label = target.title || target.work || target.series;

  async function loadPlan() {
    setBusy(true); setError(''); setPlan(null);
    try {
      const response = await fetch(`${base}/deletion-plan`, { method: 'POST' });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || '无法核对删除范围');
      setPlan(data);
    } catch (cause) { setError(cause instanceof Error ? cause.message : '无法核对删除范围'); }
    finally { setBusy(false); }
  }

  useEffect(() => { if (!legacy) void loadPlan(); }, [base, legacy]);

  async function remove() {
    if ((!legacy && !plan) || busy) return;
    setBusy(true); setError('');
    try {
      const response = legacy ? await fetch(base, { method: 'DELETE' }) : await fetch(base, { method: 'DELETE', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ plan_id: plan!.plan_id, confirm: target.work || target.series }) });
      const data = await response.json();
      if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '删除失败，请重新核对');
      onDeleted();
    } catch (cause) { setError(cause instanceof Error ? cause.message : '删除失败'); }
    finally { setBusy(false); }
  }

  return <div className="director-delete-backdrop" role="presentation" onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}><section className="director-delete-dialog" role="dialog" aria-modal="true" aria-label={legacy ? '移出旧作品' : `删除${target.resource ? '系列资料' : target.work ? '作品' : '系列'}`}>
    <h2>{legacy ? `移出“${label}”？` : `删除${target.resource ? '系列资料目录' : target.work ? '作品目录' : '整个系列'}？`}</h2>
    <p>{legacy ? '这是早期登记在 D 盘的作品。移出后，它不再出现在工作台；D 盘原始素材、工作流和生成文件会保留。若 E 盘有同名旧目录，可在左侧单独删除。' : target.resource ? '此操作会删除 E 盘上的系列资料和其中的素材。其他作品如果引用了这些文件，相关预览或生成可能无法继续使用。' : `此操作会删除 E 盘上的目录和其中的素材。${target.work ? '作品的分镜、历史版本和任务文件也会失效。' : '系列内所有作品、分镜和素材都会失效。'}`}</p>
    {plan && !legacy && <div className="director-delete-impact"><strong>{plan.target}</strong><span>{plan.snapshot.files.toLocaleString()} 个文件 · {plan.snapshot.directories.toLocaleString()} 个目录 · {(plan.snapshot.bytes / 1024 ** 3).toFixed(2)} GiB</span>{plan.snapshot.links_or_reparse_points > 0 && <span>含 {plan.snapshot.links_or_reparse_points} 个链接或重解析项；请先检查目录。</span>}</div>}
    {error && <p role="alert" className="director-delete-error">{error}</p>}
    <div className="director-delete-actions"><button type="button" className="button secondary compact" onClick={onClose}>取消</button>{!legacy && <button type="button" className="button secondary compact" disabled={busy} onClick={() => void loadPlan()}>重新核对</button>}<button type="button" className="button danger compact" disabled={busy || (!legacy && (!plan || plan.snapshot.links_or_reparse_points > 0))} onClick={() => void remove()}>{busy ? '处理中…' : legacy ? '移出工作台' : '确认删除'}</button></div>
  </section></div>;
}
