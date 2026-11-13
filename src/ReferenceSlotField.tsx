import { useEffect, useMemo, useRef, useState } from 'react';
import { ImageIcon, LoaderCircle, PenLine, Plus, X } from 'lucide-react';
import { request } from './api/client';

export type ReferenceRole = '底图' | '脸型' | '造型' | '服装' | '姿势' | '自有素材';
/** 一个槽要么是一张库里的图（material_id），要么是一句你自己写的描述（custom）。 */
export type SlotValue = { role: ReferenceRole; material_id: string; custom?: string; title: string; thumb: string; people?: number };
type LibraryMaterial = { id: string; title: string; category: string; thumb_url: string; people?: number };
/** 姿势库里 pair60_* 这批是双人图（275 张）。不另建目录，用虚拟分类把它们聚在一起，
 *  选的时候就不会误以为是一张单人姿势。 */
const MULTI_CATEGORY = '双人';

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

/** 每个槽只列自己那几类子库。
 *
 * 以前四个槽都能点到「场景」「道具」「姿势·深度」，点了多半是空的：脸型槽里
 * 既没有场景也没有姿势。与其让人一层层试错，不如按槽收口；真要用一张完整
 * 的图（或者别人的成片、自己的首帧），走「自有素材」那个槽——它不限分类。
 */
const ROLE_CATEGORIES: Record<ReferenceRole, string[] | null> = {
  '底图': null,
  '脸型': ['脸型·发型'],
  '造型': ['脸型·发型'],
  '服装': ['服饰·写实', '服饰·动画'],
  '姿势': ['姿势·线稿', '姿势·深度'],
  '自有素材': null,
};

/** 不用图也能定下来的几件事。点一下就是选中，比逼人选一张不合适的图更准。 */
const ROLE_PRESETS: Partial<Record<ReferenceRole, string[]>> = {
  '服装': ['不穿衣服', '内衣', '泳装', '常服', '正装', '睡袍'],
  '造型': ['短发', '长直发', '大波浪', '丸子头', '马尾', '寸头'],
  '姿势': ['站姿', '坐姿', '躺姿', '跪姿'],
};

