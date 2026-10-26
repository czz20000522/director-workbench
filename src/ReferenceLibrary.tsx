import { useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import {
  ArrowRight, BookOpen, Check, ChevronLeft, Copy, Film, Image as ImageIcon, Images, LoaderCircle, RefreshCw,
  ScanText, Search, X,
} from 'lucide-react';
import { request } from './api/client';
import './reference-library.css';

type Summary = {
  id: string; title: string; kind: string; work: string; tags: string[];
  document_id: string; document_title: string; line: number; excerpt: string;
  score?: number; matched_terms?: string[];
};
type Entry = Summary & { body: string; plain: string; document_path: string };
type WorkDocument = { id: string; title: string; short_title: string; sections: number; kinds: Record<string, number> };
type Work = {
  id: string; name: string; sections: number; materials: number;
  documents: WorkDocument[]; kinds: Record<string, number>; featured: Summary[]; covers: string[];
};
type Material = {
  id: string; title: string; filename: string; category: string; work: string; source_title: string;
  description?: string | null; related_entry_id?: string | null; tags: string[];
  thumb_url: string; file_url: string;
};
type Overview = {
  documents: { id: string; title: string; path: string; work: string; sections: number; origin?: string }[];
  works: Work[];
  featured: Summary[]; materials: Material[]; counts: { documents: number; sections: number; materials: number };
  kinds: Record<string, string | number>; notes: string[]; config_error?: string | null;
  config_path: string; inbox_path: string;
};
type SearchResult = { query: string; items: Summary[]; terms: string[]; note: string };
type AnatomyNode = { id: number; label: string; text: string };
type AnatomyResult = {
  nodes: AnatomyNode[]; missing: string[]; unclassified: string[]; note: string;
  anchors: { code: string; entry_id: string | null; title: string | null }[];
  related: Summary[];
};

export type PromptTarget = 'image' | 'text-video' | 'image-video';
export const promptTargetLabels: Record<PromptTarget, string> = { image: '文生图', 'text-video': '文生视频', 'image-video': '图生视频' };
const promptTargets = Object.keys(promptTargetLabels) as PromptTarget[];

const errorText = (reason: unknown, fallback: string) => reason instanceof Error ? reason.message : fallback;

const documentLabel = (item: { work: string; document_title: string }) =>
  item.document_title.startsWith(item.work)
    ? item.document_title.slice(item.work.length).replace(/^[\s·\-_]+/, '') || item.document_title
    : item.document_title;

function useDebounced(value: string, delay: number): string {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), delay);
    return () => clearTimeout(timer);
  }, [value, delay]);
  return debounced;
}

function CopyButton({ text, label = '复制' }: { text: string; label?: string }) {
  const [done, setDone] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setDone(true);
      setTimeout(() => setDone(false), 1600);
    } catch {
      setDone(false);
    }
  };
  return <button className="rl-action" onClick={() => void copy()}>{done ? <Check size={13} /> : <Copy size={13} />}{done ? '已复制' : label}</button>;
}

function Modal({ title, onClose, wide = false, children }: { title: string; onClose: () => void; wide?: boolean; children: ReactNode }) {
  useEffect(() => {
    const handler = (event: KeyboardEvent) => { if (event.key === 'Escape') onClose(); };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [onClose]);
  return <div className="sf-modal-backdrop rl-backdrop" role="dialog" aria-modal="true" aria-label={title} onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}>
    <div className={`rl-modal ${wide ? 'wide' : ''}`}>
      <header className="rl-modal-head"><h2>{title}</h2><button aria-label="关闭" className="rl-close" onClick={onClose}><X size={17} /></button></header>
      <div className="rl-modal-body">{children}</div>
    </div>
  </div>;
}

