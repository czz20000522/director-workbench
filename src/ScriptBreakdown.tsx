import { useEffect, useMemo, useState } from 'react';
import { AlertCircle, Check, LoaderCircle, Plus, Save, Sparkles, Trash2 } from 'lucide-react';
import { ApiError, request } from './api/client';
import './script-breakdown.css';

/** 剧本 → 素材清单。
 *
 * AI 只出**建议**：每一条都能改标题和描述、能勾掉、能删、能补。确认后才轮到生成，
 * 所以这里不写任何「自动开跑」的逻辑——清单本身就是那道人工闸门。
 */
type ItemType = 'character' | 'scene' | 'prop' | 'voice' | 'music';
type Item = { id: string; type: ItemType; title: string; prompt: string; voice?: string; note?: string; enabled: boolean };
type Breakdown = { items: Item[]; confirmed?: boolean; provider?: string; model?: string; truncated?: boolean; extracted_at?: number };
type Draft = { id: string; title: string; content: string; breakdown: Breakdown; stale?: boolean };

const TYPES: { id: ItemType; label: string }[] = [
  { id: 'character', label: '角色' },
  { id: 'scene', label: '场景' },
  { id: 'prop', label: '道具' },
  { id: 'voice', label: '音色' },
  { id: 'music', label: '配乐' },
];

const labelOf = (type: ItemType) => TYPES.find(entry => entry.id === type)?.label || type;
const emptyItem = (type: ItemType): Item => ({
  id: `local-${crypto.randomUUID().slice(0, 8)}`, type, title: '', prompt: '', voice: '', note: '', enabled: true,
});

