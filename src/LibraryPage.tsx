import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ArrowLeft, Copy, Download, ExternalLink, ImageIcon, LoaderCircle, Scaling, Search, X } from 'lucide-react';
import { request } from './api/client';
import LiquifyDialog from './LiquifyDialog';
import './library-page.css';

type Library = { name: string; count: number; covers: string[]; source_title: string };
type Item = { id: string; title: string; filename: string; category: string; thumb_url: string; file_url: string; size: number; source_title: string };

const PAGE = 60;

const formatSize = (bytes: number) => bytes >= 1048576
  ? `${(bytes / 1048576).toFixed(1)} MB`
  : `${Math.max(1, Math.round(bytes / 1024))} KB`;

export default function LibraryPage({ initialCategory = '' }: { initialCategory?: string }) {
  const [libraries, setLibraries] = useState<Library[] | null>(null);
  const [category, setCategory] = useState(initialCategory);
  const [query, setQuery] = useState('');
  const [debounced, setDebounced] = useState('');
  const [items, setItems] = useState<Item[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [selected, setSelected] = useState<Item | null>(null);
  const [notice, setNotice] = useState('');
  const [liquify, setLiquify] = useState<Item | null>(null);
  const requestId = useRef(0);

  useEffect(() => {
    let alive = true;
    void request<{ libraries: Library[]; total: number }>('/api/reference-library/libraries')
      .then(data => {
        if (!alive) return;
        const list = data.libraries || [];
        setLibraries(list);
        // 深链接里的子库名对不上（改名/手输）时退回「全部」，不让页面空转。
        if (initialCategory && list.length && !list.some(item => item.name === initialCategory)) setCategory('');
      })
      .catch(() => { if (alive) setError('参考库清单读取失败'); });
    return () => { alive = false; };
  }, [initialCategory]);

  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(query.trim()), 220);
    return () => window.clearTimeout(timer);
  }, [query]);

  useEffect(() => { setItems([]); setTotal(0); }, [category, debounced]);

  const load = useCallback(async (offset: number) => {
    const ticket = ++requestId.current;
    setLoading(true);
    setError('');
    try {
      const params = new URLSearchParams({ category, q: debounced, offset: String(offset), limit: String(PAGE) });
      const page = await request<{ items: Item[]; total: number }>(`/api/reference-library/library?${params}`);
      if (ticket !== requestId.current) return;
      setTotal(page.total);
      setItems(current => offset === 0 ? page.items : [...current, ...page.items]);
    } catch {
      if (ticket === requestId.current) setError('素材读取失败，请重试');
    } finally {
      if (ticket === requestId.current) setLoading(false);
    }
  }, [category, debounced]);

  useEffect(() => { void load(0); }, [load]);

  useEffect(() => {
    if (!selected) return;
    const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') setSelected(null); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [selected]);

  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(''), 2000);
    return () => window.clearTimeout(timer);
  }, [notice]);

  const go = (next: string) => {
    setCategory(next);
    window.location.hash = next ? `#/library/${encodeURIComponent(next)}` : '#/library';
  };

  const copy = async (text: string, label: string) => {
    try { await navigator.clipboard.writeText(text); setNotice(`${label}已复制`); }
    catch { setNotice('浏览器不允许复制，请手动选择'); }
  };

  const activeName = category || '全部素材';
  const activeCount = useMemo(
    () => (category ? libraries?.find(item => item.name === category)?.count ?? 0 : libraries?.reduce((sum, item) => sum + item.count, 0) ?? 0),
    [libraries, category],
  );

  return <div className="lp-page">
    <header className="lp-top">
      <a className="lp-back" href="#"><ArrowLeft size={14} /> 返回工作台</a>
      <div className="lp-title"><h1>{activeName}</h1><span>{debounced ? `${total} 条匹配` : `${activeCount} 张`}</span></div>
      <label className="lp-search">
        <Search size={13} />
        <input value={query} maxLength={40} placeholder="搜标签，如 蕾丝 / 双马尾 / 站姿" onChange={event => setQuery(event.target.value)} aria-label="搜索素材标签" />
        {query && <button type="button" aria-label="清除搜索" onClick={() => setQuery('')}><X size={12} /></button>}
      </label>
    </header>

    <div className="lp-body">
      <nav className="lp-rail" aria-label="子库导航">
        <button type="button" className={`lp-lib ${category === '' ? 'selected' : ''}`} onClick={() => go('')}>
          <span className="lp-lib-name">全部素材</span>
          <span className="lp-lib-count">{libraries ? libraries.reduce((sum, item) => sum + item.count, 0) : '—'}</span>
          <span className="lp-lib-covers">{(libraries || []).slice(0, 4).flatMap(item => item.covers.slice(0, 1)).map((src, index) => <img key={index} src={src} alt="" loading="lazy" />)}</span>
        </button>
        {(libraries || []).map(item => <button key={item.name} type="button" className={`lp-lib ${category === item.name ? 'selected' : ''}`} onClick={() => go(item.name)}>
          <span className="lp-lib-name">{item.name}</span>
          <span className="lp-lib-count">{item.count}</span>
          <span className="lp-lib-covers">{item.covers.map((src, index) => <img key={index} src={src} alt="" loading="lazy" />)}</span>
        </button>)}
        {!libraries && <p className="lp-hint"><LoaderCircle size={12} className="sf-spin" /> 正在读取…</p>}
      </nav>

      <main className="lp-main">
        {error && <p className="lp-error" role="alert">{error}</p>}
        {!error && !items.length && !loading && <p className="lp-hint"><ImageIcon size={16} /> 这个筛选下没有素材。</p>}
        <div className="lp-grid">
          {items.map(item => <button key={item.id} type="button" className="lp-card" onClick={() => setSelected(item)} title={item.title}>
            <img src={item.thumb_url} alt={item.title} loading="lazy" />
            <span className="lp-card-title">{item.title}</span>
            {!category && <span className="lp-card-meta">{item.category}</span>}
          </button>)}
        </div>
        {loading && <p className="lp-hint"><LoaderCircle size={12} className="sf-spin" /> 正在加载…</p>}
        {!loading && items.length > 0 && items.length < total && <button type="button" className="lp-more" onClick={() => void load(items.length)}>再加载 {PAGE} 张（已显示 {items.length} / {total}）</button>}
      </main>
    </div>

    {selected && <aside className="lp-detail" aria-label="素材详情">
      <button type="button" className="lp-detail-close" aria-label="关闭" onClick={() => setSelected(null)}><X size={14} /></button>
      <img src={selected.file_url} alt={selected.title} />
      <h2>{selected.title}</h2>
      <dl>
        <div><dt>子库</dt><dd>{selected.category}</dd></div>
        <div><dt>文件</dt><dd>{selected.filename}</dd></div>
        <div><dt>大小</dt><dd>{formatSize(selected.size)}</dd></div>
      </dl>
      <div className="lp-detail-actions">
        <a className="lp-action" href={selected.file_url} target="_blank" rel="noreferrer"><ExternalLink size={13} /> 打开原图</a>
        <a className="lp-action" href={selected.file_url} download={selected.filename}><Download size={13} /> 下载</a>
        <button type="button" className="lp-action" onClick={() => setLiquify(selected)}><Scaling size={13} /> 液化塑形</button>
        <button type="button" className="lp-action" onClick={() => void copy(selected.title, '标签')}><Copy size={13} /> 复制标签</button>
      </div>
    </aside>}

    {liquify && <LiquifyDialog imagePath={liquify.file_url} materialId={liquify.id} allowRefine={false}
      onClose={() => setLiquify(null)}
      onDone={(_path, count) => {
        setLiquify(null);
        setNotice(`已存回「${liquify.category}」子库（${count} 个调整中心）；原图未改动`);
        void load(0);
      }} />}


    {notice && <p className="lp-notice" role="status">{notice}</p>}
  </div>;
}