function EntryDialog({ entryId, onClose, onUsePrompt, onAnatomy, target }: {
  entryId: string; onClose: () => void; onUsePrompt?: (text: string, target: PromptTarget) => void; onAnatomy?: (text: string) => void;
  target?: PromptTarget;
}) {
  const [entry, setEntry] = useState<Entry | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    let cancelled = false;
    setEntry(null); setError('');
    void request<Entry>(`/api/reference-library/entries/${encodeURIComponent(entryId)}`)
      .then(data => { if (!cancelled) setEntry(data); })
      .catch(reason => { if (!cancelled) setError(errorText(reason, '无法读取原文')); });
    return () => { cancelled = true; };
  }, [entryId]);
  return <Modal title={entry?.title || '原文'} wide onClose={onClose}>
    {error && <p className="sf-error" role="alert">{error}</p>}
    {!entry && !error && <p className="rl-loading"><LoaderCircle size={15} className="sf-spin" /> 正在读取原文…</p>}
    {entry && <>
      <p className="rl-source">{entry.document_title} · 第 {entry.line} 行 · {entry.kind}</p>
      <pre className="rl-body">{entry.plain}</pre>
      <div className="rl-actions">
        <CopyButton text={entry.plain} label="复制原文" />
        {onAnatomy && <button className="rl-action" onClick={() => onAnatomy(entry.plain)}><ScanText size={13} /> 拆成参考节点</button>}
        {onUsePrompt && (target
          ? <button className="rl-action primary" onClick={() => onUsePrompt(entry.plain, target)}>带进{promptTargetLabels[target]} <ArrowRight size={13} /></button>
          : promptTargets.map(item => <button key={item} className="rl-action primary" onClick={() => onUsePrompt(entry.plain, item)}>带进{promptTargetLabels[item]} <ArrowRight size={13} /></button>))}
      </div>
    </>}
  </Modal>;
}

function AnatomyDialog({ initialText, onClose, onOpenEntry }: {
  initialText: string; onClose: () => void; onOpenEntry: (entryId: string) => void;
}) {
  const [text, setText] = useState(initialText);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<AnatomyResult | null>(null);
  const [error, setError] = useState('');
  const run = async () => {
    if (!text.trim()) { setError('请先把一段提示词粘贴进来。'); return; }
    setBusy(true); setError('');
    try {
      setResult(await request<AnatomyResult>('/api/reference-library/anatomy', {
        init: { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text }) },
        timeoutMs: 60_000,
      }));
    } catch (reason) { setError(errorText(reason, '拆解失败')); }
    finally { setBusy(false); }
  };
  return <Modal title="拆成参考节点" wide onClose={onClose}>
    <p className="rl-note">{result?.note || '粘贴一段提示词。工作台按你文档里的 13 块结构切开，只换行、不改写，也不生成新句子。'}</p>
    <textarea className="rl-textarea" rows={7} value={text} onChange={event => setText(event.target.value)} placeholder="把整段提示词粘贴到这里。" />
    <div className="rl-actions">
      <button className="rl-action primary" disabled={busy || !text.trim()} onClick={() => void run()}>
        {busy ? <LoaderCircle size={13} className="sf-spin" /> : <ScanText size={13} />}{busy ? '切分中…' : '拆成参考节点'}
      </button>
    </div>
    {error && <p className="sf-error" role="alert">{error}</p>}
    {result && <div className="rl-anatomy">
      <div className="rl-anatomy-head"><strong>{result.nodes.length} 个节点</strong><span>缺 {result.missing.length} 块</span></div>
      <div className="rl-node-list">
        {result.nodes.map(node => <article className="rl-node" key={node.id}>
          <div className="rl-node-head"><span>{String(node.id).padStart(2, '0')}</span><strong>{node.label}</strong><CopyButton text={node.text} /></div>
          <p>{node.text}</p>
        </article>)}
      </div>
      {result.missing.length > 0 && <p className="rl-missing">这段里没有找到：{result.missing.join('、')}。缺项不补写，需要你自己决定是否补。</p>}
      {result.unclassified.length > 0 && <details className="rl-unclassified"><summary>未归类片段（{result.unclassified.length}）</summary>{result.unclassified.map((piece, index) => <p key={index}>{piece}</p>)}</details>}
      {result.anchors.length > 0 && <section className="rl-anchors"><h3>这段里引用的锚点</h3>{result.anchors.map(anchor => <div className="rl-anchor" key={anchor.code}><strong>{anchor.code}</strong>{anchor.entry_id ? <button onClick={() => onOpenEntry(anchor.entry_id as string)}>{anchor.title} <ArrowRight size={12} /></button> : <span>资料里没有这张锚点卡</span>}</div>)}</section>}
      {result.related.length > 0 && <section className="rl-related"><h3>资料里写法相近的原文</h3>{result.related.map(item => <button key={item.id} onClick={() => onOpenEntry(item.id)}><strong>{item.title}</strong><small>{item.document_title}</small></button>)}</section>}
    </div>}
  </Modal>;
}

