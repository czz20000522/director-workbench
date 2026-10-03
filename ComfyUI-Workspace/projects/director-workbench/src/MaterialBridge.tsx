import ProjectMaterialLibrary from './ProjectMaterialLibrary';
import { useEffect, useMemo, useRef, useState } from 'react';
import { ArrowDownToLine, ArrowUp, Check, ChevronRight, FileJson, Film, Folder, HardDrive, Image as ImageIcon, Laptop, LoaderCircle, Music2, Search, X } from 'lucide-react';

type MaterialKind = 'all' | 'video' | 'audio' | 'image' | 'document';
type MaterialEntry = { name: string; relative_path: string; entry_type: 'directory' | 'file'; kind: MaterialKind | 'directory' | 'other'; size_bytes: number; modified_at: number };
type MaterialResponse = { source_id: string; source_label: string; current_path: string; query: string; kind: MaterialKind; entries: MaterialEntry[]; truncated: boolean; sources: Array<{ id: string; label: string; import_mode?: 'reference' | 'copy' }> };

const kindLabels: Record<MaterialKind, string> = { all: '全部', video: '视频', audio: '音频', image: '图片', document: '文档' };
const mediaKinds = new Set(['video', 'audio', 'image']);

function formatBytes(bytes: number) {
  if (!bytes) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  return `${(bytes / 1024 ** index).toFixed(index > 1 ? 1 : 0)} ${units[index]}`;
}

function entryIcon(entry: MaterialEntry) {
  if (entry.entry_type === 'directory') return Folder;
  if (entry.kind === 'video') return Film;
  if (entry.kind === 'audio') return Music2;
  if (entry.kind === 'image') return ImageIcon;
  return FileJson;
}

