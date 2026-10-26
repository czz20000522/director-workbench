import { useEffect, useState } from 'react';
import { Image as ImageIcon, LoaderCircle, Sparkles } from 'lucide-react';
import { ApiError, mediaUrl, request } from './api/client';

type Item = { name: string; path: string; size: number; modified: number };
const imageExt = /\.(png|jpe?g|webp|gif|bmp)$/i;

// 清晰化库：素材库\清晰化 的只读视图，和「后期 → 清晰化」里看到的是同一批文件。
export default function EnhanceLibraryPanel() {
  const [items, setItems] = useState<Item[] | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    let active = true;
    request<{ items: Item[] }>('/api/enhance-library')
      .then(data => { if (active) { setItems(data.items || []); setError(''); } })
      .catch(reason => { if (active) setError(reason instanceof ApiError ? reason.message : '无法读取清晰化库'); });
    return () => { active = false; };
  }, []);
  return <section className="pl-panel" aria-label="清晰化库">
    <header className="pl-head"><div><h2>清晰化库</h2><p>2× 成品 · 来自 素材库\清晰化</p></div><span className="pl-icon"><Sparkles size={16} /></span></header>
    <div className="pl-list" role="list">
      {!items ? <p className="pl-empty" role="status"><LoaderCircle size={14} className="sf-spin" /> 正在读取清晰化库…</p>
        : items.length === 0 ? <p className="pl-empty">还没有清晰化成品。在作品里 后期 → 清晰化，处理完点「存入清晰化库」。</p>
          : items.map(item => <div className="pl-card hl-card" role="listitem" key={item.path}>
            {imageExt.test(item.name) && <img className="hl-thumb" src={mediaUrl(item.path)} alt="" loading="lazy" />}
            {!imageExt.test(item.name) && <span className="hl-thumb hl-video"><ImageIcon size={16} /></span>}
            <div className="hl-main"><strong>{item.name}</strong>
              <span>{(item.size / 1048576).toFixed(1)} MB · {new Date(item.modified * 1000).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })}</span></div>
            <a className="sf-ghost" href={mediaUrl(item.path)} target="_blank" rel="noreferrer" download>下载</a>
          </div>)}
    </div>
    {error && <p className="pl-error" role="alert">{error}</p>}
  </section>;
}