export function PromptLibraryPanel({ onUsePrompt, target }: { onUsePrompt: (text: string, target: PromptTarget) => void; target?: PromptTarget }) {
  const [overview, setOverview] = useState<Overview | null>(null);
  const [error, setError] = useState('');
  const [work, setWork] = useState('');
  const [documentId, setDocumentId] = useState('');
  const [query, setQuery] = useState('');
  const [kind, setKind] = useState('');
  const [result, setResult] = useState<SearchResult | null>(null);
  const [searching, setSearching] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [tick, setTick] = useState(0);
  const [entryId, setEntryId] = useState('');
  const [anatomyText, setAnatomyText] = useState<string | null>(null);
  const debounced = useDebounced(query, 280);
  const initialised = useRef(false);

  useEffect(() => {
    let cancelled = false;
    setError('');
    void request<Overview>('/api/reference-library')
      .then(data => { if (!cancelled) { setOverview(data); initialised.current = true; } })
      .catch(reason => { if (!cancelled) setError(errorText(reason, '无法读取提示词库')); });
    return () => { cancelled = true; };
  }, [tick]);

  useEffect(() => {
    if (!initialised.current) return;
    if (!work && !debounced) { setResult(null); setSearching(false); return; }
    let cancelled = false;
    setSearching(true);
    const params = new URLSearchParams({ q: debounced, kind, work, document: documentId });
    if (!debounced && (kind || documentId)) params.set('limit', '50');
    void request<SearchResult>(`/api/reference-library/search?${params.toString()}`)
      .then(data => { if (!cancelled) setResult(data); })
      .catch(reason => { if (!cancelled) setError(errorText(reason, '检索失败')); })
      .finally(() => { if (!cancelled) setSearching(false); });
    return () => { cancelled = true; };
  }, [debounced, kind, work, documentId, tick]);

  const refresh = async () => {
    setRefreshing(true); setError('');
    try {
      setOverview(await request<Overview>('/api/reference-library/refresh', { init: { method: 'POST' }, timeoutMs: 60_000 }));
      setTick(value => value + 1);
    } catch (reason) { setError(errorText(reason, '刷新索引失败')); }
    finally { setRefreshing(false); }
  };

  const currentWork = overview?.works.find(item => item.name === work);
  const currentDocument = currentWork?.documents.find(item => item.id === documentId);
  const items = result?.items || currentWork?.featured || [];
  const scopedKinds = currentDocument?.kinds || currentWork?.kinds || overview?.kinds || {};
  const kindEntries = Object.entries(scopedKinds).filter(([, count]) => Number(count) > 0);
  const selectedKind = kindEntries.some(([name]) => name === kind) ? kind : '';
  const showWorks = !work && !debounced;

  const openWork = (name: string) => {
    setWork(name); setDocumentId(''); setKind(''); setQuery(''); setResult(null);
  };
  const backToWorks = () => {
    setWork(''); setDocumentId(''); setKind(''); setQuery(''); setResult(null);
  };

  return <aside className="rl-panel rl-panel-prompts" aria-label="提示词库">
    <header className="rl-panel-head">
      <div><h2>提示词库</h2><span>{overview ? work ? `${work} · ${currentWork?.sections || 0} 段原文` : `${overview.works.length} 部作品 · ${overview.counts.sections} 段原文` : '正在读取…'}</span></div>
      <button className="rl-icon-button" aria-label="刷新索引" title="刷新索引" onClick={() => void refresh()} disabled={refreshing}>
        {refreshing ? <LoaderCircle size={14} className="sf-spin" /> : <RefreshCw size={14} />}
      </button>
    </header>
    {work && <div className="rl-scope">
      <button onClick={backToWorks}><ChevronLeft size={13} /> 全部作品</button>
      <span>{work}</span>
    </div>}
    <label className="rl-search">
      <Search size={14} />
      <input value={query} onChange={event => setQuery(event.target.value)} placeholder={work ? `在${work}里找：雨夜 / 扶车 / S3` : '找：雨夜 / 扶车 / S3 / 负面'} aria-label="检索提示词原文" />
      {query && <button aria-label="清空检索" onClick={() => setQuery('')}><X size={13} /></button>}
    </label>
    {work && currentWork && <div className="rl-chips rl-doc-chips" role="group" aria-label="按资料筛选">
      <button className={`rl-chip ${documentId === '' ? 'selected' : ''}`} onClick={() => { setDocumentId(''); setKind(''); }}>全部资料<small>{currentWork.sections}</small></button>
      {currentWork.documents.map(item => <button key={item.id} className={`rl-chip ${documentId === item.id ? 'selected' : ''}`} onClick={() => { setDocumentId(item.id); setKind(''); }}>{item.short_title}<small>{item.sections}</small></button>)}
    </div>}
    {work && <div className="rl-chips" role="group" aria-label="按类型筛选">
      <button className={`rl-chip ${selectedKind === '' ? 'selected' : ''}`} onClick={() => setKind('')}>{documentId ? '原文' : '常用'}</button>
      {kindEntries.map(([name, count]) => <button key={name} className={`rl-chip ${selectedKind === name ? 'selected' : ''}`} onClick={() => setKind(name)}>{name}<small>{count}</small></button>)}
    </div>}
    <div className="rl-panel-scroll">
      {error && <p className="sf-error" role="alert">{error}</p>}
      {overview?.config_error && <p className="rl-warn">{overview.config_error}</p>}
      {overview?.notes.map(note => <p className="rl-warn" key={note}>{note}</p>)}
      {showWorks && <div className="rl-works">
        {overview?.works.filter(item => item.sections > 0).map(item => <button className="rl-work" key={item.id} onClick={() => openWork(item.name)}>
          <span className="rl-work-title">{item.name}</span>
          <span className="rl-work-meta">{item.documents.length} 份资料 · {item.sections} 段原文{item.materials ? ` · ${item.materials} 张参考图` : ''}</span>
          <span className="rl-work-kinds">{Object.entries(item.kinds).slice(0, 4).map(([name, count]) => `${name} ${count}`).join(' · ')}</span>
          <ArrowRight size={14} className="rl-work-arrow" />
        </button>)}
      </div>}
      {searching && <p className="rl-loading"><LoaderCircle size={14} className="sf-spin" /> 正在检索原文…</p>}
      {!searching && !showWorks && result && <p className="rl-search-note">{result.note}</p>}
      {!showWorks && <div className="rl-list">
        {items.map(item => <button className="rl-card" key={item.id} onClick={() => setEntryId(item.id)}>
          <span className="rl-card-meta"><span className="rl-kind">{item.kind}</span><span className="rl-doc">{work ? documentLabel(item) : `${item.work} · ${documentLabel(item)}`}</span></span>
          <strong>{item.title}</strong>
          <p>{item.excerpt}</p>
        </button>)}
      </div>}
      {!searching && !showWorks && !items.length && !error && <p className="rl-empty"><BookOpen size={18} />没有匹配的原文。换个词，或把新文档放进资料箱后点刷新。</p>}
    </div>
    <footer className="rl-panel-foot">
      <button className="rl-foot-button" onClick={() => setAnatomyText('')}><ScanText size={13} /> 拆解一段提示词</button>
      <span className="rl-foot-hint" title={overview?.inbox_path}>{overview ? `资料箱：${overview.inbox_path.split(/[\\/]/).pop()}` : ''}</span>
    </footer>
    {entryId && <EntryDialog entryId={entryId} target={target} onClose={() => setEntryId('')} onUsePrompt={(text, item) => { setEntryId(''); onUsePrompt(text, item); }} onAnatomy={text => { setEntryId(''); setAnatomyText(text); }} />}
    {anatomyText !== null && <AnatomyDialog initialText={anatomyText} onClose={() => setAnatomyText(null)} onOpenEntry={id => { setAnatomyText(null); setEntryId(id); }} />}
  </aside>;
}