export default function ScriptBreakdown({ draftId, onChanged }: { draftId: string; onChanged?: () => void }) {
  const [draft, setDraft] = useState<Draft | null>(null);
  const [items, setItems] = useState<Item[]>([]);
  const [saved, setSaved] = useState('');
  const [tab, setTab] = useState<'all' | ItemType>('all');
  const [provider, setProvider] = useState<'auto' | 'local' | 'online'>('auto');
  const [busy, setBusy] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [askExtract, setAskExtract] = useState(false);

  const apply = (next: Draft) => {
    setDraft(next);
    setItems(next.breakdown?.items || []);
    setSaved(JSON.stringify(next.breakdown?.items || []));
  };

  useEffect(() => {
    let alive = true;
    setDraft(null); setItems([]); setSaved(''); setError(''); setNotice(''); setAskExtract(false); setTab('all');
    void request<{ draft: Draft }>(`/api/script-library/drafts/${encodeURIComponent(draftId)}`)
      .then(data => { if (alive) apply(data.draft); })
      .catch(reason => { if (alive) setError(reason instanceof ApiError ? reason.message : '清单读取失败'); });
    return () => { alive = false; };
  }, [draftId]);

  const dirty = useMemo(() => JSON.stringify(items) !== saved, [items, saved]);
  const counts = useMemo(() => {
    const result: Record<string, number> = { all: items.length };
    for (const item of items) result[item.type] = (result[item.type] || 0) + 1;
    return result;
  }, [items]);
  const visible = tab === 'all' ? items : items.filter(item => item.type === tab);
  const enabledCount = items.filter(item => item.enabled).length;
  const confirmed = Boolean(draft?.breakdown?.confirmed);

  const patch = (id: string, change: Partial<Item>) => {
    setItems(current => current.map(item => item.id === id ? { ...item, ...change } : item));
    setNotice('');
  };

  const extract = async () => {
    setAskExtract(false);
    setBusy(true); setError(''); setNotice('');
    try {
      // 拆一份一万多字的剧本，在线通道实测 30 秒上下，本地首次加载更久；
      // 默认 30 秒会先被页面掐断，后端其实还在跑。
      const data = await request<{ draft: Draft; count: number }>(
        `/api/script-library/drafts/${encodeURIComponent(draftId)}/breakdown`,
        { json: { provider }, timeoutMs: 300_000 });
      apply(data.draft);
      setNotice(`拆出 ${data.count} 项。逐条看一遍：改完、勾完再点「确认清单」。`);
      onChanged?.();
    } catch (reason) {
      const message = reason instanceof ApiError ? reason.message : '拆解未完成';
      // 页面超时 ≠ 后端失败：模型往往在后台跑完并落库了。回读一次，能救就救，
      // 不让用户再花半分钟重跑一遍。
      try {
        const latest = await request<{ draft: Draft }>(`/api/script-library/drafts/${encodeURIComponent(draftId)}`);
        const before = draft?.breakdown?.extracted_at || 0;
        const after = latest.draft.breakdown?.extracted_at || 0;
        if (after > before && latest.draft.breakdown.items.length) {
          apply(latest.draft);
          setNotice(`模型其实已经跑完了（${latest.draft.breakdown.items.length} 项），刚才只是页面等不及；已载入。`);
          onChanged?.();
          return;
        }
      } catch { /* 回读也失败：用原来的错误提示 */ }
      setError(message);
    } finally { setBusy(false); }
  };

  const save = async (confirm: boolean) => {
    if (saving) return;
    setSaving(true); setError(''); setNotice('');
    try {
      const data = await request<{ draft: Draft }>(
        `/api/script-library/drafts/${encodeURIComponent(draftId)}/breakdown`,
        { init: { method: 'PUT' }, json: { items, confirmed: confirm } });
      apply(data.draft);
      setNotice(confirm ? `已确认 ${data.draft.breakdown.items.filter(item => item.enabled).length} 项，下一步才会按清单生成。` : '清单已保存。');
      onChanged?.();
    } catch (reason) {
      setError(reason instanceof ApiError ? reason.message : '保存未完成');
    } finally { setSaving(false); }
  };

  const add = () => {
    const type: ItemType = tab === 'all' ? 'character' : tab;
    const fresh = emptyItem(type);
    setItems(current => [...current, fresh]);
    setNotice('已加一条空条目：先写标题和描述，再勾选。');
  };

  const remove = (id: string) => {
    setItems(current => current.filter(item => item.id !== id));
    setNotice('');
  };

  if (!draft && !error) {
    return <p className="sb-loading"><LoaderCircle size={15} className="sf-spin" /> 正在读这份剧本…</p>;
  }

  return <section className="sb" aria-label="剧本素材清单">
    <header className="sb-head">
      <div>
        <strong>素材清单</strong>
        <small>{items.length ? `${enabledCount} / ${items.length} 项将进入生成` : '还没有拆解'}</small>
      </div>
      <div className="sb-head-actions">
        <select value={provider} disabled={busy} onChange={event => setProvider(event.target.value as typeof provider)}
          aria-label="拆解通道" title="本地通道不联网；在线通道更快但受平台内容规则约束">
          <option value="auto">自动通道</option>
          <option value="local">本地模型</option>
          <option value="online">在线模型</option>
        </select>
        {askExtract
          ? <>
              <button type="button" className="sb-danger" onClick={() => void extract()}>确认重拆（会覆盖当前修改）</button>
              <button type="button" onClick={() => setAskExtract(false)}>取消</button>
            </>
          : <button type="button" onClick={() => (items.length ? setAskExtract(true) : void extract())} disabled={busy || !draft?.content?.trim()}>
              {busy ? <><LoaderCircle size={13} className="sf-spin" /> 正在读剧本…</> : <><Sparkles size={13} /> {items.length ? '重新拆解' : 'AI 拆解剧本'}</>}
            </button>}
      </div>
    </header>

    {draft?.stale && items.length > 0 && <p className="sb-warn"><AlertCircle size={13} /> 正文在拆解后改过，清单可能对不上；建议重新拆解，或手工增删。</p>}
    {draft?.breakdown?.truncated && <p className="sb-warn"><AlertCircle size={13} /> 正文较长，只拆了前 1.6 万字；后半段可以等拆完再补。</p>}
    {error && <p className="sb-error" role="alert">{error}</p>}

    {!items.length ? <div className="sb-empty">
      <Sparkles size={20} />
      <strong>让 AI 先读一遍，出一份素材清单</strong>
      <p>它会抽角色、场景、道具，角色写了声线就顺带出一条音色，情绪段落出一条配乐。只出建议，不会生成任何图或声音。</p>
      <button type="button" className="sf-primary" disabled={busy || !draft?.content?.trim()} onClick={() => void extract()}>
        {busy ? '正在拆解…' : '开始拆解'}
      </button>
      <small className="sb-note">在线通道通常 20–60 秒；本地模型首次加载会更久，跑起来别关页面。</small>
    </div> : <>
      <div className="sb-tabs" role="group" aria-label="按类型筛选">
        {[{ id: 'all' as const, label: '全部' }, ...TYPES].map(entry => <button key={entry.id} type="button"
          aria-pressed={tab === entry.id} onClick={() => setTab(entry.id)}>
          {entry.label}<span>{counts[entry.id] || 0}</span>
        </button>)}
      </div>

      <div className="sb-list">
        {visible.map(item => <article key={item.id} className={`sb-item${item.enabled ? '' : ' is-off'} sb-${item.type}`}>
          <input type="checkbox" checked={item.enabled} aria-label={`是否生成 ${item.title || '未命名'}`}
            onChange={event => patch(item.id, { enabled: event.target.checked })} />
          <span className="sb-type">{labelOf(item.type)}</span>
          <div className="sb-body">
            <input className="sb-title" value={item.title} maxLength={80} placeholder="名称"
              aria-label="素材名称" onChange={event => patch(item.id, { title: event.target.value })} />
            <textarea rows={2} value={item.prompt} maxLength={1200} placeholder="生成时用的描述：外观、光线、材质，越具体越稳"
              aria-label="素材描述" onChange={event => patch(item.id, { prompt: event.target.value })} />
            {item.type === 'voice' && <input className="sb-voice" value={item.voice || ''} maxLength={600}
              placeholder="可选的声线补充：年龄感、音色、语速、口音"
              aria-label="声线补充" onChange={event => patch(item.id, { voice: event.target.value })} />}
            {item.note && <small className="sb-note">{item.note}</small>}
          </div>
          <button type="button" className="sb-del" aria-label={`删除 ${item.title || '未命名'}`} onClick={() => remove(item.id)}>
            <Trash2 size={13} />
          </button>
        </article>)}
        {!visible.length && <p className="sb-hint">这一类还没有条目。</p>}
      </div>
    </>}

    <footer className="sb-foot">
      <button type="button" onClick={add} disabled={busy}><Plus size={13} /> 新增一项</button>
      <span className="sb-count">{confirmed && !dirty ? <><Check size={12} /> 已确认 · {enabledCount} 项</> : `${enabledCount} 项待确认`}</span>
      <span style={{ flex: 1 }} />
      <button type="button" onClick={() => void save(false)} disabled={saving || !dirty}><Save size={13} /> 保存清单</button>
      <button type="button" className="sf-primary" onClick={() => void save(true)} disabled={saving || !items.length}>
        {saving ? '保存中…' : confirmed ? '更新确认' : '确认清单'}
      </button>
    </footer>
    {notice && <p className="sb-notice" role="status" aria-live="polite">{notice}</p>}
  </section>;
}
