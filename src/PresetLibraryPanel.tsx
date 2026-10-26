import { useEffect, useState } from 'react';
import { AudioLines, LoaderCircle, Music, Trash2 } from 'lucide-react';
import { ApiError, request } from './api/client';

// 起点预设卡片：内置的那几张不能删，你自己存的可以删。
// 「带入」永远是一次显式点击，不会替任何面板填参数。
type Card = { name: string; params: Record<string, unknown>; builtin: boolean; modified: boolean };
export type PresetKind = 'timbre' | 'music';

const meta: Record<PresetKind, { title: string; note: string; icon: typeof Music }> = {
  timbre: { title: '音色库', note: '一句话的音色起点 · 带入后仍要你点生成', icon: AudioLines },
  music: { title: '配乐库', note: '风格标签与节奏起点 · 带入后仍要你点生成', icon: Music },
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

export default function PresetLibraryPanel({ kind, onUse }: {
  kind: PresetKind; onUse: (params: Record<string, unknown>, name: string) => void;
}) {
  const [cards, setCards] = useState<Card[] | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState('');
  const [tick, setTick] = useState(0);
  const info = meta[kind];
  useEffect(() => {
    let active = true;
    request<{ library: Record<PresetKind, Card[]> }>('/api/preset-library')
      .then(data => { if (active) { setCards(data.library[kind] || []); setError(''); } })
      .catch(reason => { if (active) setError(reason instanceof ApiError ? reason.message : `无法读取${info.title}`); });
    return () => { active = false; };
  }, [kind, tick]);
  async function remove(card: Card) {
    setBusy(card.name); setError('');
    try {
      await request(`/api/preset-library/${kind}/${encodeURIComponent(card.name)}`, { init: { method: 'DELETE' } });
      setTick(value => value + 1);
    } catch (reason) { setError(reason instanceof ApiError ? reason.message : '删除未完成'); }
    finally { setBusy(''); }
  }
  return <section className="pl-panel" aria-label={info.title}>
    <header className="pl-head"><div><h2>{info.title}</h2><p>{info.note}</p></div><span className="pl-icon"><info.icon size={16} /></span></header>
    <div className="pl-list" role="list">
      {!cards ? <p className="pl-empty" role="status"><LoaderCircle size={14} className="sf-spin" /> 正在读取{info.title}…</p>
        : cards.length === 0 ? <p className="pl-empty">还没有卡片。</p>
          : cards.map(card => <div className="pl-card" role="listitem" key={`${card.name}-${card.builtin}`}>
            <strong>{card.name}{card.modified ? '（已改）' : ''}</strong>
            <span>{summary(kind, card.params)}</span>
            <div className="pl-card-actions">
              <small>{card.builtin ? '内置起点' : '我的卡片'}{generatedBy(kind, card.params) ? ` · ${generatedBy(kind, card.params)}` : ''}</small>
              <button className="sf-ghost" type="button" onClick={() => onUse(card.params, card.name)}>带入</button>
              {!card.builtin && <button className="sf-ghost" type="button" disabled={busy === card.name} aria-label={`删除 ${card.name}`} onClick={() => void remove(card)}>
                <Trash2 size={13} /> 删除</button>}
            </div>
          </div>)}
    </div>
    {error && <p className="pl-error" role="alert">{error}</p>}
  </section>;
}
