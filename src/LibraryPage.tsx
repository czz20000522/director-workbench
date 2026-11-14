import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ArrowLeft, CheckSquare, Copy, Download, ExternalLink, ImageIcon, LoaderCircle, Scaling, Search, Square, Trash2, X } from 'lucide-react';
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
  // 批量选择与删除：选中态只存在这张页面上，换子库/换搜索就清空。
  const [selectMode, setSelectMode] = useState(false);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [confirm, setConfirm] = useState<{ ids: string[]; label: string } | null>(null);
  const [deleting, setDeleting] = useState(false);
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

  useEffect(() => { setItems([]); setTotal(0); setPicked(new Set()); setSelectMode(false); setConfirm(null); }, [category, debounced]);

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
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      if (confirm) { setConfirm(null); return; }
      if (selected) { setSelected(null); return; }
      if (selectMode) { setSelectMode(false); setPicked(new Set()); }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [selected, selectMode, confirm]);

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

  const togglePick = (id: string) => setPicked(current => {
    const next = new Set(current);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  });

  const pickAllLoaded = () => setPicked(current =>
    current.size === items.length ? new Set() : new Set(items.map(item => item.id)));

  // 删除只走一个接口：单张 = 一个 id，批量 = 多个 id。失败的分项会回报原因，
  // 不会因为其中一张是外部目录就整体失败。
  const removeMaterials = async (ids: string[]) => {
    if (!ids.length || deleting) return;
    setDeleting(true); setError('');
    try {
      const data = await request<{ deleted: { id: string }[]; blocked: { title: string; reason: string }[] }>(
        '/api/reference-library/materials/delete',
        { json: { material_ids: ids }, timeoutMs: 120_000 });
      const gone = new Set(data.deleted.map(item => item.id));
      if (gone.size) {
        setItems(current => current.filter(item => !gone.has(item.id)));
        setTotal(current => Math.max(0, current - gone.size));
        setSelected(current => (current && gone.has(current.id) ? null : current));
        setPicked(current => new Set([...current].filter(id => !gone.has(id))));
        setNotice(`已删除 ${gone.size} 张（移入回收站，可以还原）`);
        void request<{ libraries: Library[] }>('/api/reference-library/libraries')
          .then(next => setLibraries(next.libraries || [])).catch(() => { /* 计数下次刷新会纠正 */ });
      } else {
        setNotice('没有删除任何文件');
      }
      if (data.blocked.length) {
        setError(`${data.blocked.length} 张没删：${data.blocked.slice(0, 3).map(item => `${item.title}（${item.reason}）`).join('；')}${data.blocked.length > 3 ? '…' : ''}`);
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '删除失败');
    } finally {
      setDeleting(false); setConfirm(null);
    }
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
      <button type="button" className={`lp-batch${selectMode ? ' is-on' : ''}`}
        aria-pressed={selectMode}
        onClick={() => { setSelectMode(value => !value); setPicked(new Set()); setSelected(null); }}>
        {selectMode ? <><X size={13} /> 退出批量</> : <><CheckSquare size={13} /> 批量选择</>}
      </button>
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
        {selectMode && <div className="lp-select-bar">
          <span>已选 {picked.size} 张 / 已载入 {items.length}</span>
          <button type="button" onClick={pickAllLoaded}>{items.length && picked.size === items.length ? '取消全选' : '全选已载入'}</button>
          <button type="button" className="lp-danger" disabled={!picked.size || deleting}
            onClick={() => setConfirm({ ids: [...picked], label: `所选 ${picked.size} 张` })}>
            <Trash2 size={13} /> 删除所选
          </button>
        </div>}
        {error && <p className="lp-error" role="alert">{error}</p>}
        {!error && !items.length && !loading && <p className="lp-hint"><ImageIcon size={16} /> 这个筛选下没有素材。</p>}
        <div className="lp-grid">
          {items.map(item => <button key={item.id} type="button"
            className={`lp-card${selectMode && picked.has(item.id) ? ' is-picked' : ''}`}
            aria-pressed={selectMode ? picked.has(item.id) : undefined}
            onClick={() => (selectMode ? togglePick(item.id) : setSelected(item))} title={item.title}>
            {selectMode && <span className="lp-pick" aria-hidden>{picked.has(item.id) ? <CheckSquare size={15} /> : <Square size={15} />}</span>}
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
        <button type="button" className="lp-action lp-danger" onClick={() => setConfirm({ ids: [selected.id], label: selected.title })}><Trash2 size={13} /> 删除素材</button>
      </div>
    </aside>}

    {liquify && <LiquifyDialog imagePath={liquify.file_url} materialId={liquify.id} allowRefine={false}
      onClose={() => setLiquify(null)}
      onDone={(_path, count) => {
        setLiquify(null);
        setNotice(`已存回「${liquify.category}」子库（${count} 个调整中心）；原图未改动`);
        void load(0);
      }} />}


    {confirm && <div className="lp-confirm-mask" role="dialog" aria-modal="true" aria-label="确认删除素材">
      <div className="lp-confirm">
        <strong>删除「{confirm.label}」？</strong>
        <p>文件会移入回收站，可以还原；素材库里的缩略图和标签也会一起清掉。外部目录里的图不会被删除。</p>
        <div className="lp-confirm-actions">
          <button type="button" className="lp-danger" disabled={deleting} onClick={() => void removeMaterials(confirm.ids)}>
            {deleting ? '删除中…' : `确认删除${confirm.ids.length > 1 ? ` ${confirm.ids.length} 张` : ''}`}
          </button>
          <button type="button" disabled={deleting} onClick={() => setConfirm(null)}>取消</button>
        </div>
      </div>
    </div>}

    {notice && <p className="lp-notice" role="status">{notice}</p>}
  </div>;
}
