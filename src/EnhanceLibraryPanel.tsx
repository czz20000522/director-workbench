import { useEffect, useMemo, useState } from 'react';
import { BookOpen, ChevronLeft, Download, Image as ImageIcon, Images, LoaderCircle, Search, Sparkles } from 'lucide-react';
import { ApiError, mediaUrl, request } from './api/client';
import LibraryExpand from './LibraryExpand';
import type { LibrarySection } from './LibraryExpand';

type Item = { name: string; path: string; size: number; modified: number };
const imageExt = /\.(png|jpe?g|webp|gif|bmp)$/i;
const sizeLabel = (bytes: number) => `${(bytes / 1048576).toFixed(1)} MB`;
const dateLabel = (stamp: number) => new Date(stamp * 1000).toLocaleString('zh-CN',
  { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });

// 清晰化库：素材库\清晰化 的只读视图，和「后期 → 清晰化」里看到的是同一批文件。
export default function EnhanceLibraryPanel() {
  const [items, setItems] = useState<Item[] | null>(null);
  const [error, setError] = useState('');
  const [query, setQuery] = useState('');
  const [selected, setSelected] = useState<Item | null>(null);
  useEffect(() => {
    let active = true;
    request<{ items: Item[] }>('/api/enhance-library')
      .then(data => { if (active) { setItems(data.items || []); setError(''); } })
      .catch(reason => { if (active) setError(reason instanceof ApiError ? reason.message : '无法读取清晰化库'); });
    return () => { active = false; };
  }, []);
  const shown = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return needle ? (items || []).filter(item => item.name.toLowerCase().includes(needle)) : items || [];
  }, [items, query]);

  const compact = <div className="pl-list" role="list">
    {!items ? <p className="pl-empty" role="status"><LoaderCircle size={14} className="sf-spin" /> 正在读取清晰化库…</p>
      : items.length === 0 ? <p className="pl-empty">还没有清晰化成品。在作品里 后期 → 清晰化，处理完点「存入清晰化库」。</p>
        : items.map(item => <div className="pl-card hl-card" role="listitem" key={item.path}>
            {imageExt.test(item.name) && <img className="hl-thumb" src={mediaUrl(item.path)} alt="" loading="lazy" />}
            {!imageExt.test(item.name) && <span className="hl-thumb hl-video"><ImageIcon size={16} /></span>}
            <div className="hl-main"><strong>{item.name}</strong>
              <span>{sizeLabel(item.size)} · {dateLabel(item.modified)}</span></div>
            <a className="sf-ghost" href={mediaUrl(item.path)} target="_blank" rel="noreferrer" download>下载</a>
          </div>)}
    {error && <p className="pl-error" role="alert">{error}</p>}
  </div>;

  const gridSection: LibrarySection = {
    id: 'grid', label: '全部成品', icon: <Images size={14} />,
    note: `清晰化库 · ${items?.length ?? 0} 个成品 · 来自 素材库\\清晰化`,
    content: <>
      {selected ? <>
        <div className="lex-toolbar">
          <div className="lex-row-actions">
            <button type="button" onClick={() => setSelected(null)}><ChevronLeft size={13} /> 返回全部成品</button>
          </div>
        </div>
        <div className="lex-viewer">
          {imageExt.test(selected.name)
            ? <img src={mediaUrl(selected.path)} alt={selected.name} />
            : <div className="lex-thumb-media" style={{ aspectRatio: '16 / 10' }}><ImageIcon size={30} /></div>}
          <div className="lex-viewer-side">
            <h3>{selected.name}</h3>
            <p>{sizeLabel(selected.size)} · {dateLabel(selected.modified)}</p>
            <p>这是归档进素材库的 2× 成品，原图始终保留、没有被覆盖。</p>
            <div className="lex-row-actions">
              <a className="sf-ghost" href={mediaUrl(selected.path)} target="_blank" rel="noreferrer" download>
                <Download size={13} /> 下载成品</a>
            </div>
          </div>
        </div>
      </> : <>
        <div className="lex-toolbar"><h3>全部成品</h3><span>{items ? `${items.length} 个` : '读取中…'}</span>
          <label className="lex-search"><Search size={13} />
            <input value={query} maxLength={40} onChange={event => setQuery(event.target.value)}
              placeholder="按文件名找" aria-label="搜索清晰化成品" /></label>
        </div>
        {!items ? <p className="lex-empty"><LoaderCircle size={14} className="sf-spin" /> 正在读取清晰化库…</p>
          : shown.length === 0 ? <p className="lex-empty">{query ? '没有匹配的文件。' : '还没有清晰化成品。在作品里 后期 → 清晰化，处理完点「存入清晰化库」；首帧工作台的「推入清晰化」也会自动归档。'}</p>
            : <div className="lex-thumb-grid">{shown.map(item => <button type="button" className="lex-thumb-card" key={item.path}
                onClick={() => setSelected(item)} title={item.name}>
                <span className="lex-thumb-media">
                  {imageExt.test(item.name) ? <img src={mediaUrl(item.path)} alt="" loading="lazy" /> : <ImageIcon size={22} />}
                </span>
                <strong>{item.name}</strong>
                <span>{sizeLabel(item.size)} · {dateLabel(item.modified)}</span>
              </button>)}</div>}
      </>}
      {error && <p className="lex-error" role="alert">{error}</p>}
    </>,
  };

  const guideSection: LibrarySection = {
    id: 'guide', label: '关于', icon: <BookOpen size={14} />,
    note: '这个库是只读视图',
    content: <div className="lex-note">
      <h3>清晰化库里装的是什么</h3>
      <p>这里显示的是 <code>素材库\清晰化</code> 目录下的文件，和「后期 → 清晰化」面板底部看到的是同一批，没有第二份副本。</p>
      <ul>
        <li>在 后期 → 清晰化 处理完点「存入清晰化库」，成品就归档到这里。</li>
        <li>在首帧工作台点「推入清晰化」，会跑完 2× 超分并自动归档，原图保留。</li>
        <li>这里只做浏览与下载；要删除文件请在素材区操作，避免误删正在被作品引用的成品。</li>
      </ul>
    </div>,
  };

  return <LibraryExpand title="清晰化库" subtitle="2× 成品 · 来自 素材库\清晰化 · 点展开看大图"
    icon={<Sparkles size={16} />} compact={compact}
    sections={[gridSection, guideSection]} />;
}