/** 嵌在单步工具弹窗里的提示词库：选中原文后写回该工具的提示词框。 */
export function PromptLibraryDialog({ target, onUse, onClose }: { target: PromptTarget; onUse: (text: string) => void; onClose: () => void }) {
  return <Modal title={`从提示词库带入${promptTargetLabels[target]}`} wide onClose={onClose}>
    <div className="rl-dialog-embed">
      <PromptLibraryPanel target={target} onUsePrompt={text => { onUse(text); onClose(); }} />
    </div>
  </Modal>;
}

function MaterialDialog({ material, onClose, onUseFrame }: { material: Material; onClose: () => void; onUseFrame: (path: string, title: string) => void }) {
  const [related, setRelated] = useState<Entry | null>(null);
  const [entryOpen, setEntryOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => {
    if (!material.related_entry_id) return;
    let cancelled = false;
    void request<Entry>(`/api/reference-library/entries/${encodeURIComponent(material.related_entry_id)}`)
      .then(data => { if (!cancelled) setRelated(data); })
      .catch(() => undefined);
    return () => { cancelled = true; };
  }, [material.related_entry_id]);
  const useFrame = async () => {
    setBusy(true); setError('');
    try {
      const quick = await request<{ project_id: string }>('/api/quick-workspace');
      const staged = await request<{ path: string }>(`/api/reference-library/materials/${encodeURIComponent(material.id)}/stage`, {
        init: { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ project_id: quick.project_id }) },
      });
      onUseFrame(staged.path, material.title);
    } catch (reason) { setError(errorText(reason, '无法把这件素材送进单步工具')); }
    finally { setBusy(false); }
  };
  return <>
    <Modal title={material.title} wide onClose={onClose}>
      <div className="rl-material-detail">
        <img src={material.file_url} alt={material.title} />
        <div className="rl-material-copy">
          <p className="rl-source">{material.source_title} · {material.category} · {material.filename}</p>
          {material.description && <><h3>资料里的参考描述</h3><pre className="rl-body">{material.description}</pre><CopyButton text={material.description} label="复制参考描述" /></>}
          {related && <><h3>相关原文</h3><button className="rl-related-link" onClick={() => setEntryOpen(true)}>{related.title} <ArrowRight size={12} /></button><p className="rl-related-excerpt">{related.plain.slice(0, 220)}{related.plain.length > 220 ? '…' : ''}</p></>}
          <p className="rl-note">只把这张图复制进单步工具的开始画面；提示词仍由你自己写，工作台不代写。</p>
          <div className="rl-actions">
            <button className="rl-action primary" disabled={busy} onClick={() => void useFrame()}>{busy ? <LoaderCircle size={13} className="sf-spin" /> : <Film size={13} />}{busy ? '准备中…' : '用作图生视频开始画面'}</button>
            <a className="rl-action" href={material.file_url} target="_blank" rel="noreferrer"><ImageIcon size={13} /> 查看原图</a>
          </div>
          {error && <p className="sf-error" role="alert">{error}</p>}
        </div>
      </div>
    </Modal>
    {entryOpen && related && <EntryDialog entryId={related.id} onClose={() => setEntryOpen(false)} />}
  </>;
}

