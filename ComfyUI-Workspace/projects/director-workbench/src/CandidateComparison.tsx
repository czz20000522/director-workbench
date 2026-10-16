import { useEffect, useId, useState } from 'react';
import './candidate-comparison.css';

type Candidate = {
  id: string;
  label: string;
  mediaUrl: string;
  prompt: string;
  generatedAt: string;
  output: string;
  current: boolean;
  incomplete: boolean;
};

export default function CandidateComparison({ candidates, busy, hasMore, loading, error, onLoadMore, onRestore }: {
  candidates: Candidate[];
  busy: boolean;
  hasMore: boolean;
  loading: boolean;
  error: string;
  onLoadMore: () => void;
  onRestore: (id: string) => void;
}) {
  const panelId = useId();
  const [open, setOpen] = useState(false);
  const newest = candidates[0]?.id ?? '';
  const previous = candidates[1]?.id ?? '';
  const [pair, setPair] = useState({ left: previous, right: newest });
  const [failed, setFailed] = useState<string[]>([]);
  useEffect(() => { setPair({ left: previous, right: newest }); }, [newest, previous]);
  if (candidates.length < 2) return null;
  const right = candidates.find(candidate => candidate.id === pair.right) ?? candidates[0];
  const left = candidates.find(candidate => candidate.id === pair.left && candidate.id !== right.id)
    ?? candidates.find(candidate => candidate.id !== right.id)!;

  function card(side: 'left' | 'right', candidate: Candidate, other: Candidate) {
    const title = side === 'left' ? '左侧' : '右侧';
    return <article className="candidate-comparison-card" aria-label={`${title}候选`}>
      <label>{title}候选<select aria-label={`${title}比较版本`} value={candidate.id} disabled={busy} onChange={event => {
        const id = event.target.value;
        setPair(current => id === current[side === 'left' ? 'right' : 'left'] ? current : { ...current, [side]: id });
      }}>
        {candidates.map(option => <option key={option.id} value={option.id} disabled={option.id === other.id}>{option.label}{option.current ? ' · 当前候选' : ''}</option>)}
      </select></label>
      <video key={candidate.id} src={candidate.mediaUrl || undefined} controls muted playsInline preload="metadata" aria-label={`${title}${candidate.label}视频预览`}
        onError={() => setFailed(ids => ids.includes(candidate.id) ? ids : [...ids, candidate.id])}
        onLoadedData={() => setFailed(ids => ids.includes(candidate.id) ? ids.filter(id => id !== candidate.id) : ids)} />
      {(!candidate.mediaUrl || failed.includes(candidate.id)) && <p role="alert">此候选暂时无法播放，请在历史记录中核对原任务和文件。</p>}
      <div className="candidate-comparison-facts"><strong>{candidate.label}</strong>{candidate.current && <span>当前候选</span>}<small>生成于 {candidate.generatedAt}</small><small>{candidate.output}</small>{candidate.incomplete && <small>旧任务输入信息不完整</small>}</div>
      <details><summary>查看完整提示词</summary><pre aria-label={`${title}候选提示词`}>{candidate.prompt}</pre></details>
      <button type="button" className="button secondary compact" disabled={busy || candidate.current} onClick={() => onRestore(candidate.id)}>{candidate.current ? '已是当前候选' : busy ? '恢复中…' : '恢复为当前候选'}</button>
    </article>;
  }

  return <section className="candidate-comparison" aria-label="候选并排比较">
    <div className="candidate-comparison-heading"><div><strong>看看哪一版更合适</strong><p>比较仅预览。需要使用旧版本时，恢复为当前后再确认采用。</p></div><button type="button" className="button secondary compact" aria-expanded={open} aria-controls={panelId} onClick={() => setOpen(value => !value)}>{open ? '收起候选比较' : '并排比较候选'}</button></div>
    {open && <div id={panelId}>
      <p className="candidate-comparison-help">默认比较最近两次生成；左右视频独立播放，默认静音，可分别打开声音。</p>
      <div className="candidate-comparison-grid">{card('left', left, right)}{card('right', right, left)}</div>
      {error && <p role="alert">{error}</p>}
      {hasMore && <button type="button" className="button secondary compact candidate-comparison-more" disabled={loading} onClick={onLoadMore}>{loading ? '正在读取历史…' : '加载更早的候选'}</button>}
    </div>}
  </section>;
}
