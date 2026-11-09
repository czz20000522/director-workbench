import { useEffect, useRef, useState } from 'react';
import {
  BookMarked, BookmarkPlus, LoaderCircle, Maximize2, Minimize2, PenLine,
  Settings2, Sparkles, Trash2, Wand2, X,
} from 'lucide-react';
import { request } from './api/client';

/** 提示词助手：自己写的能润色，没写的能代写。
 *
 * 两条通道，用户显式切换：
 *  - 本地：本机 Ollama 的无审查模型，成人向描述不会被拒答；点生成才拉起服务。
 *  - 在线：任意 OpenAI 兼容端点（默认 DeepSeek），Key 存本机数据根，不上传。
 *
 * 两个动作：
 *  - 「润色这段」：把输入框里的草稿（默认就是你正在写的那段）保留原意改好；
 *  - 「写一段」：把一句想法展开成完整画面描述。
 *
 * 三种形态共用一份 state：
 *  - 生成表单里的内联按钮 + 小面板；
 *  - 展开后的全屏二级工作台（左写右库）。
 */

type ProviderState = {
  available: boolean;
  model: string;
  reason: string;
  hint?: string;
  running?: boolean;
  can_start?: boolean;
  base_url?: string;
  has_key?: boolean;
  key_hint?: string;
};
type Capability = {
  available: boolean;
  running: boolean;
  model: string;
  reason: string;
  hint?: string;
  can_start?: boolean;
  keep_alive: string;
  default: 'local' | 'online';
  providers: { local: ProviderState; online: ProviderState };
};
type Draft = {
  prompt: string;
  provider: 'local' | 'online';
  mode: 'compose' | 'polish';
  model: string;
  elapsed_ms: number;
  started_service?: boolean;
  startup_ms?: number;
};
type ConfigView = {
  provider: 'local' | 'online';
  local: { model: string; keep_alive: string };
  online: { base_url: string; model: string; has_key: boolean; key_hint: string };
};

export type PromptLink = {
  project_id?: string;
  preset_id?: string;
  profile?: string;
  aspect?: string;
  seed?: number | null;
  materials?: { role: string; label: string; material_id?: string }[];
  result?: string;
};

type LibraryItem = {
  id: string;
  text: string;
  title: string;
  tags: string[];
  note: string;
  source: string;
  idea: string;
  model: string;
  links: PromptLink;
  uses: number;
  created_at: number;
  updated_at: number;
};

export type PromptApply = {
  /** 这条配方在收藏里的 id：生成成功后要把产物回写到它身上。 */
  itemId?: string;
  profile?: string;
  aspect?: string;
  seed?: number | null;
  materials?: { role: string; label: string; material_id?: string }[];
};

const SOURCE_LABELS: Record<string, string> = {
  manual: '手写', draft: '代写', generation: '生成', import: '导入',
};
const ASPECT_LABELS: Record<string, string> = {
  portrait: '竖屏', landscape: '横屏', square: '方图',
};
const PROFILE_LABELS: Record<string, string> = {
  general: '常规', 'adult-realism': '写实·成人向', 'adult-anime': '二次元·成人向',
};

function shorten(value: string, limit: number): string {
  const text = value.trim();
  return text.length > limit ? `${text.slice(0, limit)}…` : text;
}