export default function MaterialBridge({ projectId, initialMode = 'server', onClose, onRegistered }: { projectId: string; initialMode?: 'server' | 'device' | 'projects'; onClose: () => void; onRegistered: (count: number) => void }) {
  const [mode, setMode] = useState<'server' | 'device' | 'projects'>(initialMode);
  const [sourceId, setSourceId] = useState('project');
  const [path, setPath] = useState('');
  const [kind, setKind] = useState<MaterialKind>('all');
  const [queryDraft, setQueryDraft] = useState('');
  const [query, setQuery] = useState('');
  const [data, setData] = useState<MaterialResponse | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [deviceFiles, setDeviceFiles] = useState<File[]>([]);
  const [loading, setLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [notice, setNotice] = useState('');
  const [error, setError] = useState('');
  const deviceInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!projectId || mode !== 'server') return;
    const controller = new AbortController();
    setLoading(true); setError('');
    const params = new URLSearchParams({ project_id: projectId, source_id: sourceId, path, kind });
    if (query) params.set('query', query);
    fetch(`/api/material-library?${params.toString()}`, { signal: controller.signal })
      .then(async response => { if (!response.ok) throw new Error(await response.text()); return response.json(); })
      .then((value: MaterialResponse) => setData(value))
      .catch(reason => { if (reason?.name !== 'AbortError') setError(reason instanceof Error ? reason.message : String(reason)); })
      .finally(() => setLoading(false));
    return () => controller.abort();
  }, [projectId, sourceId, path, kind, query, mode]);

  const crumbs = useMemo(() => {
    const parts = path ? path.split('/') : [];
    return [{ label: data?.source_label ?? '素材库', path: '' }, ...parts.map((part, index) => ({ label: part, path: parts.slice(0, index + 1).join('/') }))];
  }, [path, data?.source_label]);
  const selectedItems = [...selected].map(value => { const divider = value.indexOf(':'); return { source_id: value.slice(0, divider), relative_path: value.slice(divider + 1) }; });

  function switchSource(next: string) {
    setSourceId(next); setPath(''); setQuery(''); setQueryDraft(''); setSelected(new Set());
  }

  function toggleEntry(entry: MaterialEntry) {
    const key = `${sourceId}:${entry.relative_path}`;
    setSelected(previous => { const next = new Set(previous); if (next.has(key)) next.delete(key); else next.add(key); return next; });
  }

  async function registerSelected() {
    if (!selectedItems.length) return;
    setSubmitting(true); setError(''); setNotice('');
    try {
      const response = await fetch(`/api/projects/${encodeURIComponent(projectId)}/materials/register`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ items: selectedItems }),
      });
      if (!response.ok) throw new Error(await response.text());
      const payload = await response.json();
      const count = Array.isArray(payload.registered) ? payload.registered.length : 0;
      setNotice(`已登记 ${count} 个 Windows 素材。`); setSelected(new Set()); onRegistered(count);
    } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setSubmitting(false); }
  }

  async function uploadDeviceFiles() {
    if (!deviceFiles.length) return;
    setSubmitting(true); setError(''); setNotice('');
    try {
      const form = new FormData();
      const batch = new Date().toISOString().replace(/[:.]/g, '-');
      deviceFiles.forEach(file => form.append('files', file, `assets/imported-device/${batch}/${file.name}`));
      const response = await fetch(`/api/projects/${encodeURIComponent(projectId)}/upload`, { method: 'POST', body: form });
      if (!response.ok) throw new Error(await response.text());
      const payload = await response.json();
      const count = Array.isArray(payload.uploaded) ? payload.uploaded.length : 0;
      setNotice(`已从当前设备上传并登记 ${count} 个文件。`); setDeviceFiles([]); onRegistered(count);
      if (deviceInputRef.current) deviceInputRef.current.value = '';
    } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setSubmitting(false); }
  }

  return <div className="material-bridge-backdrop" role="presentation" onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}>
    <section className="material-bridge" role="dialog" aria-modal="true" aria-labelledby="material-bridge-title">
      <header className="material-bridge-head"><div><span className="director-kicker">跨设备素材桥</span><h2 id="material-bridge-title">选择项目素材</h2></div><button className="icon-button" aria-label="关闭素材桥" onClick={onClose}><X size={18} /></button></header>
      <div className="material-bridge-modes" role="tablist" aria-label="素材来源"><button role="tab" aria-selected={mode === 'projects'} className={mode === 'projects' ? 'active' : ''} onClick={() => setMode('projects')}><Folder size={15} />其他作品</button><button role="tab" aria-selected={mode === 'server'} className={mode === 'server' ? 'active' : ''} onClick={() => setMode('server')}><HardDrive size={15} />Windows 素材库</button><button role="tab" aria-selected={mode === 'device'} className={mode === 'device' ? 'active' : ''} onClick={() => setMode('device')}><Laptop size={15} />此设备上传</button></div>

      {mode === 'projects' ? <ProjectMaterialLibrary projectId={projectId} onRegistered={onRegistered} /> : mode === 'server' ? <>
        <div className="material-toolbar"><select aria-label="选择 Windows 素材库" value={sourceId} onChange={event => switchSource(event.target.value)}>{(data?.sources ?? [{ id: 'input', label: 'Windows 输入素材' }, { id: 'output', label: 'Windows 生成结果' }, { id: 'project', label: '当前项目' }, { id: 'downloads', label: 'Windows 下载目录' }]).map(source => <option key={source.id} value={source.id}>{source.label}</option>)}</select><div className="material-search"><Search size={14} /><input value={queryDraft} onChange={event => setQueryDraft(event.target.value)} onKeyDown={event => { if (event.key === 'Enter') setQuery(queryDraft.trim()); }} placeholder="搜索文件名" /><button className="button secondary compact" disabled={Boolean(queryDraft.trim()) && queryDraft.trim().length < 2} onClick={() => setQuery(queryDraft.trim())}>搜索</button></div></div>
        <div className="material-kind-filter" aria-label="素材类型">{(Object.keys(kindLabels) as MaterialKind[]).map(value => <button key={value} className={kind === value ? 'active' : ''} onClick={() => setKind(value)}>{kindLabels[value]}</button>)}</div>
        <nav className="material-breadcrumbs" aria-label="素材目录">{crumbs.map((crumb, index) => <span key={`${crumb.path}-${index}`}><button onClick={() => { setPath(crumb.path); setQuery(''); setQueryDraft(''); }}>{index === 0 ? <HardDrive size={13} /> : crumb.label}</button>{index < crumbs.length - 1 && <ChevronRight size={12} />}</span>)}{query && <span><ChevronRight size={12} /><button onClick={() => { setQuery(''); setQueryDraft(''); }}>搜索：{query}</button></span>}</nav>
        <div className="material-list" aria-busy={loading}>{loading ? <div className="material-empty"><LoaderCircle className="spin" size={22} />正在读取素材库</div> : data?.entries.length ? data.entries.map(entry => { const Icon = entryIcon(entry); const registerable = entry.entry_type === 'file' && mediaKinds.has(entry.kind); const key = `${sourceId}:${entry.relative_path}`; const download = `/material-file?project_id=${encodeURIComponent(projectId)}&source_id=${encodeURIComponent(sourceId)}&path=${encodeURIComponent(entry.relative_path)}`; return <article key={entry.relative_path} className={selected.has(key) ? 'selected' : ''}><button className="material-entry-main" onClick={() => entry.entry_type === 'directory' ? (setPath(entry.relative_path), setQuery(''), setQueryDraft('')) : registerable && toggleEntry(entry)}><span className="material-entry-icon"><Icon size={17} /></span><span><strong>{entry.name}</strong><small>{entry.entry_type === 'directory' ? '文件夹' : `${kindLabels[entry.kind as MaterialKind] ?? '文件'} · ${formatBytes(entry.size_bytes)}`}</small></span>{registerable && <span className="material-check">{selected.has(key) && <Check size={13} />}</span>}</button>{entry.entry_type === 'file' && <a className="icon-button" href={download} title="下载到当前设备" aria-label={`下载 ${entry.name}`}><ArrowDownToLine size={15} /></a>}</article>; }) : <div className="material-empty"><Folder size={23} />当前目录没有匹配素材</div>}</div>
        {data?.truncated && <p className="material-limit">结果较多，仅显示前 200 项；可缩小目录或使用搜索。</p>}
        <footer className="material-bridge-foot"><span>{selectedItems.length ? `已选择 ${selectedItems.length} 个素材` : '选择 Windows 素材后登记到当前项目'}</span><button className="button primary" disabled={!selectedItems.length || submitting} onClick={() => void registerSelected()}>{submitting ? <LoaderCircle className="spin" size={15} /> : <Check size={15} />}登记到项目</button></footer>
      </> : <div className="device-upload"><div className="device-upload-icon"><Laptop size={27} /></div><h3>从当前设备选择</h3><p>浏览器在 Mac 上打开时选择 Mac 文件，在 Windows 上打开时选择 Windows 文件。</p><input ref={deviceInputRef} type="file" multiple accept="video/*,audio/*,image/*,.json,.txt,.md,.srt,.vtt" onChange={event => setDeviceFiles(Array.from(event.target.files ?? []))} /><div className="device-file-summary">{deviceFiles.length ? <><strong>{deviceFiles.length} 个文件</strong><span>{formatBytes(deviceFiles.reduce((sum, file) => sum + file.size, 0))}</span></> : <span>尚未选择文件</span>}</div><button className="button primary" disabled={!deviceFiles.length || submitting} onClick={() => void uploadDeviceFiles()}>{submitting ? <LoaderCircle className="spin" size={15} /> : <ArrowUp size={15} />}上传并登记</button></div>}
      {mode !== 'projects' && (notice || error) && <div className={`material-notice ${error ? 'error' : ''}`}>{error || notice}</div>}
    </section>
  </div>;
}
