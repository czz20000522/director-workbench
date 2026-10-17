import { useEffect, useMemo, useState } from 'react';
import { ChevronDown, ChevronRight, ChevronsDownUp, Film, Folder, Image as ImageIcon, Music2, Plus, RefreshCw, Trash2 } from 'lucide-react';
import { assetLabel, assetTitle, type TreeKind } from './shotLabels';

type Project = { id: string; title: string; series?: string };
type PhysicalSeries = { name: string; works: Array<{ name: string; project_id: string | null }> };
type Archive = { series: string; work: string };
const isSeriesResource = (name: string) => name === '公共素材' || name === 'plans';
type Segment = { id: string; location?: string; audio?: { guide?: string | null; delivery_master?: string | null }; keyframes?: { first?: string | null; last?: string | null } };
type ProjectAsset = { id?: unknown; kind?: unknown; title?: unknown };
type TreeData = { segments: Segment[]; assets: ProjectAsset[] };
type TreeItem = { id: string; kind: TreeKind; title: string };

function treeItems(data: TreeData): Record<TreeKind, TreeItem[]> {
  const result: Record<TreeKind, TreeItem[]> = { video: [], audio: [], image: [] };
  const seen = new Set<string>();
  for (const segment of data.segments) {
    result.video.push({ id: segment.id, kind: 'video', title: segment.location || assetLabel(segment.id) });
    seen.add(`video:${segment.id}`);
    if (segment.audio?.delivery_master || segment.audio?.guide) {
      result.audio.push({ id: segment.id, kind: 'audio', title: segment.audio.delivery_master ? '成片音频' : '表演音频' });
      seen.add(`audio:${segment.id}`);
    }
    if (segment.keyframes?.first) {
      result.image.push({ id: segment.id, kind: 'image', title: '首帧' });
      seen.add(`image:${segment.id}`);
    }
  }
  for (const asset of data.assets) {
    if (typeof asset.id !== 'string' || !['video', 'audio', 'image'].includes(String(asset.kind))) continue;
    const kind = asset.kind as TreeKind;
    const key = `${kind}:${asset.id}`;
    if (seen.has(key)) continue;
    seen.add(key);
    result[kind].push({ id: asset.id, kind, title: typeof asset.title === 'string' ? asset.title : asset.id });
  }
  return result;
}

