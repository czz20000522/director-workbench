import { useEffect, useRef, useState } from 'react';
import AudioEditPanel from './AudioEditPanel';
import { mediaUrl } from './api/client';
import './AssemblyEditPanel.css';

type Join = {
  key: string; left_id: string; right_id: string; cut_seconds: number;
  left_preview_start: number; right_preview_end: number;
  status: 'pending' | 'approved' | 'redo_left' | 'redo_right' | 'stale';
  review_ready?: boolean; review_completed?: boolean;
  constraint: string; note: string;
  signature: { left_video?: string; right_video?: string };
};
type Continuity = {
  key: string; left_id?: string; right_id?: string; advisory: boolean;
  available: boolean; status: 'ok' | 'unavailable'; warnings: string[]; reason?: string;
  video?: { differences?: Record<string, { different: boolean; delta?: number }> };
  frames?: { mean_absolute_rgb_difference?: number; brightness_delta?: number; note?: string };
  audio?: { differences?: Record<string, { different: boolean }>; boundary?: {
    left?: { rms: number; peak: number; silent: boolean; clipping: boolean };
    right?: { rms: number; peak: number; silent: boolean; clipping: boolean };
  } };
};
type Edit = {
  revision: number; joins: Join[]; blocking_joins: string[];
  join_review?: { state: string; completed: boolean; remaining_joins: string[] };
  sound: { policy: string; audio_asset_id?: string | null; path?: string | null; duration_seconds?: number | null; error?: string };
  audio_assets: Array<{ id: string; title: string }>; warnings?: string[];
  continuity?: { advisory: boolean; available: boolean; note: string; joins: Continuity[] };
  target_duration_seconds: number;
  timing_summary?: { playback_seconds: number; frame_count: number; precision: string; segments: { segment_id: string; start_seconds: number; duration_seconds: number; frame_count: number; playback_seconds: number }[] } | null;
};
type Props = { projectId: string; planRevision: number; assemblyPath?: string; onPlanChanged: () => void | Promise<void>; onBrowse: () => void; onOpenShot: (shotId: string) => void };
const statusLabel: Record<Join['status'], string> = {
  pending: '待检查', approved: '已通过', redo_left: '重做前段', redo_right: '重做后段', stale: '版本已变化',
};

