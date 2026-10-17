import { useEffect, useRef, useState } from 'react';

type Facts = { known?: boolean; duration_seconds?: number | null; width?: number | null; height?: number | null; frames?: number | null; fps?: number | null; audio_streams?: number | null };
type Timing = { requested_seconds?: number; expected_candidate?: { frame_count: number; playback_seconds: number; fps: number } | null; observed_candidate: Facts; assembly_take?: { start_seconds: number; duration_seconds: number; frame_count: number; playback_seconds: number; fps: number } | null };
type Source = { id: string; title: string; path?: string | null; facts: Facts; offset_seconds: number; duration_seconds?: number | null };
export type Inspection = { revision: number; segment_id: string; candidate_version?: string | null; timing: Timing;
  reference_options: { id: string; title: string }[];
  reference_comparison: { reference?: { asset_id?: string; title?: string } | null; planned: Facts;
    differences: { field: string; reference?: unknown; planned?: unknown; status: string }[];
    first_frame: { preprocessing: string; aspect_mismatch: boolean }; note: string } };
type Data = { revision: number; inspection: Inspection; comparison_sources: Source[]; change_impact: { effects: { kind: string; id: string; effect: string; note?: string }[]; no_effect: string; old_candidates_preserved: boolean };
  items: { task_id: string; parameter_differences: { field: string; current?: unknown; previous?: unknown }[] }[];
  review_records: { revision: number; data: { note?: string; status?: string; candidate_ref?: string } }[] };
const url = (path?: string | null) => path ? `/media-file?path=${encodeURIComponent(path)}` : undefined;
const value = (v: unknown) => v === null || v === undefined ? '未知' : typeof v === 'object' ? JSON.stringify(v) : String(v);
const names: Record<string, string> = { width: '宽度', height: '高度', duration_seconds: '播放时长', fps: '帧率', audio_strategy: '声音策略', prompt: '提示词', first_frame: '首帧', last_frame: '尾帧', audio_guide: '声音参考', workflow: '制作方案', seed: '随机种子' };

export function ReferenceDifferenceSummary({ inspection }: { inspection: Inspection }) {
  const labels: Record<string, string> = { has_audio: '有音轨', silent: '无音轨', segment_native: '保留各段原音', complete_master: '完整母版替换', mute: '全片静音', legacy_master: '原完整母版' };
  const display = (v: unknown) => typeof v === 'string' ? labels[v] || v : value(v);
  return <div><table><thead><tr><th>规格</th><th>参考</th><th>计划输出</th><th>差异</th></tr></thead><tbody>
    {inspection.reference_comparison.differences.map(row => <tr key={row.field}><td>{names[row.field]}</td><td>{display(row.reference)}</td><td>{display(row.planned)}</td><td>{{ same: '一致', different: '不同', unknown: '未知', review_required: '须核对策略' }[row.status] || '未知'}</td></tr>)}
  </tbody></table>
  {inspection.reference_comparison.first_frame.preprocessing === 'stretch' && <p role={inspection.reference_comparison.first_frame.aspect_mismatch ? 'status' : undefined}>当前H3节点将首帧拉伸到输出宽高{inspection.reference_comparison.first_frame.aspect_mismatch ? '；比例不同，可能产生变形，请主动核对画幅方案。' : '；选择相同比例可避免比例偏离。'}</p>}</div>;
}