export default function WorkspaceExplorer({ projects, activeProjectId, activeSegments, activeAssets, selectedKind, selectedId, onOpenProject, onOpenItem, privateMode = false, onOpenArchive, onCreateWork, onDeleteDirectory, onRefreshProjects, refreshKey = 0 }: {
  projects: Project[];
  activeProjectId?: string;
  activeSegments: Segment[];
  activeAssets: ProjectAsset[];
  selectedKind: TreeKind;
  selectedId: string;
  onOpenProject: (projectId: string, item?: { kind: TreeKind; id: string }) => void;
  onOpenItem: (kind: TreeKind, id: string) => void;
  privateMode?: boolean;
  onOpenArchive?: (archive: Archive) => void;
  onCreateWork?: (series: string) => void;
  onDeleteDirectory?: (target: { series: string; work?: string; projectId?: string; title?: string; resource?: boolean }) => void;
  onRefreshProjects?: () => void;
  refreshKey?: number;
}) {
  const [physicalSeries, setPhysicalSeries] = useState<PhysicalSeries[]>([]);
  const [seriesError, setSeriesError] = useState('');
  const [newSeriesOpen, setNewSeriesOpen] = useState(false);
  const [newSeriesName, setNewSeriesName] = useState('');
  const [creatingSeries, setCreatingSeries] = useState(false);
  const [localRefresh, setLocalRefresh] = useState(0);
  const [openSeries, setOpenSeries] = useState<string[]>([]);
  const [openProjects, setOpenProjects] = useState<string[]>([]);
  const [cache, setCache] = useState<Record<string, TreeData>>({});
  const [loading, setLoading] = useState<string[]>([]);
  const [errors, setErrors] = useState<string[]>([]);
  const [categoryOverrides, setCategoryOverrides] = useState<Record<string, boolean>>({});
  const groups = useMemo(() => {
    const grouped = new Map<string, Array<Project | { title: string; archive: Archive }>>();
    for (const item of physicalSeries) {
      grouped.set(item.name, []);
    }
    for (const project of projects) {
      const series = project.series?.trim() || '未分类系列';
      grouped.set(series, [...(grouped.get(series) ?? []), project]);
    }
    const registeredIds = new Set(projects.map(project => project.id));
    for (const item of physicalSeries) {
      grouped.set(item.name, [...(grouped.get(item.name) ?? []), ...item.works.filter(work => !work.project_id || !registeredIds.has(work.project_id)).map(work => ({ title: work.name, archive: { series: item.name, work: work.name } }))]);
    }
    return Array.from(grouped.entries());
  }, [projects, physicalSeries]);

  useEffect(() => {
    if (!privateMode) return;
    let current = true;
    fetch('/api/private/series').then(async response => {
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || '系列目录读取失败');
      if (current) { setPhysicalSeries(Array.isArray(data.series) ? data.series : []); setSeriesError(''); }
    }).catch(error => { if (current) setSeriesError(error instanceof Error ? error.message : '系列目录读取失败'); });
    return () => { current = false; };
  }, [privateMode, refreshKey, localRefresh]);

  useEffect(() => {
    if (!privateMode) return;
    const timer = window.setInterval(() => setLocalRefresh(value => value + 1), 30000);
    return () => window.clearInterval(timer);
  }, [privateMode]);

  async function createSeries() {
    if (!newSeriesName.trim() || creatingSeries) return;
    setCreatingSeries(true);
    try {
      const response = await fetch('/api/private/series', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: newSeriesName.trim() }) });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || '创建系列失败');
      setPhysicalSeries(previous => [...previous, data]);
      setOpenSeries(previous => [...previous, data.name]);
      setNewSeriesName(''); setNewSeriesOpen(false); setSeriesError('');
    } catch (error) { setSeriesError(error instanceof Error ? error.message : '创建系列失败'); }
    finally { setCreatingSeries(false); }
  }

  useEffect(() => {
    if (!activeProjectId || !selectedId) return;
    setCategoryOverrides(previous => ({ ...previous, [`${activeProjectId}:${selectedKind}`]: true }));
  }, [activeProjectId, selectedKind]);

  function refreshDirectory() {
    setLocalRefresh(value => value + 1);
    onRefreshProjects?.();
    setCache({});
    setErrors([]);
    for (const projectId of openProjects) void loadProject(projectId, true);
  }

  async function toggleProject(projectId: string) {
    if (openProjects.includes(projectId)) { setOpenProjects(previous => previous.filter(id => id !== projectId)); return; }
    setOpenProjects(previous => [...previous, projectId]);
    await loadProject(projectId);
  }

  async function loadProject(projectId: string, force = false) {
    if (projectId === activeProjectId || (!force && cache[projectId]) || loading.includes(projectId)) return;
    setLoading(previous => [...previous, projectId]);
    try {
      const [manifestResponse, planResponse] = await Promise.all([
        fetch(`/api/projects/${encodeURIComponent(projectId)}`),
        fetch(`/api/projects/${encodeURIComponent(projectId)}/plan`),
      ]);
      if (!manifestResponse.ok || !planResponse.ok) throw new Error('读取失败');
      const [manifest, plan] = await Promise.all([manifestResponse.json(), planResponse.json()]);
      setCache(previous => ({ ...previous, [projectId]: {
        segments: Array.isArray(plan.segments) ? plan.segments : [],
        assets: Array.isArray(manifest.assets) ? manifest.assets : [],
      } }));
      setErrors(previous => previous.filter(id => id !== projectId));
    } catch { setErrors(previous => [...previous, projectId]); }
    finally { setLoading(previous => previous.filter(id => id !== projectId)); }
  }

  return <nav className="director-explorer" aria-label="我的系列和作品">
    <div className="director-explorer-heading"><strong>我的作品</strong><div className="director-explorer-tools"><button type="button" className="director-explorer-add" aria-label="全部折叠" title="全部折叠" onClick={() => { setOpenSeries([]); setOpenProjects([]); setCategoryOverrides({}); }}><ChevronsDownUp size={15} /></button>{privateMode && <><button type="button" className="director-explorer-add" aria-label="刷新目录" title="读取硬盘上的最新目录" onClick={refreshDirectory}><RefreshCw size={14} /></button><button type="button" className="director-explorer-add" aria-label="新建系列" title="新建系列" onClick={() => setNewSeriesOpen(open => !open)}><Plus size={16} /></button></>}</div></div>
    {newSeriesOpen && <form className="director-explorer-new-series" onSubmit={event => { event.preventDefault(); void createSeries(); }}><input aria-label="新系列名称" autoFocus value={newSeriesName} onChange={event => setNewSeriesName(event.target.value)} placeholder="输入系列名称" /><button type="submit" disabled={creatingSeries || !newSeriesName.trim()}>{creatingSeries ? '创建中' : '创建'}</button></form>}
    {seriesError && <p className="director-explorer-hint" role="alert">{seriesError}</p>}
    {!groups.length && <p className="director-explorer-empty">还没有系列。点击上方“＋”新建系列。</p>}
    {groups.map(([series, works]) => <div className="director-explorer-series" key={series}>
      <div className="director-explorer-series-row"><button type="button" className="director-explorer-row director-explorer-series-button" aria-expanded={openSeries.includes(series)} onClick={() => setOpenSeries(previous => previous.includes(series) ? [] : [series])}>
        {openSeries.includes(series) ? <ChevronDown size={15} /> : <ChevronRight size={15} />}<Folder size={15} /><span title={series}>{series}</span>
      </button>{onCreateWork && <button type="button" className="director-explorer-series-add" aria-label={`在${series}新建作品`} title="在此系列新建作品" onClick={() => onCreateWork(series)}><Plus size={15} /></button>}{physicalSeries.some(item => item.name === series) && onDeleteDirectory && <button className="director-explorer-delete" type="button" aria-label={`删除系列${series}`} title="删除系列" onClick={() => onDeleteDirectory({ series })}><Trash2 size={13} /></button>}</div>
      {openSeries.includes(series) && works.map(work => {
        if ('archive' in work) return <div className="director-explorer-archive-row" key={`archive:${work.archive.work}`}><button type="button" className="director-explorer-archive" title={`浏览现有目录：${work.title}`} onClick={() => onOpenArchive?.(work.archive)}><Folder size={14} /><span>{work.title}</span><small>{work.archive.work === '公共素材' ? '系列素材' : work.archive.work === 'plans' ? '资料目录' : '旧目录'}</small></button>{onDeleteDirectory && <button className="director-explorer-delete" type="button" aria-label={`删除目录${work.title}`} title="删除目录" onClick={() => onDeleteDirectory({ series, work: work.archive.work, resource: isSeriesResource(work.archive.work) })}><Trash2 size={13} /></button>}</div>;
        const expanded = openProjects.includes(work.id);
        const physicalWork = physicalSeries.find(item => item.name === series)?.works.find(folder => folder.project_id === work.id);
        const data = work.id === activeProjectId ? { segments: activeSegments, assets: activeAssets } : cache[work.id];
        const contents = data ? treeItems(data) : null;
        return <div className="director-explorer-work" key={work.id}>
          <div className={`director-explorer-row director-explorer-work-row ${work.id === activeProjectId ? 'active' : ''}`}>
            <button type="button" className="director-explorer-fold" aria-label={`${expanded ? '收起' : '展开'}${work.title}`} aria-expanded={expanded} onClick={() => void toggleProject(work.id)}>{expanded ? <ChevronDown size={15} /> : <ChevronRight size={15} />}</button>
            <button type="button" className="director-explorer-work-name" title={work.title} onClick={() => { if (work.id !== activeProjectId) onOpenProject(work.id); if (!expanded) void toggleProject(work.id); }}>{work.title}</button>
            {onDeleteDirectory && (physicalWork ? <button className="director-explorer-delete" type="button" aria-label={`删除作品${work.title}`} title="删除 E 盘作品目录" onClick={() => onDeleteDirectory({ series, work: physicalWork.name })}><Trash2 size={13} /></button> : privateMode && <button className="director-explorer-delete" type="button" aria-label={`移出旧作品${work.title}`} title="移出工作台，保留 D 盘文件" onClick={() => onDeleteDirectory({ series, projectId: work.id, title: work.title })}><Trash2 size={13} /></button>)}
          </div>
          {expanded && <div className="director-explorer-children">
            {loading.includes(work.id) && <span className="director-explorer-hint">正在读取素材…</span>}
            {errors.includes(work.id) && <button type="button" className="director-explorer-hint" onClick={() => void loadProject(work.id)}>读取失败，点击重试</button>}
            {contents && (['video', 'audio', 'image'] as TreeKind[]).map(kind => {
              const items = contents[kind];
              if (!items.length) return null;
              const Icon = kind === 'video' ? Film : kind === 'audio' ? Music2 : ImageIcon;
              const categoryKey = `${work.id}:${kind}`;
              const categoryOpen = categoryOverrides[categoryKey] ?? kind === 'video';
              return <div className="director-explorer-category" key={kind}><button type="button" className="director-explorer-category-label" aria-expanded={categoryOpen} onClick={() => setCategoryOverrides(previous => ({ ...previous, [categoryKey]: !categoryOpen }))}>{categoryOpen ? <ChevronDown size={13} /> : <ChevronRight size={13} />}<Icon size={13} />{kind === 'video' ? '分镜与视频' : kind === 'audio' ? '音频' : '图片'}<small>{items.length}</small></button>{categoryOpen && items.map(item => <button type="button" key={`${item.kind}:${item.id}`} className={`director-explorer-leaf ${work.id === activeProjectId && selectedKind === item.kind && selectedId === item.id ? 'selected' : ''}`} title={assetTitle(item.id, item.title, item.kind)} onClick={() => work.id === activeProjectId ? onOpenItem(item.kind, item.id) : onOpenProject(work.id, item)}>{assetTitle(item.id, item.title, item.kind)}</button>)}</div>;
            })}
            {contents && !Object.values(contents).some(items => items.length) && <span className="director-explorer-hint">这部作品还没有分镜或素材</span>}
          </div>}
        </div>;
      })}
      {openSeries.includes(series) && <div className="director-explorer-work-count">该系列已有 {works.filter(work => !('archive' in work) || !isSeriesResource(work.archive.work)).length} 部作品</div>}
    </div>)}
  </nav>;
}
