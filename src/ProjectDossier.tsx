import { useEffect, useState } from 'react';
import { FolderOpen, HardDrive, Trash2 } from 'lucide-react';
import { ApiError, request } from './api/client';

type FileItem = { path: string; size: number; modified: number };
type Archivable = { asset_id: string; target: string; library: string; title: string };
type Dossier = { project_id: string; title: string; series: string; workspace: string; workspace_exists: boolean;
  file_count: number; size_bytes: number; files: FileItem[]; archivable: Archivable[] };
type PlanEntry = { project_id: string; title: string; workspace: string; workspace_exists: boolean;
  snapshot: { file_count: number; size_bytes: number }; archivable?: Archivable[]; outputs?: string[] };
type Plan = { plan_id: string; entries: PlanEntry[]; preserved: string };

const kb = (size: number) => size >= 1048576 ? `${(size / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(size / 1024))} KB`;

export default function ProjectDossier({ projectId, onDeleted }: { projectId: string; onDeleted: () => void }) {
  const [data, setData] = useState<Dossier | null>(null);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [archivePick, setArchivePick] = useState<string[]>([]);
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');

  async function load() {
    try {
      setData(await request<Dossier>(`/api/projects/${encodeURIComponent(projectId)}/dossier`));
      setMessage('');
    } catch (reason) { setMessage(reason instanceof ApiError ? reason.message : '无法读取项目档案'); }
  }
  useEffect(() => { void load(); }, [projectId]);

  async function previewDelete() {
    if (busy) return;
    setBusy(true); setMessage('');
    try {
      setPlan(await request<Plan>('/api/projects/deletion-plan', { init: { method: 'POST' }, json: { project_ids: [projectId] } }));
      setArchivePick([]); setConfirmed(false);
    } catch (reason) { setMessage(reason instanceof ApiError ? reason.message : '无法生成删除预览'); }
    finally { setBusy(false); }
  }
  async function applyDelete() {
    if (!plan || !confirmed || busy) return;
    setBusy(true);
    try {
      await request('/api/projects/deletion', { init: { method: 'POST' }, json: {
        plan_id: plan.plan_id, project_ids: [projectId], archive_asset_ids: archivePick, confirm: true } });
      setPlan(null);
      onDeleted();
    } catch (reason) { setMessage(reason instanceof ApiError ? reason.message : '删除未完成'); }
    finally { setBusy(false); }
  }
  const items = plan?.entries[0]?.archivable || [];

  return <div className="sf-step-panel sf-tool-panel pd-panel">
    <p className="sf-step-label">项目工具</p>
    <h1>项目档案</h1>
    <p className="sf-lede">这个项目的全部中间文件都在它的专属工作区里；归档进「作品/素材库」的东西不属于这里，删项目也不会被带走。</p>
    {message && <p className="sf-post-message" role="status" aria-live="polite">{message}</p>}
    {data && <>
      <div className="pd-grid">
        <div className="pd-card"><HardDrive size={16} /><div><strong>{data.file_count} 个文件 · {kb(data.size_bytes)}</strong><span>{data.workspace_exists ? data.workspace : '工作区目录已不存在'}</span></div></div>
        <div className="pd-card"><FolderOpen size={16} /><div><strong>{data.title}</strong><span>{data.series || '未命名系列'} · 项目 ID {data.project_id}</span></div></div>
      </div>
      <div className="sf-post-subblock">
        <div className="sf-post-section-head"><h3 className="sf-post-subtitle">最近文件</h3><p className="sf-post-meta">最多 40 个</p></div>
        {!data.files.length ? <p className="sf-post-hint">工作区还是空的。</p> : <ul className="sf-post-history">{data.files.map(file => <li key={file.path}>
          <span>{file.path}</span><time>{kb(file.size)} · {new Date(file.modified * 1000).toLocaleString('zh-CN')}</time>
        </li>)}</ul>}
      </div>
      <div className="sf-post-actions">
        <button className="button secondary compact" type="button" disabled={busy} onClick={() => void load()}>刷新档案</button>
        <button className="button secondary compact" type="button" disabled={busy} onClick={() => void previewDelete()}><Trash2 size={14} /> 删除此项目…</button>
      </div>
    </>}
    {plan && <div className="sf-modal-mask" role="dialog" aria-modal="true" aria-label="删除项目预览">
      <div className="sf-modal">
        <h2>删除项目预览</h2>
        <p className="sf-lede">{plan.preserved}</p>
        {plan.entries.map(entry => <ul className="sf-del-list" key={entry.project_id}>
          <li><strong>{entry.title}</strong>
            <span>{entry.workspace_exists ? `${entry.snapshot.file_count} 个文件 · ${kb(entry.snapshot.size_bytes)}` : '工作区已不存在，仅删除登记与任务记录'}</span>
            <span className="sf-del-sample">{entry.workspace}</span>
          </li>
        </ul>)}
        {items.length > 0 && <div className="sf-del-archive">
          <p className="sf-del-archive-head">删除前归档 · 可选，默认不勾；勾中的会移进素材库/作品并登记</p>
          {items.map(item => <label className="sf-del-confirm" key={item.asset_id}>
            <input type="checkbox" checked={archivePick.includes(item.asset_id)}
              onChange={() => setArchivePick(current => current.includes(item.asset_id) ? current.filter(id => id !== item.asset_id) : [...current, item.asset_id])} />
            存进{item.library}：{item.title}
          </label>)}
        </div>}
        <label className="sf-del-confirm"><input type="checkbox" checked={confirmed} onChange={event => setConfirmed(event.target.checked)} /> 我已核对以上内容，确认删除且不可恢复</label>
        <div className="sf-step-actions">
          <button className="sf-ghost" disabled={busy} onClick={() => setPlan(null)}>取消</button>
          <button className="sf-primary" disabled={busy || !confirmed} onClick={() => void applyDelete()}>{busy ? '删除中…' : '确认删除'}</button>
        </div>
      </div>
    </div>}
  </div>;
}