export default function PromptWriter({ disabled, prompt, context, onUse, label, embedded, libraryOnly }: {
  disabled?: boolean;
  /** 输入框里当前这段：打开面板时预填，可直接润色或存入收藏。 */
  prompt: string;
  context: PromptLink;
  onUse: (text: string, extra?: PromptApply) => void;
  label?: string;
  /** 嵌进别的面板（提示词库展开页）时用：不渲染触发按钮和弹层，直接给写作区。 */
  embedded?: boolean;
  /** 只要收藏列表，不要写作区：提示词库展开页的「我的收藏」用它。 */
  libraryOnly?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const [tab, setTab] = useState<'write' | 'library'>('write');
  const [input, setInput] = useState('');
  const [draft, setDraft] = useState('');
  const [lastMode, setLastMode] = useState<'compose' | 'polish'>('polish');
  const [provider, setProvider] = useState<'local' | 'online'>('local');
  const [busy, setBusy] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [capability, setCapability] = useState<Capability | null>(null);
  const [config, setConfig] = useState<ConfigView | null>(null);
  const [configOpen, setConfigOpen] = useState(false);
  const [form, setForm] = useState({ base_url: '', model: '', api_key: '' });
  const [withMaterials, setWithMaterials] = useState(true);
  const [withParams, setWithParams] = useState(false);
  const [items, setItems] = useState<LibraryItem[]>([]);
  const [total, setTotal] = useState(0);
  const [query, setQuery] = useState('');
  const [loadingLibrary, setLoadingLibrary] = useState(false);
  const [composing, setComposing] = useState(false);
  const [newText, setNewText] = useState('');
  // 外部已经有提示词时，不再摆一个同样大的输入框（跟上面重合）。默认显示一行摘要，
  // 点「改这段」才展开编辑；提示词库那种没有外部输入框的场景（embedded）始终展开。
  const [editingSource, setEditingSource] = useState(false);
  const ideaRef = useRef<HTMLTextAreaElement>(null);
  const searchSeq = useRef(0);

  const materials = context.materials || [];
  const active = capability?.providers?.[provider];
  const ready = Boolean(active?.available);
  const externalText = prompt.trim();
  const showInput = embedded || !externalText || editingSource;

  useEffect(() => {
    let alive = true;
    request<Capability>('/api/prompt-writer')
      .then(value => {
        if (!alive) return;
        setCapability(value);
        setProvider(value.default === 'online' ? 'online' : 'local');
      })
      .catch(() => { if (alive) setCapability(null); });
    return () => { alive = false; };
  }, []);

  // 打开面板时把正在写的那段带进来：润色的默认对象就是它。
  useEffect(() => {
    if (!open && !embedded) return;
    setInput(current => (current.trim() ? current : prompt));
    // 有外部提示词就先收着，只显示一行摘要；没有才直接给输入框。
    setEditingSource(embedded || !prompt.trim());
    // prompt 只在打开这一刻取快照，之后外部编辑不该覆盖用户正在改的草稿。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, embedded]);

  // 大页面打开时 Esc 只关这一层：用捕获阶段拦住，别让下层工作台一起响应。
  useEffect(() => {
    if (!expanded) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      event.preventDefault();
      event.stopPropagation();
      setExpanded(false);
    };
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  }, [expanded]);

  const loadConfig = async () => {
    try {
      const value = await request<ConfigView>('/api/prompt-writer/config');
      setConfig(value);
      setForm({
        base_url: value.online.base_url || '',
        model: value.online.model || '',
        api_key: '',
      });
    } catch { /* 配置读不到不影响写词 */ }
  };

  const openConfig = () => {
    setConfigOpen(true);
    if (!config) void loadConfig();
  };

  const saveConfig = async () => {
    setSaving(true); setError(''); setNotice('');
    try {
      const next = await request<ConfigView>('/api/prompt-writer/config', {
        json: {
          provider,
          online: {
            ...(form.base_url.trim() ? { base_url: form.base_url.trim() } : {}),
            ...(form.model.trim() ? { model: form.model.trim() } : {}),
            ...(form.api_key ? { api_key: form.api_key } : {}),
          },
        },
      });
      setConfig(next);
      setNotice('在线接口已保存（Key 只存在本机数据根）');
      setConfigOpen(false);
      const state = await request<Capability>('/api/prompt-writer');
      setCapability(state);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '保存失败');
    } finally { setSaving(false); }
  };

  const chooseProvider = async (next: 'local' | 'online') => {
    setProvider(next); setError(''); setNotice('');
    // 只切通道，不动输入和结果：用户经常是「本地试一版，切在线再试一版」，
    // 清掉就得重打一遍。要清有单独的「清空」。
    try {
      await request('/api/prompt-writer/config', { json: { provider: next } });
    } catch { /* 记住选择失败不影响本次调用 */ }
  };

  const loadLibrary = async (needle: string) => {
    const seq = searchSeq.current + 1;
    searchSeq.current = seq;
    setLoadingLibrary(true);
    try {
      const suffix = needle.trim() ? `?query=${encodeURIComponent(needle.trim())}` : '';
      const data = await request<{ items: LibraryItem[]; total: number }>(`/api/prompt-library${suffix}`);
      if (searchSeq.current !== seq) return;
      setItems(data.items || []);
      setTotal(data.total || 0);
    } catch (reason) {
      if (searchSeq.current === seq) setError(reason instanceof Error ? reason.message : '词库读取失败');
    } finally {
      if (searchSeq.current === seq) setLoadingLibrary(false);
    }
  };

  useEffect(() => {
    if ((!open && !expanded && !embedded) || (tab !== 'library' && !expanded && !embedded)) return;
    const timer = setTimeout(() => void loadLibrary(query), query ? 260 : 0);
    return () => clearTimeout(timer);
    // context 不进依赖：它每次渲染都是新对象，会把搜索打成死循环。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, tab, expanded, embedded, query]);

  const run = async (mode: 'compose' | 'polish') => {
    const text = (mode === 'polish' ? (input.trim() || prompt.trim()) : input.trim());
    if (!text) {
      setError(mode === 'polish' ? '先自己写一段，或在生成表单里写点东西' : '先写一句想法');
      return;
    }
    setBusy(true); setError(''); setNotice(''); setDraft(''); setLastMode(mode);
    try {
      const result = await request<Draft>('/api/prompt-writer/draft', {
        json: {
          idea: text,
          provider,
          mode,
          ...(withMaterials && materials.length ? { context: { materials: materials.slice(0, 8) } } : {}),
        },
        // 首次加载要读权重；服务没起时后端还要先拉起它，30 秒不够。
        timeoutMs: 300_000,
      });
      setDraft(result.prompt);
      if (result.started_service) {
        setNotice(`已拉起本机服务（${((result.startup_ms || 0) / 1000).toFixed(1)} 秒），${capability?.keep_alive || '30s'} 后退显存`);
      } else if (result.provider === 'online') {
        setNotice(`由在线模型 ${result.model} 生成`);
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '生成失败');
    } finally { setBusy(false); }
  };

  /** 「写一段」在没有输入框时先把输入框放出来，别让人对着按钮发呆。 */
  const startCompose = () => {
    if (!showInput) {
      setEditingSource(true);
      setInput('');
      setNotice('写一句想法，再点「写一段」');
      requestAnimationFrame(() => ideaRef.current?.focus());
      return;
    }
    void run('compose');
  };

  const clearAll = () => {
    setInput('');
    setDraft('');
    setError('');
    setNotice('');
    setEditingSource(true);
    requestAnimationFrame(() => ideaRef.current?.focus());
  };

  const save = async (text: string, source: 'manual' | 'draft', fromIdea = '') => {
    const body = text.trim();
    if (!body) { setError('没有可存的提示词'); return; }
    setSaving(true); setError(''); setNotice('');
    try {
      const links: PromptLink = { ...context };
      if (!links.materials?.length) delete links.materials;
      const result = await request<{ item: LibraryItem; created: boolean }>('/api/prompt-library', {
        json: {
          text: body,
          source,
          idea: fromIdea,
          model: active?.model || capability?.model || '',
          links,
        },
      });
      setNotice(result.created ? '已存入收藏' : '收藏里已有这段，关联已合并');
      try { window.dispatchEvent(new Event('director:prompt-library-changed')); } catch { /* 忽略 */ }
      if ((open && tab === 'library') || expanded) await loadLibrary(query);
      else setTotal(current => (result.created ? current + 1 : current));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '存入失败');
    } finally { setSaving(false); }
  };

  const apply = (item: LibraryItem) => {
    // 即使不还原参数，也要把 id 递出去：生成成功后由工作台回写产物。
    const extra: PromptApply = {
      itemId: item.id,
      ...(withParams ? {
        profile: item.links.profile,
        aspect: item.links.aspect,
        seed: item.links.seed ?? null,
        materials: item.links.materials,
      } : {}),
    };
    onUse(item.text, extra);
    setInput(item.text);
    setNotice(`已填入「${shorten(item.title, 12)}」`);
  };

  const remove = async (item: LibraryItem) => {
    if (!window.confirm(`从收藏里删掉「${shorten(item.title, 20)}」？只删这条记录，已经生成的东西不受影响。`)) return;
    try {
      await request(`/api/prompt-library/${encodeURIComponent(item.id)}`, { init: { method: 'DELETE' } });
      try { window.dispatchEvent(new Event('director:prompt-library-changed')); } catch { /* 忽略 */ }
      setItems(current => current.filter(entry => entry.id !== item.id));
      setTotal(current => Math.max(0, current - 1));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '删除失败');
    }
  };

  const hint = capability
    ? (ready ? (active?.hint || '点生成就出词') : active?.reason || '')
    : '正在检查本机模型…';

  const closeAll = () => { setExpanded(false); setOpen(false); };

  /** 通道切换 + 在线配置入口。 */
  const providerBar = <div className="pw-provider">
    <div className="ffs-write-tabs" role="tablist" aria-label="模型通道">
      <button type="button" role="tab" aria-selected={provider === 'local'}
        onClick={() => void chooseProvider('local')}>本地</button>
      <button type="button" role="tab" aria-selected={provider === 'online'}
        onClick={() => void chooseProvider('online')}>在线</button>
    </div>
    <small title={active?.model || ''}>
      {ready ? shorten(active?.model || '', 26) : (provider === 'online' ? '未配置' : '未就绪')}
    </small>
    {provider === 'online' && <button type="button" className="ffs-write-x" aria-label="在线接口设置"
      title="在线接口设置" onClick={openConfig}><Settings2 size={12} /></button>}
  </div>;

  /** 写作区：小面板与二级页共用，只有尺寸不同。 */
  const writePane = (large: boolean) => <>
    {showInput
      ? <textarea ref={ideaRef} rows={large ? 5 : 3} value={input} disabled={busy}
        className={large ? 'pw-idea pw-idea-lg' : 'ffs-write-idea'}
        onChange={event => setInput(event.target.value)}
        placeholder="把自己写的草稿贴进来点「润色这段」，或者只写一句想法点「写一段」" />
      : <div className="pw-source">
        <span title={externalText}>润色对象：{shorten(input.trim() || externalText, 64)}</span>
        <button type="button" onClick={() => setEditingSource(true)}>改这段</button>
      </div>}
    {materials.length > 0 && <label className="ffs-write-check">
      <input type="checkbox" checked={withMaterials} disabled={busy}
        onChange={event => setWithMaterials(event.target.checked)} />
      把已选的 {materials.length} 张素材一起告诉它
    </label>}
    <p className={ready ? 'ffs-write-hint is-ready' : 'ffs-write-hint'}>
      {hint}
      {provider === 'online' && !ready && <button type="button" className="pw-hint-link" onClick={openConfig}>填接口</button>}
    </p>
    <div className="ffs-write-actions">
      <button type="button" className="sf-secondary" disabled={saving || !(input.trim() || externalText)}
        onClick={() => void save(input.trim() || externalText, 'manual')}>
        <BookmarkPlus size={12} /> 存这段
      </button>
      <button type="button" className="sf-secondary" disabled={busy || disabled || !(input.trim() || externalText)}
        onClick={() => void run('polish')}>
        {busy && lastMode === 'polish'
          ? <><LoaderCircle size={13} className="sf-spin" /> 润色中…</>
          : <><Wand2 size={12} /> 润色这段</>}
      </button>
      <button type="button" className="sf-primary" disabled={busy || disabled} onClick={startCompose}>
        {busy && lastMode === 'compose'
          ? <><LoaderCircle size={13} className="sf-spin" /> 写作中…</>
          : <><PenLine size={12} /> 写一段</>}
      </button>
    </div>
  </>;

  /** 结果区：直接可改，改完再填进画面描述。 */
  const resultPane = (large: boolean) => draft ? <div className="ffs-write-result">
    <div className="ffs-write-result-head">
      <span>{lastMode === 'polish' ? '润色结果' : '写出来的描述'}{large ? '（可以直接改）' : ''}</span>
      <small>{draft.length} 字</small>
    </div>
    <textarea rows={large ? 7 : 3} value={draft} className="pw-result"
      onChange={event => setDraft(event.target.value)} />
    <div className="ffs-write-actions">
      <button type="button" className="pw-clear" onClick={clearAll}>清空</button>
      <button type="button" className="sf-secondary" disabled={saving}
        onClick={() => void save(draft, 'draft', input.trim() || externalText)}><BookmarkPlus size={12} /> 存进收藏</button>
      <button type="button" className="sf-primary" onClick={() => onUse(draft)}>{embedded ? '复制' : '用这段'}</button>
    </div>
  </div> : null;

  /** 收藏区：搜索 + 直接新建 + 列表。 */
  const libraryPane = (large: boolean) => <>
    <div className="ffs-lib-bar">
      <input value={query} onChange={event => setQuery(event.target.value)}
        placeholder="搜词、标签、素材名、作品…" aria-label="搜索收藏" />
      <button type="button" className="sf-secondary" onClick={() => setComposing(value => !value)}>
        <PenLine size={12} /> 写一条
      </button>
    </div>
    {composing && <div className="pw-new">
      <textarea rows={4} value={newText} className="pw-result"
        onChange={event => setNewText(event.target.value)}
        placeholder="直接写一条提示词存起来；不需要先生成。标点和换行都会保留。" />
      <div className="ffs-write-actions">
        <button type="button" className="sf-secondary" onClick={() => { setComposing(false); setNewText(''); }}>取消</button>
        <button type="button" className="sf-primary" disabled={saving || !newText.trim()}
          onClick={() => void save(newText, 'manual').then(() => { setNewText(''); setComposing(false); })}>存进收藏</button>
      </div>
    </div>}
    <label className="ffs-write-check">
      <input type="checkbox" checked={withParams} onChange={event => setWithParams(event.target.checked)} />
      填入时连同画幅 / 画质 / 种子一起还原
    </label>
    {loadingLibrary && items.length === 0 && <p className="ffs-lib-empty">读取收藏…</p>}
    {!loadingLibrary && items.length === 0 && <p className="ffs-lib-empty">
      {total === 0
        ? '还没有收藏。用「存这段」或「写一条」存进来；左栏「提示词库」放的是资料原文，这里放你自己验过的配方。'
        : '没有匹配的条目，换个词试试。'}
    </p>}
    {items.length > 0 && <ul className="ffs-lib-list">{items.map(item => {
      const meta = [
        ASPECT_LABELS[item.links.aspect || ''] || '',
        PROFILE_LABELS[item.links.profile || ''] || '',
        item.links.materials?.length ? `素材 ×${item.links.materials.length}` : '',
        ...item.tags.slice(0, 2).map(tag => `#${tag}`),
      ].filter(Boolean).join(' · ');
      const body = item.text.startsWith(item.title)
        ? item.text.slice(item.title.length).replace(/^[，。、；：！？\s]+/, '')
        : item.text;
      return <li className="ffs-lib-item" key={item.id}>
        <div className="ffs-lib-item-head">
          <strong title={item.title}>{item.title}</strong>
          <small>{SOURCE_LABELS[item.source] || item.source}{item.uses ? ` · 用过 ${item.uses} 次` : ''}</small>
        </div>
        <p title={item.text}>{large ? (body || item.text) : shorten(body || item.text, 110)}</p>
        <div className="ffs-lib-item-foot">
          <small>{meta || new Date(item.updated_at).toLocaleDateString('zh-CN')}</small>
          <div className="ffs-lib-item-actions">
            <button type="button" className="sf-secondary" onClick={() => apply(item)}>填入</button>
            <button type="button" className="ffs-lib-del" title="删除这条" aria-label="删除这条"
              onClick={() => void remove(item)}><Trash2 size={12} /></button>
          </div>
        </div>
      </li>;
    })}</ul>}
    {items.length > 0 && <p className="ffs-lib-foot">
      <BookMarked size={11} /> 共 {total} 条{items.length < total ? `，当前显示 ${items.length} 条` : ''}
    </p>}
  </>;

  /** 在线接口配置：只在选了「在线」时出现。 */
  const configPane = configOpen ? <div className="pw-config">
    <div className="pw-config-row">
      <label>接口地址<input value={form.base_url} placeholder="https://api.deepseek.com/v1"
        onChange={event => setForm({ ...form, base_url: event.target.value })} /></label>
      <label>模型<input value={form.model} placeholder="deepseek-chat"
        onChange={event => setForm({ ...form, model: event.target.value })} /></label>
    </div>
    <label>API Key
      <input type="password" value={form.api_key} autoComplete="off"
        placeholder={config?.online.has_key ? `已保存 ${config.online.key_hint}，留空表示不改` : 'sk-...'}
        onChange={event => setForm({ ...form, api_key: event.target.value })} />
    </label>
    <div className="ffs-write-actions">
      <small>Key 只写本机 <code>director-data/config/prompt-writer.json</code>，不会发给任何第三方。</small>
      <button type="button" className="sf-secondary" onClick={() => setConfigOpen(false)}>取消</button>
      <button type="button" className="sf-primary" disabled={saving} onClick={() => void saveConfig()}>保存</button>
    </div>
  </div> : null;

  const messages = <>
    {error && <p className="sf-error" role="alert">{error}</p>}
    {notice && <p className="sf-fine ffs-write-notice">{notice}</p>}
  </>;

  /** 嵌进提示词库展开页：不弹层，直接把「写 / 润色 + 收藏」铺开。 */
  // 只要收藏：提示词库展开页的第三个功能区，避免把写作区也铺出来。
  if (embedded && libraryOnly) {
    return <div className="pw-embed is-library-only">
      <aside className="pw-embed-lib">
        <h5>润色提示词<small>共 {total} 条 · 从润色或写作结果存下来的</small></h5>
        <div className="pw-lib-scroll">{libraryPane(true)}</div>
      </aside>
    </div>;
  }

  if (embedded) {
    return <div className="pw-embed">
      <div className="pw-embed-head">
        <h4>写提示词 · 润色<small>{provider === 'local' ? `本地 ${active?.model || ''}` : `在线 ${active?.model || ''}`}</small></h4>
        {providerBar}
      </div>
      <div className="pw-embed-body">
        <section className="pw-embed-write">
          {writePane(true)}
          {configPane}
          {resultPane(true)}
          {messages}
        </section>
        <aside className="pw-embed-lib">
          <h5>润色提示词<small>共 {total} 条</small></h5>
          <div className="pw-lib-scroll">{libraryPane(true)}</div>
        </aside>
      </div>
    </div>;
  }

  if (!open) {
    return <button type="button" className="ffs-write-trigger" disabled={disabled}
      onClick={() => { setOpen(true); setError(''); setNotice(''); }}>
      <Sparkles size={12} /> {label || 'AI 润色'}
      {total > 0 && <span className="ffs-write-badge">{total}</span>}
    </button>;
  }

  if (expanded) {
    return <div className="pw-stage" role="dialog" aria-modal="true" aria-label="提示词工作台">
      <div className="pw-stage-card">
        <header className="pw-stage-head">
          <div className="pw-stage-title">
            <strong>提示词工作台</strong>
            <small>
              <span className={ready ? 'pw-dot is-on' : 'pw-dot'} />
              {provider === 'local' ? '本地' : '在线'} · {active?.model || '…'}
              <em>{ready ? '就绪' : (provider === 'local' && active?.can_start ? '按需启动' : '未就绪')}</em>
            </small>
          </div>
          <div className="pw-stage-head-actions">
            <button type="button" className="sf-secondary" onClick={() => setExpanded(false)}>
              <Minimize2 size={12} /> 收进侧栏
            </button>
            <button type="button" className="ffs-write-x" aria-label="关闭" onClick={closeAll}><X size={14} /></button>
          </div>
        </header>
        <div className="pw-stage-body">
          <section className="pw-stage-write">
            <div className="pw-stage-section">
              <h4>润色 / 代写
                <small>{provider === 'local' ? '本机模型，点生成才拉起，写完自动退显存' : '你配的在线接口，按平台规则可能拒答成人向'}</small>
              </h4>
              {providerBar}
              {writePane(true)}
              {configPane}
            </div>
            <div className="pw-stage-section">
              {resultPane(true) || <p className="pw-stage-empty">
                润色或写出来的描述会出现在这里，可以直接改字，再点「用这段」填回画面描述。
              </p>}
            </div>
            {messages}
          </section>
          <aside className="pw-stage-lib">
            <h4>收藏<small>写一条、存一段，随时删。</small></h4>
            <div className="pw-lib-scroll">{libraryPane(true)}</div>
          </aside>
        </div>
        <footer className="pw-stage-foot">
          <span>本地通道：点生成才拉起 Ollama，{capability?.keep_alive || '30s'} 后退显存；出片前还会主动卸一次。</span>
          <span>Esc 收起</span>
        </footer>
      </div>
    </div>;
  }

  return <div className="ffs-write-panel">
    <div className="ffs-write-head">
      <div className="ffs-write-tabs" role="tablist">
        <button type="button" role="tab" aria-selected={tab === 'write'}
          onClick={() => { setTab('write'); setError(''); setNotice(''); }}>润色</button>
        <button type="button" role="tab" aria-selected={tab === 'library'}
          onClick={() => { setTab('library'); setError(''); setNotice(''); }}>
          收藏{total > 0 ? ` ${total}` : ''}
        </button>
      </div>
      <div className="ffs-write-head-actions">
        <button type="button" className="ffs-write-expand" title="展开成工作台"
          onClick={() => { setExpanded(true); setError(''); setNotice(''); }}>
          <Maximize2 size={12} /> 展开
        </button>
        <button type="button" className="ffs-write-x" aria-label="收起" onClick={() => setOpen(false)}><X size={13} /></button>
      </div>
    </div>

    {tab === 'write' ? <>
      {providerBar}
      {writePane(false)}
      {configPane}
      {resultPane(false)}
    </> : libraryPane(false)}

    {messages}
  </div>;
}