export default function AssemblyEditPanel({ projectId, planRevision, assemblyPath, onPlanChanged, onBrowse, onOpenShot }: Props) {
  const [edit, setEdit] = useState<Edit | null>(null);
  const [supported, setSupported] = useState(false);
  const [openKey, setOpenKey] = useState('');
  const [drafts, setDrafts] = useState<Record<string, { constraint: string; note: string }>>({});
  const [policy, setPolicy] = useState('segment_native');
  const [audioAssetId, setAudioAssetId] = useState('');
  const [error, setError] = useState('');
  const [preflight, setPreflight] = useState('');
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const loadSequence = useRef(0);
  const leftRef = useRef<HTMLVideoElement>(null);
  const rightRef = useRef<HTMLVideoElement>(null);
  const fullRef = useRef<HTMLVideoElement>(null);

  async function load() {
    const sequence = ++loadSequence.current;
    setLoading(true); setPreflight('');
    try {
      const response = await fetch(`/api/projects/${encodeURIComponent(projectId)}/assembly/edit`);
      if (sequence !== loadSequence.current) return;
      if (response.status === 404) { setSupported(false); return; }
      const data = await response.json();
      if (sequence !== loadSequence.current) return;
      if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '接点计划读取失败');
      setEdit(data as Edit);
      setSupported(true);
      setDrafts(Object.fromEntries((data as Edit).joins.map(join => [join.key, { constraint: join.constraint, note: join.note }])));
      setPolicy(['complete_master', 'mute'].includes(data.sound.policy) ? data.sound.policy : 'segment_native');
      setAudioAssetId(data.sound.audio_asset_id ?? '');
      setError('');
    } finally { if (sequence === loadSequence.current) setLoading(false); }
  }

  async function checkPreflight() {
    const sequence = loadSequence.current;
    try {
      const response = await fetch(`/api/projects/${encodeURIComponent(projectId)}/assembly/preflight`);
      const data = await response.json();
      if (sequence !== loadSequence.current) return;
      if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '装配预检失败');
      const missing = (data.missing_videos as string[]).join('、');
      const joins = (data.blocking_joins as string[]).join('、');
      setPreflight((data.ready ? '预检通过：接点和音轨可用于装配。' : `暂不可装配${missing ? `；缺少视频：${missing}` : ''}${joins ? `；待审核接点：${joins}` : ''}${data.sound?.error ? `；音轨：${data.sound.error}` : ''}`) + ((data.warnings as string[] | undefined)?.length ? ` 提醒：${data.warnings.join('；')}` : ''));
    } catch (cause) { setPreflight(cause instanceof Error ? cause.message : '装配预检失败'); }
  }

  useEffect(() => { setEdit(null); setOpenKey(''); void load().catch(cause => { setSupported(true); setError(cause instanceof Error ? cause.message : '接点计划读取失败'); }); return () => { loadSequence.current++; }; }, [projectId, planRevision]);

  async function save(path: string, body: Record<string, unknown>) {
    if (!edit || busy || loading) return;
    setBusy(true); setError('');
    try {
      const response = await fetch(`/api/projects/${encodeURIComponent(projectId)}/assembly/${path}`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ expected_revision: edit.revision, ...body }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '保存失败，请刷新计划后重试');
      setEdit(data as Edit);
      await onPlanChanged();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '保存失败，请刷新计划后重试');
      void load().catch(() => {});
    } finally { setBusy(false); }
  }

  const selected = edit?.joins.find(join => join.key === openKey);
  const continuityByKey = new Map((edit?.continuity?.joins ?? []).map(row => [row.key, row]));
  const selectedContinuity = selected ? continuityByKey.get(selected.key) : undefined;
  const continuityDifferenceLabels: Record<string, string> = {
    width: '宽度', height: '高度', fps: '帧率', rotation: '旋转', aspect_ratio: '宽高比', duration_seconds: '时长',
    sample_rate: '采样率', channels: '声道数',
  };
  if (!supported) return null;
  return <details className="assembly-edit-panel">
    <summary><strong>接点与全片声音</strong><span>{edit ? `${edit.joins.length} 个接点 · ${(edit.join_review?.remaining_joins ?? edit.blocking_joins).length} 个待处理` : '读取中'}</span></summary>
    {error && <p role="alert" className="assembly-edit-error">{error}</p>}
    {edit && <div className="assembly-edit-content"><div className="assembly-edit-row"><button type="button" className="button secondary compact" disabled={busy || loading} onClick={() => void checkPreflight()}>无 GPU 装配预检</button>{preflight && <small role="status">{preflight}</small>}</div>
      <section><h3>全片声音</h3><p>选择已登记的完整音频母版后，装配会用它替换各段原音。母版应已包含需要保留的对白和音效。</p>
        {edit.timing_summary && <details><summary>装配取片：{edit.timing_summary.frame_count}帧／{edit.timing_summary.playback_seconds}秒</summary>
          {edit.timing_summary.segments.map(row => <p key={row.segment_id}>{row.segment_id}：从{row.start_seconds}秒取{row.duration_seconds}秒 → {row.frame_count}帧／{row.playback_seconds}秒</p>)}<p>{edit.timing_summary.precision}</p></details>}
        <div className="assembly-edit-row"><label>音轨方式<select value={policy} onChange={event => setPolicy(event.target.value)}><option value="segment_native">保留各段原音</option><option value="mute">全片静音（移除声音）</option><option value="complete_master">统一完整音轨</option></select></label>
          {policy === 'complete_master' && <label>已登记音频<select value={audioAssetId} onChange={event => setAudioAssetId(event.target.value)}><option value="">选择音频</option>{edit.audio_assets.map(asset => <option key={asset.id} value={asset.id}>{asset.title}</option>)}</select></label>}
          <button className="button secondary compact" type="button" onClick={onBrowse}>登记素材</button><button className="button secondary compact" type="button" onClick={() => void load().catch(cause => setError(cause instanceof Error ? cause.message : '音频列表刷新失败'))}>刷新素材</button>
          <button className="button primary compact" type="button" disabled={busy || (policy === 'complete_master' && !audioAssetId)} onClick={() => void save('sound', { policy, audio_asset_id: policy === 'complete_master' ? audioAssetId : null })}>保存音轨方案</button></div>
        <small>剧情计划总长 {edit.target_duration_seconds} 秒{edit.sound.duration_seconds !== null && edit.sound.duration_seconds !== undefined ? ` · 当前音轨实测 ${edit.sound.duration_seconds} 秒` : ' · 当前独立音轨时长未知或不适用'}。短母版明确拒绝；请先用全片母版后期明确补静音，最长处理30分钟。原音接缝不自动混合。</small>
        {edit.sound.error && <p role="alert" className="assembly-edit-error">{edit.sound.error}</p>}
        {edit.sound.path && <audio controls preload="metadata" src={mediaUrl(edit.sound.path)} aria-label="当前全片音轨试听" />}
        <AudioEditPanel projectId={projectId} audioAssets={edit.audio_assets} onBrowse={onBrowse} onComplete={() => { void load().catch(cause => setError(cause.message)); onPlanChanged(); }} />
      </section>
      <section className="assembly-continuity" aria-label="自动检查（仅供参考）"><h3>自动检查（仅供参考）</h3><p>{edit.continuity?.note ?? 'CPU 媒体指标仅供人工审核参考，不会自动通过或阻止装配。'}</p>
        {edit.continuity?.joins?.length ? <div className="assembly-continuity-list">{edit.continuity.joins.map(row => {
          const differences = [
            ...Object.entries(row.video?.differences ?? {}).filter(([, value]) => value.different).map(([key]) => continuityDifferenceLabels[key] ?? key),
            ...Object.entries(row.audio?.differences ?? {}).filter(([, value]) => value.different).map(([key]) => continuityDifferenceLabels[key] ?? key),
          ];
          return <div className="assembly-continuity-row" key={row.key}><strong>连续性 · {row.left_id} → {row.right_id}</strong><span className={row.available ? 'assembly-continuity-ok' : 'assembly-continuity-unavailable'}>{row.available ? '已测量' : '不可用'}</span><small>{differences.length ? `规格差异：${differences.join('、')}` : '未发现规格差异'}{row.warnings.length ? ` · ${row.warnings.join('；')}` : ''}{row.reason ? ` · ${row.reason}` : ''}</small></div>;
        })}</div> : <p>至少两个分镜才会形成自动检查接点。</p>}
      </section>
      <section><h3>相邻接点</h3><p>逐个查看前段末尾与后段开头，记录动作、构图和声音连续性。任一当前视频版本变化后，原结论会过期。</p>
        {edit.joins.length === 0 && <p>至少两个分镜才会形成接点。</p>}
        <div className="assembly-edit-joins">{edit.joins.map(join => <button type="button" key={join.key} className={openKey === join.key ? 'active' : ''} onClick={() => setOpenKey(openKey === join.key ? '' : join.key)}><span>{join.left_id} → {join.right_id}</span><small>{Number(join.cut_seconds ?? 0).toFixed(1)} 秒 · {statusLabel[join.status]}</small></button>)}</div>
        {selected && <div className="assembly-edit-review"><div className="assembly-edit-previews"><div><span>{selected.left_id} 末尾</span>{selected.signature.left_video ? <video ref={leftRef} controls preload="metadata" src={mediaUrl(selected.signature.left_video)} onLoadedMetadata={event => { event.currentTarget.currentTime = selected.left_preview_start; }} /> : <p>尚无当前视频</p>}</div><div><span>{selected.right_id} 开头</span>{selected.signature.right_video ? <video ref={rightRef} controls preload="metadata" src={mediaUrl(selected.signature.right_video)} onLoadedMetadata={event => { event.currentTarget.currentTime = 0; }} /> : <p>尚无当前视频</p>}</div></div>
          {selectedContinuity && <div className="assembly-continuity-detail"><strong>自动检查结果（仅供参考）</strong>{selectedContinuity.available ? <><span>{selectedContinuity.frames?.mean_absolute_rgb_difference !== undefined ? `边界画面 RGB 差异 ${selectedContinuity.frames.mean_absolute_rgb_difference}` : '边界画面未测量'}{selectedContinuity.frames?.brightness_delta !== undefined ? ` · 亮度差 ${selectedContinuity.frames.brightness_delta}` : ''}</span>{selectedContinuity.audio?.boundary && <span>边界音频 RMS：前段 {selectedContinuity.audio.boundary.left?.rms ?? '—'}，后段 {selectedContinuity.audio.boundary.right?.rms ?? '—'}</span>}</> : <span>{selectedContinuity.reason ?? '媒体指标不可用'}</span>}{selectedContinuity.warnings.length > 0 && <small>提示：{selectedContinuity.warnings.join('；')}</small>}<small>{selectedContinuity.frames?.note ?? '启发式、非艺术判断；请以人工审核为准。'}</small></div>}
          <button className="button secondary compact" type="button" onClick={() => { if (leftRef.current) leftRef.current.currentTime = selected.left_preview_start; if (rightRef.current) rightRef.current.currentTime = 0; if (fullRef.current) fullRef.current.currentTime = Math.max(0, Number(selected.cut_seconds) - 1); }}>定位接点</button>
          {assemblyPath && <div className="assembly-edit-full"><span>已装配候选的同一接点</span><video ref={fullRef} controls preload="metadata" src={mediaUrl(assemblyPath)} /></div>}
          <label>连续性约束<input value={drafts[selected.key]?.constraint ?? ''} maxLength={2000} onChange={event => setDrafts(value => ({ ...value, [selected.key]: { ...value[selected.key], constraint: event.target.value } }))} placeholder="例如：人物仍面向画面右侧，雨声持续" /></label>
          <label>审核记录<textarea rows={2} value={drafts[selected.key]?.note ?? ''} maxLength={2000} onChange={event => setDrafts(value => ({ ...value, [selected.key]: { ...value[selected.key], note: event.target.value } }))} placeholder="记录画面与声音接点的实际观察" /></label>
          <div className="assembly-edit-actions">{(['pending', 'approved', 'redo_left', 'redo_right'] as const).map(status => <button key={status} className={`button ${status === 'approved' ? 'primary' : 'secondary'} compact`} type="button" disabled={busy || (status === 'approved' && selected.review_ready === false)} onClick={() => void save(`joins/${encodeURIComponent(selected.left_id)}/${encodeURIComponent(selected.right_id)}`, { status, ...drafts[selected.key] })}>{status === 'pending' ? '保存待检查' : statusLabel[status]}</button>)}<button type="button" className="button secondary compact" onClick={() => onOpenShot(selected.left_id)}>打开前段</button><button type="button" className="button secondary compact" onClick={() => onOpenShot(selected.right_id)}>打开后段</button></div>
        </div>}
      </section>
    </div>}
  </details>;
}