export function MaterialLibraryPanel({ onUseFrame }: { onUseFrame: (path: string, title: string) => void }) {
  const [overview, setOverview] = useState<Overview | null>(null);
  const [error, setError] = useState('');
  const [work, setWork] = useState('');
  const [category, setCategory] = useState('全部');
  const [selected, setSelected] = useState<Material | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setError('');
    void request<Overview>('/api/reference-library')
      .then(data => { if (!cancelled) setOverview(data); })
      .catch(reason => { if (!cancelled) setError(errorText(reason, '无法读取素材参考库')); });
    return () => { cancelled = true; };
  }, [tick]);

  const refresh = async () => {
    setRefreshing(true); setError('');
    try {
      setOverview(await request<Overview>('/api/reference-library/refresh', { init: { method: 'POST' }, timeoutMs: 60_000 }));
      setTick(value => value + 1);
    } catch (reason) { setError(errorText(reason, '刷新索引失败')); }
    finally { setRefreshing(false); }
  };

  const materials = overview?.materials || [];
  const works = (overview?.works || []).filter(item => item.materials > 0);
  const scoped = work ? materials.filter(item => item.work === work) : [];
  const categories = Array.from(new Set(scoped.map(item => item.category))).filter(Boolean);
  const visible = category === '全部' ? scoped : scoped.filter(item => item.category === category);

  const categorySummary = (name: string) => {
    const counts: Record<string, number> = {};
    materials.filter(item => item.work === name).forEach(item => { counts[item.category] = (counts[item.category] || 0) + 1; });
    return Object.entries(counts).map(([label, count]) => `${label} ${count}`).join(' · ');
  };
  const openWork = (name: string) => { setWork(name); setCategory('全部'); setSelected(null); };
  const backToWorks = () => { setWork(''); setCategory('全部'); setSelected(null); };

  return <aside className="rl-panel rl-panel-material" aria-label="素材参考库">
    <header className="rl-panel-head">
      <div><h2>素材参考库</h2><span>{overview ? work ? `${work} · ${scoped.length} 张` : `${works.length} 部作品 · ${materials.length} 张参考图` : '正在读取…'}</span></div>
      <button className="rl-icon-button" aria-label="刷新素材索引" title="刷新素材索引" onClick={() => void refresh()} disabled={refreshing}>
        {refreshing ? <LoaderCircle size={14} className="sf-spin" /> : <RefreshCw size={14} />}
      </button>
    </header>
    {work && <div className="rl-scope">
      <button onClick={backToWorks}><ChevronLeft size={13} /> 全部作品</button>
      <span>{work}</span>
    </div>}
    {work && <div className="rl-chips" role="group" aria-label="按素材类型筛选">
      <button className={`rl-chip ${category === '全部' ? 'selected' : ''}`} onClick={() => setCategory('全部')}>全部<small>{scoped.length}</small></button>
      {categories.map(name => <button key={name} className={`rl-chip ${category === name ? 'selected' : ''}`} onClick={() => setCategory(name)}>{name}<small>{scoped.filter(item => item.category === name).length}</small></button>)}
    </div>}
    <div className="rl-panel-scroll">
      {error && <p className="sf-error" role="alert">{error}</p>}
      {!work && <div className="rl-works">
        {works.map(item => <button className="rl-work rl-work-material" key={item.id} onClick={() => openWork(item.name)}>
          <span className="rl-work-covers">{item.covers.map(src => <img key={src} src={src} alt="" loading="lazy" />)}</span>
          <span className="rl-work-title">{item.name}</span>
          <span className="rl-work-meta">{item.materials} 张参考图{item.documents.length ? ` · ${item.documents.length} 份资料` : ''}</span>
          <span className="rl-work-kinds">{categorySummary(item.name)}</span>
          <ArrowRight size={14} className="rl-work-arrow" />
        </button>)}
      </div>}
      {work && <div className="rl-mat-grid">
        {visible.map(item => <button className="rl-mat" key={item.id} onClick={() => setSelected(item)}>
          <img src={item.thumb_url} alt={item.title} loading="lazy" />
          <span>{item.title}</span>
          <small>{item.category}</small>
        </button>)}
      </div>}
      {!work && !works.length && !error && <p className="rl-empty"><Images size={18} />还没有素材。把图片放进资料箱，或检查素材文件夹是否存在。</p>}
      {work && !visible.length && !error && <p className="rl-empty"><Images size={18} />这部作品在这个分类里还没有素材。</p>}
    </div>
    <footer className="rl-panel-foot">
      <span className="rl-foot-hint" title={overview?.config_path}>{overview ? `来源：${overview.documents.length} 份文档 · ${materials.length} 张图` : ''}</span>
    </footer>
    {selected && <MaterialDialog material={selected} onClose={() => setSelected(null)} onUseFrame={onUseFrame} />}
  </aside>;
}

