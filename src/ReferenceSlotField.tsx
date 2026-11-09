import { useEffect, useMemo, useState } from 'react';
import { ImageIcon, LoaderCircle, PenLine, X } from 'lucide-react';
import { request } from './api/client';

export type ReferenceRole = '底图' | '脸型' | '造型' | '服装' | '姿势';
/** 一个槽要么是一张库里的图（material_id），要么是一句你自己写的描述（custom）。 */
export type SlotValue = { role: ReferenceRole; material_id: string; custom?: string; title: string; thumb: string };
type LibraryMaterial = { id: string; title: string; category: string; thumb_url: string };

// 参考库索引一次就够（7104 张）：面板里反复开关不该重复拉两兆 JSON。
let materialCache: LibraryMaterial[] | null = null;
let inflight: Promise<LibraryMaterial[]> | null = null;

function loadMaterials(): Promise<LibraryMaterial[]> {
  if (materialCache) return Promise.resolve(materialCache);
  if (!inflight) {
    inflight = request<{ materials: LibraryMaterial[] }>('/api/reference-library')
      .then(data => {
        materialCache = data.materials || [];
        return materialCache;
      })
      .finally(() => { inflight = null; });
  }
  return inflight;
}

/** 刚存进素材库的首帧要能被下一次打开的选择器看到。 */
export function invalidateMaterialIndex() { materialCache = null; }

/** 按 id 取回素材的标题与缩略图：从提示词库回填配方时要用它还原槽位。 */
export async function lookupMaterial(materialId: string): Promise<{ id: string; title: string; thumb: string } | null> {
  if (!materialId) return null;
  try {
    const materials = await loadMaterials();
    const found = materials.find(item => item.id === materialId);
    return found ? { id: found.id, title: found.title, thumb: found.thumb_url } : null;
  } catch { return null; }
}

const PAGE = 60;

/** 分类标签的顺序：常用子库排在前面，其余按名字排。 */
const CATEGORY_ORDER = ['首帧图参考', '脸型·发型', '服饰·写实', '服饰·动画', '姿势·线稿', '姿势·深度'];
/** 服饰库里内衣/泳装/性感排在最后：第一屏应当是常服，不是内衣。 */
const TAIL_PREFIXES = ['内衣', '泳装', '性感'];
const tailRank = (title: string) => (TAIL_PREFIXES.some(prefix => title.startsWith(prefix)) ? 1 : 0);

/** 每个槽默认落在哪个子库、只看哪批标题。
 *
 * 以前四个槽都从「全部」开始，脸型槽里先看到的是 1400 张姿势图和 1400 张服装，
 * 用户得自己翻。库里其实早就分好了：`正面脸图·` 是长相，`造型参考·` 是发型，
 * 所以这里直接把默认范围和关键词带进去，要换范围点「全部」就行。
 */
const ROLE_SCOPE: Partial<Record<ReferenceRole, { category: string; prefix?: string }>> = {
  '脸型': { category: '脸型·发型', prefix: '正面脸图' },
  '造型': { category: '脸型·发型', prefix: '造型参考' },
  '服装': { category: '服饰·写实' },
  '姿势': { category: '姿势·线稿' },
};

/** 不用图也能定下来的几件事。点一下就是选中，比逼人选一张不合适的图更准。 */
const ROLE_PRESETS: Partial<Record<ReferenceRole, string[]>> = {
  '服装': ['不穿衣服', '内衣', '泳装', '常服', '正装', '睡袍'],
  '造型': ['短发', '长直发', '大波浪', '丸子头', '马尾', '寸头'],
  '姿势': ['站姿', '坐姿', '躺姿', '跪姿'],
};