export default function CreativeInspectionPanel({ projectId, segmentId, revision, onPlanChanged }: { projectId: string; segmentId: string; revision: number; onPlanChanged: () => void }) {
  const [data, setData] = useState<Data | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [left, setLeft] = useState('current');
  const [right, setRight] = useState('reference');
  const [failed, setFailed] = useState<string[]>([]);
  const [ready, setReady] = useState<string[]>([]);
  const leftRef = useRef<HTMLVideoElement>(null);
  const rightRef = useRef<HTMLVideoElement>(null);
  const identity = useRef('');
  identity.current = `${projectId}:${segmentId}`;
  const base = `/api/projects/${encodeURIComponent(projectId)}`;
  useEffect(() => {
    let cancelled = false;
    setData(null); setError(''); setLeft('current'); setRight('reference'); setFailed([]); setReady([]);
    fetch(`${base}/segments/${encodeURIComponent(segmentId)}/versions?limit=20`).then(async response => {
      const result = await response.json(); if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : '对照资料读取失败');
      if (!cancelled && result.inspection && result.comparison_sources) setData(result);
    }).catch(cause => { if (!cancelled) setError(cause.message); });
    return () => { cancelled = true; };
  }, [projectId, segmentId, revision]);

  async function chooseReference(assetId: string) {
    if (!data || busy) return;
    const owner = identity.current;
    setBusy(true); setError('');
    try {
      const response = await fetch(`${base}/plan/segments/${encodeURIComponent(segmentId)}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ expected_revision: data.revision, reference_asset_id: assetId || null }) });
      const result = await response.json(); if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : '参考绑定已变化，请刷新后重试');
      if (identity.current === owner) onPlanChanged();
    } catch (cause) { if (identity.current === owner) setError(cause instanceof Error ? cause.message : '保存失败'); }
    finally { if (identity.current === owner) setBusy(false); }
  }

  const sources = data?.comparison_sources ?? [];
  const a = sources.find(row => row.id === left), b = sources.find(row => row.id === right);
  useEffect(() => {
    setReady([...(leftRef.current && leftRef.current.readyState >= 3 ? [left] : []),
              ...(rightRef.current && rightRef.current.readyState >= 3 ? [right] : [])]);
  }, [left, right, data]);
  const playable = (source?: Source) => Boolean(source?.path && source.facts.known && source.facts.width && !failed.includes(source.id));
  const preview = (source: Source | undefined, ref: React.RefObject<HTMLVideoElement | null>) => source && playable(source) ? <video key={`${source.id}:${source.path}`} ref={ref} controls preload="metadata" src={url(source.path)}
    onCanPlay={() => setReady(previous => previous.includes(source.id) ? [...previous] : [...previous, source.id])}
    onError={() => setFailed(previous => [...previous, source.id])} /> : <p>{source && failed.includes(source.id) ? '文件不能播放，请核对或重新上传。' : '没有可读取的候选／参考视频。'}</p>;
  if (error && !data) return <p role="alert">{error}</p>;
  if (!data) return null;
  const timing = data.inspection.timing;
  return <details className="director-operation-panel" aria-label="参考与候选对照">
    <summary>参考、时长与版本对照</summary>
    {error && <p role="alert">{error}</p>}
    <p>保存版本 {data.revision} · 当前候选 {data.inspection.candidate_version || '尚未生成'}</p>
    <label>参考视频<select value={data.inspection.reference_comparison.reference?.asset_id || ''} disabled={busy} onChange={event => void chooseReference(event.target.value)}>
      <option value="">使用原参考绑定／无参考</option>{data.inspection.reference_options.map(row => <option value={row.id} key={row.id}>{row.title}</option>)}
    </select></label>
    <ReferenceDifferenceSummary inspection={data.inspection} />
    <dl><dt>剧情请求时长</dt><dd>{value(timing.requested_seconds)} 秒</dd>
      <dt>模型对齐的预计候选</dt><dd>{timing.expected_candidate ? `${timing.expected_candidate.frame_count} 帧／${timing.expected_candidate.playback_seconds} 秒／${timing.expected_candidate.fps}fps` : '未知；按当前方案预检'}</dd>
      <dt>当前候选实际测量</dt><dd>{value(timing.observed_candidate.duration_seconds)} 秒 · {value(timing.observed_candidate.frames)} 帧</dd>
      <dt>装配采用的取片</dt><dd>{timing.assembly_take ? `从 ${timing.assembly_take.start_seconds} 秒取 ${timing.assembly_take.duration_seconds} 秒；${timing.assembly_take.frame_count} 帧／${timing.assembly_take.playback_seconds} 秒` : '装配构建器未提供取片信息'}</dd></dl>
    <p>实际文件测量与预计值分开显示；装配逐段按帧取整，尾段也如此。差异不会自动切换方案。</p>
    <div className="assembly-edit-previews"><div><label>左侧对照<select value={left} onChange={event => setLeft(event.target.value)}>{sources.map(row => <option key={row.id} value={row.id}>{row.title}</option>)}</select></label>{preview(a, leftRef)}</div>
      <div><label>右侧对照<select value={right} onChange={event => setRight(event.target.value)}>{sources.map(row => <option key={row.id} value={row.id}>{row.title}</option>)}</select></label>{preview(b, rightRef)}</div></div>
    <button className="button secondary compact" disabled={!playable(a) || !playable(b) || !ready.includes(a?.id || '') || !ready.includes(b?.id || '') || (leftRef.current?.readyState ?? 0) < 3 || (rightRef.current?.readyState ?? 0) < 3}
      onClick={() => { if (leftRef.current && a) leftRef.current.currentTime = a.offset_seconds; if (rightRef.current && b) rightRef.current.currentTime = b.offset_seconds; }}>定位两侧片段起点</button>
    {sources.filter(row => row.id === left || row.id === right).map(row => <p key={row.id}>{row.title} · 从{row.offset_seconds}秒开始 · 可用{value(row.duration_seconds)}秒 · {row.facts.audio_streams === 0 ? '无音轨' : row.facts.audio_streams ? '有音轨' : '音轨未知'}</p>)}
    <details><summary>版本参数差异</summary>{data.items.map(item => <div key={item.task_id}><strong>{item.task_id}</strong>{item.parameter_differences.map(row => <p key={row.field}>{names[row.field] || row.field}：当前 {value(row.current)}／该版 {value(row.previous)}</p>)}</div>)}</details>
    <details><summary>重做理由与影响（仅预告，不提交）</summary>
      {data.review_records.map(row => <p key={row.revision}>审核记录 {row.revision} · {row.data.status} · {row.data.note || '未填写理由'}{row.data.candidate_ref ? '（已绑定候选）' : '（未绑定特定候选）'}</p>)}
      {data.change_impact.effects.map(row => <p key={`${row.kind}:${row.id}`}>{row.id} · {{ stale_when_video_changes: '换成新当前视频后过期', needs_recheck: '需主动核对输入' }[row.effect] || row.effect} {row.note}</p>)}
      <p>{data.change_impact.no_effect}；旧候选和回执保留。技术尺寸、时长和音轨探测不等于艺术审核，重做理由沿用现有审核入口保存。</p>
    </details>
  </details>;
}