export default function ReferenceSlotField({ role, hint, disabled, projectId, value, onChange }: {
  role: ReferenceRole; hint: string; disabled?: boolean;
  /** 只有「自有素材」用得上：本地上传要先登记进当前单步项目。 */
  projectId?: string;
  value?: SlotValue; onChange: (next: SlotValue | undefined) => void;
}) {
  const scope = ROLE_SCOPE[role];
  const allowed = ROLE_CATEGORIES[role];
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState<LibraryMaterial[] | null>(materialCache);
  const [error, setError] = useState('');
  const [category, setCategory] = useState(scope?.category || allowed?.[0] || '全部');
  const [prefix, setPrefix] = useState(scope?.prefix || '');
  const [query, setQuery] = useState('');
  const [draft, setDraft] = useState('');
  const [limit, setLimit] = useState(PAGE);
  const [uploading, setUploading] = useState(false);
  const fileInput = useRef<HTMLInputElement | null>(null);

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
    const ordered = [
      ...CATEGORY_ORDER.filter(name => seen.includes(name)),
      ...seen.filter(name => !CATEGORY_ORDER.includes(name)).sort((a, b) => a.localeCompare(b, 'zh')),
    ];
    // 姿势库里有 275 张双人图，混在单人里很容易选错：给姿势槽加一个虚拟分类。
    const multiCount = (items || []).filter(item => (item.people || 1) >= 2).length;
    const poseScope = allowed ? allowed.some(name => name.startsWith('姿势')) : false;
    const multiTab = poseScope && multiCount ? [MULTI_CATEGORY] : [];
    // 不限分类（自有素材 / 底图）：全部子库都摆出来。
    if (!allowed) return ['全部', ...ordered];
    // 只列这个槽真能用的子库；库里暂时没有就退回「全部」，不要留一排空标签。
    const scoped = allowed.filter(name => seen.includes(name));
    return scoped.length ? [...multiTab, ...scoped] : ['全部'];
  }, [items, allowed]);

  const matched = useMemo(() => {
    const keyword = query.trim();
    const inCategory = (item: LibraryMaterial) => category === '全部'
      ? true
      : category === MULTI_CATEGORY
        ? (item.people || 1) >= 2
        : item.category === category;
    return (items || []).filter(item =>
      inCategory(item) &&
      (!prefix || item.title.includes(prefix)) &&
      (!keyword || item.title.includes(keyword) || item.id.includes(keyword)),
    ).sort((a, b) => tailRank(a.title) - tailRank(b.title));
  }, [items, category, prefix, query]);

  const pick = (item: LibraryMaterial) => {
    onChange({ role, material_id: item.id, title: item.title, thumb: item.thumb_url, people: item.people });
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
  const isOwn = role === '自有素材';

  /** 本地上传：先登记进当前单步项目，再存一份到素材库「首帧图参考」，
   *  这样这一次能直接用，下一次还能在库里选回来。 */
  const uploadOwn = async (file: File) => {
    if (!projectId) { setError('单步项目还没准备好，稍后再试'); return; }
    setUploading(true); setError('');
    try {
      const form = new FormData(); form.append('files', file, `assets/${file.name}`);
      const uploaded = await request<{ uploaded?: string[] }>(`/api/projects/${projectId}/upload`, {
        init: { method: 'POST', body: form },
      });
      const path = uploaded.uploaded?.[0];
      if (!path) throw new Error('上传没有返回文件路径');
      const saved = await request<{ material: { id: string; title: string; thumb_url: string } }>('/api/reference-library/first-frames', {
        json: { path, title: file.name.replace(/\.[^.]+$/, '') },
      });
      invalidateMaterialIndex();
      onChange({ role, material_id: saved.material.id, title: saved.material.title, thumb: saved.material.thumb_url });
      setOpen(false);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '上传失败');
    } finally { setUploading(false); }
  };

  return <div className={isOwn ? 'sf-slot is-own' : 'sf-slot'}>
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
            {isOwn ? <><Plus size={13} /> 添加自有素材</> : <><ImageIcon size={13} /> 从素材库选</>}
          </button>}
      {value && <button type="button" className="sf-slot-clear" disabled={disabled} aria-label="清除这一槽" onClick={() => onChange(undefined)}><X size={12} /></button>}
    </div>
    {!value && <p className="sf-slot-hint">{hint}</p>}
    {open && <div className="sf-slot-picker">
      <div className="sf-slot-bar">
        {items && (categories.length > 1
          ? <div className="sf-slot-cats">
              {categories.map(name => <button key={name} type="button" className={category === name ? 'selected' : ''} onClick={() => { setCategory(name); setLimit(PAGE); }}>{name}</button>)}
            </div>
          : <span className="sf-slot-scope">{categories[0] === '全部' ? '全部分类' : `只列「${categories[0]}」`}</span>)}
        <input value={query} maxLength={40} placeholder="按标题筛选，如 中长发 / 蕾丝 / 站姿" onChange={event => { setQuery(event.target.value); setLimit(PAGE); }} />
      </div>
      {isOwn && projectId && <div className="sf-slot-upload">
        <button type="button" disabled={disabled || uploading} onClick={() => fileInput.current?.click()}>
          {uploading ? '上传中…' : '上传本地图片'}
        </button>
        <small>上传的原图会存进素材库「首帧图参考」，下次能直接选。</small>
        <input ref={fileInput} type="file" hidden accept="image/png,image/jpeg,image/webp,.heic,.heif"
          onChange={event => { const file = event.target.files?.[0]; if (file) void uploadOwn(file); event.target.value = ''; }} />
      </div>}
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
      {items && !matched.length && <p className="sf-slot-hint">这个筛选下没有素材。换个词，也可以在上面上传本地图片。</p>}
      {items && <div className="sf-slot-grid">
        {matched.slice(0, limit).map(item => <button key={item.id} type="button" onClick={() => pick(item)}
          title={(item.people || 1) >= 2 ? `${item.title}（画面里有 ${item.people} 个人，描述要写清各自的动作）` : item.title}>
          <img src={item.thumb_url} alt={item.title} loading="lazy" />
          {(item.people || 1) >= 2 && <em className="sf-slot-people">{item.people} 人</em>}
          <span>{item.title}</span>
        </button>)}
      </div>}
      {matched.length > limit && <button type="button" className="sf-inline-link" onClick={() => setLimit(limit + PAGE)}>再显示 {PAGE} 张（共 {matched.length} 张）</button>}
    </div>}
  </div>;
}
