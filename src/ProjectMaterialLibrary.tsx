import { useEffect, useMemo, useState } from 'react';
import { Copy, Film, FolderOpen, LoaderCircle, Search } from 'lucide-react';

type Project = { id: string; title: string; series?: string };
type Material = { id: string; title: string; kind: string; path: string; size_bytes: number };
const kinds = [['all', '全部'], ['image', '图片'], ['audio', '音频'], ['video', '视频']];

async function readResponse(response: Response) {
  const payload = await response.json();
  if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : '素材读取或复制失败，请重试。');
  return payload;
}

export default function ProjectMaterialLibrary({ projectId, onRegistered }: {
  projectId: string; onRegistered: (count: number) => void;
}) {
  const [projects, setProjects] = useState<Project[]>([]);
  const [series, setSeries] = useState('');
  const [sourceId, setSourceId] = useState('');
  const [materials, setMaterials] = useState<Material[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [query, setQuery] = useState('');
  const [kind, setKind] = useState('all');
  const [loading, setLoading] = useState(true);
  const [projectsLoading, setProjectsLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [refresh, setRefresh] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setProjectsLoading(true); setError(''); setProjects([]); setSourceId(''); setSelected(new Set());
    fetch('/api/projects', { signal: controller.signal }).then(readResponse).then(payload => {
      if (controller.signal.aborted) return;
      const available = (payload.projects as Project[] ?? []).filter(item => item.id !== projectId);
      setProjects(available); setSeries(''); setSourceId(available[0]?.id ?? '');
    }).catch(reason => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : '作品列表读取失败'); })
      .finally(() => { if (!controller.signal.aborted) setProjectsLoading(false); });
    return () => controller.abort();
  }, [projectId, refresh]);

  useEffect(() => {
    const controller = new AbortController();
    setMaterials([]); setSelected(new Set()); setNotice('');
    if (!sourceId) { setLoading(false); return () => controller.abort(); }
    setLoading(true); setError('');
    fetch(`/api/projects/${encodeURIComponent(sourceId)}/reusable-materials`, { signal: controller.signal })
      .then(readResponse).then(payload => { if (!controller.signal.aborted) setMaterials(payload.materials ?? []); })
      .catch(reason => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : '作品素材读取失败'); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [sourceId, refresh]);

  const seriesNames = [...new Set(projects.map(project => project.series || '未分类系列'))];
  const sourceProjects = projects.filter(project => !series || (project.series || '未分类系列') === series);
  const visible = useMemo(() => materials.filter(item => (kind === 'all' || item.kind === kind) && item.title.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase())), [materials, kind, query]);
  const allVisibleSelected = visible.length > 0 && visible.every(item => selected.has(item.id));

  async function copySelected() {
    if (!sourceId || !selected.size || selected.size > 128 || busy) return;
    setBusy(true); setError(''); setNotice('');
    try {
      const payload = await readResponse(await fetch(`/api/projects/${encodeURIComponent(projectId)}/materials/reuse`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ source_project_id: sourceId, material_ids: [...selected] }),
      }));
      const count = Array.isArray(payload.registered) ? payload.registered.length : 0;
      const existing = Number(payload.already_registered) || 0;
      setNotice(count ? `已复制 ${count} 个素材到当前作品。${existing ? `另有 ${existing} 个已存在，已跳过。` : ''}` : '所选素材已在当前作品中，无需重复复制。');
      setSelected(new Set()); onRegistered(count);
    } catch (reason) { setError(reason instanceof Error ? reason.message : '复制失败，请重试。'); }
    finally { setBusy(false); }
  }

  return <div className="project-material-library">
    <div className="project-material-filters">
      <label>来源系列<select value={series} disabled={busy || projectsLoading} onChange={event => {
        const next = event.target.value; setSeries(next);
        setSourceId(projects.find(project => !next || (project.series || '未分类系列') === next)?.id ?? '');
      }}><option value="">全部系列</option>{seriesNames.map(name => <option key={name}>{name}</option>)}</select></label>
      <label>来源作品<select value={sourceId} disabled={busy || projectsLoading || !sourceProjects.length} onChange={event => setSourceId(event.target.value)}>
        {!sourceProjects.length && <option value="">暂无其他作品</option>}{sourceProjects.map(project => <option key={project.id} value={project.id}>{project.title}</option>)}
      </select></label>
      <label className="project-material-search"><Search size={15} /><input aria-label="搜索其他作品素材" placeholder="搜索素材名称" value={query} onChange={event => setQuery(event.target.value)} /></label>
    </div>
    <div className="project-material-filterbar"><div className="material-kind-filter" aria-label="其他作品素材类型">{kinds.map(([id, title]) => <button key={id} aria-pressed={kind === id} className={kind === id ? 'active' : ''} onClick={() => setKind(id)}>{title}</button>)}</div>
      <label className="project-material-select-all"><input type="checkbox" checked={allVisibleSelected} disabled={busy || loading || !visible.length} onChange={() => setSelected(previous => {
        const next = new Set(previous); visible.forEach(item => allVisibleSelected ? next.delete(item.id) : next.add(item.id)); return next;
      })} />选择当前结果</label></div>
    <p className="project-material-hint">复制到当前作品独立保存，保留来源信息；审核结果需重新确认。每次最多128个。</p>
    {error && <div className="material-notice error" role="alert">{error} <button className="text-button" disabled={busy} onClick={() => setRefresh(value => value + 1)}>重试</button></div>}
    <div className="project-material-grid" aria-busy={loading || projectsLoading}>
      {loading || projectsLoading ? <div className="material-empty" role="status"><LoaderCircle size={22} className="spin" />正在读取作品素材</div> : error ? null : !visible.length ? <div className="material-empty"><FolderOpen size={24} />{!projects.length ? '还没有其他作品可供复用' : !materials.length ? '此作品暂无可复用素材' : '没有匹配素材，请调整搜索或类型'}</div> : visible.map(item => <article key={item.id} className={selected.has(item.id) ? 'selected' : ''}>
        <div className="project-material-preview">{item.kind === 'image' ? <img src={`/media-file?path=${encodeURIComponent(item.path)}`} alt={item.title} loading="lazy" /> : item.kind === 'audio' ? <audio aria-label={`试听 ${item.title}`} src={`/media-file?path=${encodeURIComponent(item.path)}`} controls preload="none" /> : <Film size={32} />}</div>
        <label className="project-material-choice"><input type="checkbox" checked={selected.has(item.id)} disabled={busy} onChange={() => setSelected(previous => { const next = new Set(previous); if (next.has(item.id)) next.delete(item.id); else next.add(item.id); return next; })} /><span><strong>{item.title}</strong><small>{kinds.find(([id]) => id === item.kind)?.[1] ?? '素材'} · {(item.size_bytes / 1024 / 1024).toFixed(1)} MB</small></span></label>
      </article>)}
    </div>
    {notice && <div className="material-notice" role="status">{notice}</div>}
    <footer className="material-bridge-foot"><span>{selected.size ? `已选择 ${selected.size} 个素材${[...selected].some(id => !visible.some(item => item.id === id)) ? '（包含筛选隐藏项）' : ''}${selected.size > 128 ? '，超过单次128个上限，请取消部分选择' : ''}` : '选择素材后复制到当前作品'}</span><button className="button primary" disabled={busy || !selected.size || selected.size > 128 || loading || !projectId} onClick={() => void copySelected()}>{busy ? <LoaderCircle className="spin" size={15} /> : <Copy size={15} />}{busy ? '正在复制…' : '复制到当前作品'}</button></footer>
  </div>;
}