export default function ReferenceSlotField({ role, hint, disabled, value, onChange }: {
  role: ReferenceRole; hint: string; disabled?: boolean;
  value?: SlotValue; onChange: (next: SlotValue | undefined) => void;
}) {
  const scope = ROLE_SCOPE[role];
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState<LibraryMaterial[] | null>(materialCache);
  const [error, setError] = useState('');
  const [category, setCategory] = useState(scope?.category || '全部');
  const [prefix, setPrefix] = useState(scope?.prefix || '');
  const [query, setQuery] = useState('');
  const [draft, setDraft] = useState('');
  const [limit, setLimit] = useState(PAGE);

  useEffect(() => {
    if (!open || items) return;
    let alive = true;
    void loadMaterials()
      .then(list => { if (alive) setItems(list); })
      .catch(() => { if (alive) setError('参考库读取失败'); });
    return () => { alive = false; };
  }, [open, items]);

  const categories = useMemo(() => {
    const seen = Array.from(new Set((items || []).map(item => item.category).filter(Boolean)));
    // 这个槽的默认子库紧跟在「全部」后面，其余按固定顺序、再按名字。
    const preferred = scope?.category && seen.includes(scope.category) ? [scope.category] : [];
    const known = CATEGORY_ORDER.filter(name => seen.includes(name) && name !== scope?.category);
    const rest = seen.filter(name => !CATEGORY_ORDER.includes(name)).sort((a, b) => a.localeCompare(b, 'zh'));
    return ['全部', ...preferred, ...known, ...rest];
  }, [items, scope?.category]);

  const matched = useMemo(() => {
    const keyword = query.trim();
    return (items || []).filter(item =>
      (category === '全部' || item.category === category) &&
      (!prefix || item.title.includes(prefix)) &&
      (!keyword || item.title.includes(keyword) || item.id.includes(keyword)),
    ).sort((a, b) => tailRank(a.title) - tailRank(b.title));
  }, [items, category, prefix, query]);

  const pick = (item: LibraryMaterial) => {
    onChange({ role, material_id: item.id, title: item.title, thumb: item.thumb_url });
    setOpen(false);
  };
  const applyText = (text: string) => {
    const trimmed = text.trim();
    if (!trimmed) return;
    onChange({ role, material_id: '', custom: trimmed, title: trimmed, thumb: '' });
    setDraft('');
    setOpen(false);
  };

  const presets = ROLE_PRESETS[role] || [];

  return <div className="sf-slot">
    <div className="sf-slot-head">
      <span className="sf-slot-role">{role}</span>
      {value
        ? <button type="button" className="sf-slot-chip" disabled={disabled} onClick={() => setOpen(!open)} title="换一个">
            {value.thumb
              ? <img src={value.thumb} alt="" loading="lazy" />
              : <span className="sf-slot-chip-text"><PenLine size={12} /></span>}
            <span>{value.custom ? `${role}：${value.custom}` : value.title}</span>
          </button>
        : <button type="button" className="sf-slot-empty" disabled={disabled} onClick={() => setOpen(!open)}>
            <ImageIcon size={13} /> 从素材库选
          </button>}
      {value && <button type="button" className="sf-slot-clear" disabled={disabled} aria-label="清除这一槽" onClick={() => onChange(undefined)}><X size={12} /></button>}
    </div>
    {!value && <p className="sf-slot-hint">{hint}</p>}
    {open && <div className="sf-slot-picker">
      <div className="sf-slot-bar">
        <div className="sf-slot-cats">
          {categories.map(name => <button key={name} type="button" className={category === name ? 'selected' : ''} onClick={() => { setCategory(name); setLimit(PAGE); }}>{name}</button>)}
        </div>
        <input value={query} maxLength={40} placeholder="按标题筛选，如 中长发 / 蕾丝 / 站姿" onChange={event => { setQuery(event.target.value); setLimit(PAGE); }} />
      </div>
      {prefix && <div className="sf-slot-scope-row">
        <span className="sf-slot-scope">只看「{prefix}」</span>
        <button type="button" className="sf-inline-link" onClick={() => setPrefix('')}>看全部标题</button>
      </div>}
      {presets.length > 0 && <div className="sf-slot-presets">
        <span className="sf-slot-presets-label">不选图，直接定：</span>
        {presets.map(text => <button key={text} type="button" onClick={() => applyText(text)}>{text}</button>)}
        <span className="sf-slot-custom">
          <input value={draft} maxLength={40} placeholder="自己写，如 米色高领毛衣"
            onChange={event => setDraft(event.target.value)}
            onKeyDown={event => { if (event.key === 'Enter') { event.preventDefault(); applyText(draft); } }} />
          <button type="button" disabled={!draft.trim()} onClick={() => applyText(draft)}>用这句</button>
        </span>
      </div>}
      {error && <p className="sf-slot-hint">{error}</p>}
      {!items && !error && <p className="sf-slot-hint"><LoaderCircle size={12} className="sf-spin" /> 正在读取参考图库…</p>}
      {items && !matched.length && <p className="sf-slot-hint">这个筛选下没有素材。点「全部」或换个词，也可以在下面直接写一句。</p>}
      <div className="sf-slot-grid">
        {matched.slice(0, limit).map(item => <button key={item.id} type="button" onClick={() => pick(item)} title={item.title}>
          <img src={item.thumb_url} alt={item.title} loading="lazy" />
          <span>{item.title}</span>
        </button>)}
      </div>
      {matched.length > limit && <button type="button" className="sf-inline-link" onClick={() => setLimit(limit + PAGE)}>再显示 {PAGE} 张（共 {matched.length} 张）</button>}
    </div>}
  </div>;
}
