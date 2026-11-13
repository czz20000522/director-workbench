import { useEffect, useMemo, useState } from 'react';
import { AudioLines, BookOpen, Headphones, LoaderCircle, Music, Search, Trash2, Volume2 } from 'lucide-react';
import { ApiError, request } from './api/client';
import LibraryExpand from './LibraryExpand';
import type { LibrarySection } from './LibraryExpand';
import { forgetSample, listSamples, onPresetLibraryChanged, onSamplesChanged, sampleFor, sampleSrc, stageSample } from './timbreSamples';
import type { TimbreSample } from './timbreSamples';

// 起点预设卡片：内置的那几张不能删，你自己存的可以删。
// 「带入」永远是一次显式点击，不会替任何面板填参数。
type Card = { name: string; params: Record<string, unknown>; builtin: boolean; modified: boolean };
export type PresetKind = 'timbre' | 'music';

const meta: Record<PresetKind, { title: string; note: string; icon: typeof Music }> = {
  timbre: { title: '音色库', note: '音色起点 · 有小样可直接试听，点展开看全部', icon: AudioLines },
  music: { title: '配乐库', note: '风格标签与节奏起点 · 点展开看全部', icon: Music },
};

// 音色卡片标出是哪台 TTS 做的：生成引擎和「带入后用哪台」是两件事。
const engineNames: Record<string, string> = { 'voxcpm-2': 'VoxCPM2', 'indextts-2.5': 'IndexTTS 2.5' };
function generatedBy(kind: PresetKind, params: Record<string, unknown>): string {
  if (kind !== 'timbre') return '';
  const engine = engineNames[String(params.generated_by || params.engine || '')];
  return engine ? `由 ${engine} 生成` : '';
}

function summary(kind: PresetKind, params: Record<string, unknown>): string {
  if (kind === 'timbre') return String(params.voice_description || `参考音频：${String(params.voice_reference || '未选')}`);
  return [params.tags, `${params.bpm ?? '—'} BPM`, params.keyscale, `${params.duration_seconds ?? '—'} 秒`]
    .filter(Boolean).join(' · ');
}

/** 后端卡片将来带 sample_path / sample_url 就优先用；否则退回浏览器里记的小样索引。 */
function cardSample(kind: PresetKind, card: Card): TimbreSample | null {
  if (kind !== 'timbre') return null;
  const params = card.params;
  const nested = typeof params.sample === 'object' && params.sample !== null
    ? (params.sample as Record<string, unknown>).path : params.sample;
  const raw = params.sample_path ?? params.sample_url ?? nested;
  if (typeof raw === 'string' && raw.trim()) {
    return { path: raw.trim(), description: String(params.voice_description || ''), name: card.name,
      duration_seconds: Number(params.sample_duration_seconds) || undefined, created_at: 0, source: 'library' };
  }
  return sampleFor(String(params.voice_description || ''), card.name);
}

const durationLabel = (sample: TimbreSample) =>
  sample.duration_seconds ? `${sample.duration_seconds.toFixed(2)} 秒` : '时长未知';