function HintRow({ item }: { item: Summary }) {
  const [open, setOpen] = useState(false);
  const [entry, setEntry] = useState<Entry | null>(null);
  const [error, setError] = useState('');
  const toggle = async () => {
    if (open) { setOpen(false); return; }
    setOpen(true);
    if (entry) return;
    try { setEntry(await request<Entry>(`/api/reference-library/entries/${encodeURIComponent(item.id)}`)); }
    catch (reason) { setError(errorText(reason, '无法读取原文')); }
  };
  return <article className="rl-hint">
    <div className="rl-hint-head"><strong>{item.title}</strong><button onClick={() => void toggle()}>{open ? '收起' : '展开原文'}</button></div>
    <p className="rl-hint-source">{item.document_title} · 第 {item.line} 行 · {item.kind}</p>
    {open
      ? error ? <p className="sf-error" role="alert">{error}</p>
        : entry ? <pre className="rl-hint-body open">{entry.plain}</pre>
          : <p className="rl-loading"><LoaderCircle size={13} className="sf-spin" /> 正在读取原文…</p>
      : <p className="rl-hint-body">{item.excerpt}</p>}
  </article>;
}

/** 新建项目时的资料提示：只按输入的字词检索原文，不生成也不预填。 */
export function ReferenceHints({ query, limit = 3 }: { query: string; limit?: number }) {
  const debounced = useDebounced(query.trim(), 350);
  const [items, setItems] = useState<Summary[] | null>(null);
  const [loading, setLoading] = useState(false);
  useEffect(() => {
    if (debounced.length < 2) { setItems(null); return; }
    let cancelled = false;
    setLoading(true);
    void request<SearchResult>(`/api/reference-library/search?q=${encodeURIComponent(debounced)}&limit=${limit}`)
      .then(data => { if (!cancelled) setItems(data.items || []); })
      .catch(() => { if (!cancelled) setItems([]); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [debounced, limit]);
  if (debounced.length < 2) return null;
  return <section className="rl-hints" aria-label="资料里相关的原文">
    <p className="rl-hints-head">
      {loading ? <><LoaderCircle size={12} className="sf-spin" /> 正在检索资料…</>
        : items?.length ? <>资料里相关的原文<small>只按原文匹配，不代写</small></> : '资料里没有匹配的原文'}
    </p>
    {items?.map(item => <HintRow key={item.id} item={item} />)}
  </section>;
}
