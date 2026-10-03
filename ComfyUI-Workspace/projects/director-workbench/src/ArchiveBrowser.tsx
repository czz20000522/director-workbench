import { useEffect, useState } from 'react';
import { Film, Image as ImageIcon, Music2 } from 'lucide-react';

type Archive = { series: string; work: string };
type MediaFile = { name: string; relative_path: string; path: string; kind: 'video' | 'audio' | 'image' };

export default function ArchiveBrowser({ archive }: { archive: Archive }) {
  const [files, setFiles] = useState<MediaFile[]>([]);
  const [selected, setSelected] = useState<MediaFile | null>(null);
  const [limited, setLimited] = useState(false);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [query, setQuery] = useState('');
  const [kind, setKind] = useState<'all' | MediaFile['kind']>('all');

  useEffect(() => {
    let current = true;
    setLoading(true); setSelected(null); setFiles([]); setError('');
    fetch(`/api/private/series/${encodeURIComponent(archive.series)}/works/${encodeURIComponent(archive.work)}/media`)
      .then(async response => { const data = await response.json(); if (!response.ok) throw new Error(data.detail || '目录读取失败'); return data; })
      .then(data => { if (current) { setFiles(Array.isArray(data.files) ? data.files : []); setLimited(Boolean(data.limited)); } })
      .catch(cause => { if (current) setError(cause instanceof Error ? cause.message : '目录读取失败'); })
      .finally(() => { if (current) setLoading(false); });
    return () => { current = false; };
  }, [archive.series, archive.work]);

  const mediaUrl = (file: MediaFile) => `/media-file?path=${encodeURIComponent(file.path)}`;
  const visible = files.filter(file => (kind === 'all' || file.kind === kind) && file.relative_path.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()));
  const resourceFolder = archive.work === '公共素材' || archive.work === 'plans';
  return <section className="director-archive" aria-label={`${archive.work}的现有素材`}>
    <div className="director-archive-heading"><span>{archive.series} / {archive.work}</span><h1>{archive.work}</h1><p>{resourceFolder ? '这是系列资料目录，可以查看其中的图片、音频和视频。' : '这是当前账号已有的作品目录。可以在这里查看素材；接入分镜制作后再编辑提示词和生成视频。'}</p></div>
    {loading ? <p>正在读取目录…</p> : error ? <p role="alert">{error}</p> : <>
      <div className="director-archive-body">
        <div className="director-archive-files" aria-label="素材文件"><div className="director-archive-filters"><input aria-label="查找素材" value={query} onChange={event => setQuery(event.target.value)} placeholder="查找文件名或目录" /><select aria-label="素材类型" value={kind} onChange={event => setKind(event.target.value as typeof kind)}><option value="all">全部 · {files.length}</option><option value="video">视频 · {files.filter(file => file.kind === 'video').length}</option><option value="audio">音频 · {files.filter(file => file.kind === 'audio').length}</option><option value="image">图片 · {files.filter(file => file.kind === 'image').length}</option></select></div>{visible.map(file => <button type="button" key={file.path} className={selected?.path === file.path ? 'selected' : ''} onClick={() => setSelected(file)} title={file.relative_path}>{file.kind === 'video' ? <Film size={16} /> : file.kind === 'audio' ? <Music2 size={16} /> : <ImageIcon size={16} />}<span>{file.relative_path}</span></button>)}{!visible.length && <p>{files.length ? '没有符合条件的素材。' : '此目录尚未找到可预览的图片、音频或视频。'}</p>}</div>
        <div className="director-archive-preview">{selected ? <><strong>{selected.name}</strong>{selected.kind === 'video' ? <video src={mediaUrl(selected)} controls preload="metadata" /> : selected.kind === 'audio' ? <audio src={mediaUrl(selected)} controls preload="metadata" /> : <img src={mediaUrl(selected)} alt={selected.name} />}</> : <p>选择左侧素材进行预览</p>}</div>
      </div>
      {limited && <p className="director-archive-limited">目录文件较多，当前先显示前 200 个可预览素材。</p>}
    </>}
  </section>;
}