export default function PresetLibraryPanel({ kind, onUse }: {
  kind: PresetKind; onUse: (params: Record<string, unknown>, name: string) => void;
}) {
  const [cards, setCards] = useState<Card[] | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState('');
  const [renaming, setRenaming] = useState('');
  const [draftName, setDraftName] = useState('');
  const [query, setQuery] = useState('');
  const [samples, setSamples] = useState<TimbreSample[]>(() => listSamples());
  const [tick, setTick] = useState(0);
  const info = meta[kind];

  useEffect(() => {
    let active = true;
    request<{ library: Record<PresetKind, Card[]> }>('/api/preset-library')
      .then(data => { if (active) { setCards(data.library[kind] || []); setError(''); } })
      .catch(reason => { if (active) setError(reason instanceof ApiError ? reason.message : `无法读取${info.title}`); });
    return () => { active = false; };
  }, [kind, tick]);
  useEffect(() => onSamplesChanged(() => setSamples(listSamples())), []);
  useEffect(() => onPresetLibraryChanged(() => setTick(value => value + 1)), []);

  async function rename(card: Card) {
    const next = draftName.trim();
    if (!next) { setError('请先填写新名字'); return; }
    if (next === card.name) { setRenaming(''); return; }
    setBusy(card.name); setError('');
    try {
      await request(`/api/preset-library/${kind}/${encodeURIComponent(card.name)}/rename`, { json: { name: next } });
      setRenaming(''); setTick(value => value + 1);
    } catch (reason) { setError(reason instanceof ApiError ? reason.message : '重命名未完成'); }
    finally { setBusy(''); }
  }
  async function remove(card: Card) {
    setBusy(card.name); setError('');
    try {
      await request(`/api/preset-library/${kind}/${encodeURIComponent(card.name)}`, { init: { method: 'DELETE' } });
      setTick(value => value + 1);
    } catch (reason) { setError(reason instanceof ApiError ? reason.message : '删除未完成'); }
    finally { setBusy(''); }
  }
  /** 带入时把可试听的小样一起带走：目标面板能直接播，不必重新生成。 */
  function use(card: Card) {
    const sample = cardSample(kind, card);
    // 首页「带入」目前只透传描述字符串，小样单独留一份让音色设计面板按描述认领。
    if (kind === 'timbre' && sample) stageSample(sample);
    onUse({ ...card.params,
      ...(sample ? { sample_path: sample.path, sample_duration_seconds: sample.duration_seconds } : {}) }, card.name);
  }
  const shown = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return cards || [];
    return (cards || []).filter(card =>
      `${card.name} ${summary(kind, card.params)}`.toLowerCase().includes(needle));
  }, [cards, query, kind]);

  const renameControl = (card: Card) => renaming === card.name
    ? <span className="lex-rename">
        <input value={draftName} maxLength={40} autoFocus disabled={busy === card.name}
          aria-label={`把 ${card.name} 改名为`}
          onChange={event => setDraftName(event.target.value)}
          onKeyDown={event => { if (event.key === 'Enter') void rename(card); if (event.key === 'Escape') setRenaming(''); }} />
        <button type="button" disabled={busy === card.name} onClick={() => void rename(card)}>保存</button>
        <button type="button" onClick={() => setRenaming('')}>取消</button>
      </span>
    : <button type="button" disabled={busy === card.name} aria-label={`重命名 ${card.name}`}
        onClick={() => { setRenaming(card.name); setDraftName(card.name); }}>重命名</button>;

  const cardActions = (card: Card) => <>
    <button type="button" className="lex-primary" onClick={() => use(card)}>
      {kind === 'timbre' ? '带入音色设计' : '带入配乐'}</button>
    {!card.builtin && renameControl(card)}
    {!card.builtin && <button type="button" disabled={busy === card.name} aria-label={`删除 ${card.name}`} onClick={() => void remove(card)}>
      <Trash2 size={12} /> 删除</button>}
  </>;

  const compact = <div className="pl-list" role="list">
    {!cards ? <p className="pl-empty" role="status"><LoaderCircle size={14} className="sf-spin" /> 正在读取{info.title}…</p>
      : cards.length === 0 ? <p className="pl-empty">还没有卡片。</p>
        : cards.map(card => {
          const sample = cardSample(kind, card);
          return <div className="pl-card" role="listitem" key={`${card.name}-${card.builtin}`}>
            <strong>{card.name}{card.modified ? '（已改）' : ''}</strong>
            <span>{summary(kind, card.params)}</span>
            {sample && <audio className="pl-mini-audio" controls preload="none" src={sampleSrc(sample.path)} aria-label={`试听 ${card.name}`} />}
            <div className="pl-card-actions">
              <small>{card.builtin ? '内置起点' : '我的卡片'}{generatedBy(kind, card.params) ? ` · ${generatedBy(kind, card.params)}` : ''}</small>
              <button className="sf-ghost" type="button" onClick={() => use(card)}>带入</button>
              {!card.builtin && (renaming === card.name
                ? <span className="pl-rename">
                    <input value={draftName} maxLength={40} autoFocus disabled={busy === card.name}
                      aria-label={`把 ${card.name} 改名为`}
                      onChange={event => setDraftName(event.target.value)}
                      onKeyDown={event => { if (event.key === 'Enter') void rename(card); if (event.key === 'Escape') setRenaming(''); }} />
                    <button className="sf-ghost" type="button" disabled={busy === card.name} onClick={() => void rename(card)}>保存</button>
                    <button className="sf-ghost" type="button" onClick={() => setRenaming('')}>取消</button>
                  </span>
                : <button className="sf-ghost" type="button" disabled={busy === card.name} aria-label={`重命名 ${card.name}`}
                    onClick={() => { setRenaming(card.name); setDraftName(card.name); }}>重命名</button>)}
              {!card.builtin && <button className="sf-ghost" type="button" disabled={busy === card.name} aria-label={`删除 ${card.name}`} onClick={() => void remove(card)}>
                <Trash2 size={13} /> 删除</button>}
            </div>
          </div>;
        })}
    {error && <p className="pl-error" role="alert">{error}</p>}
  </div>;

  const cardsSection: LibrarySection = {
    id: 'cards', label: '全部卡片', icon: <BookOpen size={14} />,
    note: `${info.title} · ${cards?.length ?? 0} 张卡片 · 点右上角可收起回侧栏`,
    content: <>
      <div className="lex-toolbar">
        <h3>全部卡片</h3><span>{cards ? `${cards.length} 张` : '读取中…'}</span>
        <label className="lex-search"><Search size={13} />
          <input value={query} maxLength={40} onChange={event => setQuery(event.target.value)}
            placeholder={kind === 'timbre' ? '搜卡片名或音色描述' : '搜卡片名或风格标签'} aria-label="搜索卡片" />
        </label>
      </div>
      {!cards ? <p className="lex-empty"><LoaderCircle size={14} className="sf-spin" /> 正在读取{info.title}…</p>
        : shown.length === 0 ? <p className="lex-empty">{query ? '没有匹配的卡片。' : '还没有卡片，在音色设计或配乐面板里存一张。'}</p>
          : <div className="lex-grid">{shown.map(card => {
              const sample = cardSample(kind, card);
              return <article className="lex-card" key={`${card.name}-${card.builtin}`}>
                <div className="lex-card-top"><strong>{card.name}{card.modified ? '（已改）' : ''}</strong>
                  <span className="lex-tag">{card.builtin ? '内置起点' : '我的卡片'}</span></div>
                <p className="lex-card-desc">{summary(kind, card.params)}</p>
                <p className="lex-card-meta">
                  {kind === 'timbre'
                    ? [engineNames[String(card.params.engine || 'voxcpm-2')] || 'VoxCPM2', generatedBy(kind, card.params),
                        card.params.text ? `试听台词：${String(card.params.text).slice(0, 40)}` : ''].filter(Boolean).join(' · ')
                    : [String(card.params.keyscale || '—'), String(card.params.timesignature || '4') + ' 拍',
                        String(card.params.language || '—')].join(' · ')}
                </p>
                {sample && <div className="lex-sample">
                  <audio controls preload="none" src={sampleSrc(sample.path)} aria-label={`试听 ${card.name} 的小样`} />
                  <small>小样 · {durationLabel(sample)} · 不用重新生成，直接点播放</small>
                </div>}
                <div className="lex-card-actions">{cardActions(card)}</div>
              </article>;
            })}</div>}
      {error && <p className="lex-error" role="alert">{error}</p>}
    </>,
  };

  const samplesSection: LibrarySection = {
    id: 'samples', label: '试听小样', icon: <Headphones size={14} />,
    note: '生成过的音色样本 · 按音色描述自动挂到对应卡片',
    content: <>
      <div className="lex-toolbar"><h3>试听小样</h3><span>{samples.length} 段</span></div>
      {samples.length === 0
        ? <p className="lex-empty">还没有小样。去「音色设计」生成一段，或点开单步任务结果试听；生成成功后会按音色描述自动挂到这里和对应卡片上。</p>
        : <div className="lex-rows">{samples.map(sample => <div className="lex-row" key={sample.path}>
            <div className="lex-row-head"><strong>{sample.description || '未命名音色'}</strong>
              <span>{new Date(sample.created_at).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })} · {durationLabel(sample)}</span></div>
            <audio controls preload="none" src={sampleSrc(sample.path)} aria-label={`试听 ${sample.description}`} />
            <div className="lex-row-actions">
              <button type="button" className="lex-primary" onClick={() => {
                stageSample(sample);
                onUse({ engine: 'voxcpm-2', voice_description: sample.description,
                  sample_path: sample.path, sample_duration_seconds: sample.duration_seconds },
                  sample.name || sample.description.slice(0, 20));
              }}>带入音色设计</button>
              <button type="button" onClick={() => forgetSample(sample.path)}><Trash2 size={12} /> 移除这条小样</button>
            </div>
          </div>)}</div>}
      <p className="lex-error" style={{ color: 'var(--sf-muted)' }}>移除只清掉这条关联，音频文件仍留在作品素材里。要彻底删除文件，请走素材区的删除流程。</p>
    </>,
  };

  const guideSection: LibrarySection = {
    id: 'guide', label: '怎么用', icon: <Volume2 size={14} />,
    note: '带入、试听与存卡片的关系',
    content: <div className="lex-note">
      <h3>{info.title}和生成面板是怎么联动的</h3>
      {kind === 'timbre' ? <>
        <p>卡片存的是<strong>起点参数</strong>：引擎、音色描述（或参考音频）、试听台词。随机编号不进卡片，每次生成由你自己决定，所以同一张卡可以反复试出不同声音。</p>
        <ul>
          <li>点「带入」把参数送进音色设计或锚点配音，<strong>不会自动开始生成</strong>，你还可以改。</li>
          <li>生成成功后，小样会按<strong>音色描述</strong>自动挂回这张卡片：描述没改 → 覆盖同一条小样；描述改了 → 记成另一条音色，原卡不变。</li>
          <li>挂上小样的卡片在这里和侧栏都能<strong>直接播放</strong>，不用再点一次生成；带入时小样也会跟着一起带过去。</li>
          <li>在「音色设计」面板填卡片名点「存入音色库」，就能把当前设计留成一张新卡。</li>
        </ul>
        <p>小样关联目前记在本浏览器里（<code>localStorage</code>），换浏览器或清缓存后会重新变回「未试听」状态，卡片参数本身不受影响。</p>
      </> : <>
        <p>卡片存的是<strong>一套起点参数</strong>：风格标签、BPM、调性、拍号、时长。它不是一个音频文件，也不会自动铺到任何音轨上。</p>
        <ul>
          <li>点「带入」把参数送进配乐面板，生成前你还可以改时长、改随机编号。</li>
          <li>随机编号刻意不进卡片：那是一次生成的决定，留着才能一张卡试出不同版本。</li>
          <li>在「配乐」面板填卡片名点「存入配乐库」，就能把当前这套参数留成一张新卡。</li>
        </ul>
      </>}
      <p>内置卡片不能删；你自己存的卡片可以重命名、删除。所有改动都要你显式点一次，不会自动写回任何作品。</p>
    </div>,
  };

  const sections = kind === 'timbre'
    ? [cardsSection, samplesSection, guideSection]
    : [cardsSection, guideSection];

  return <LibraryExpand title={info.title} subtitle={info.note}
    icon={<info.icon size={16} />} compact={compact} sections={sections} />;
}
