export type ReferenceOriginData = {
  version_history?: Array<{ merged_segment?: ReferenceOriginData }>;
  source_analysis?: string;
  source_segment?: string;
  source_revision?: number;
  source_range_seconds?: [number, number];
  source_note_status?: string;
  source_observation?: string;
  source_user_note?: string;
  reference_frame?: string | null;
};

function collectOrigins(segment: ReferenceOriginData): ReferenceOriginData[] {
  const origins: ReferenceOriginData[] = [];
  const seen = new Set<string>();
  const visit = (item: ReferenceOriginData) => {
    if (item.source_analysis) {
      const key = JSON.stringify([item.source_analysis, item.source_segment, item.source_revision, item.source_range_seconds]);
      if (!seen.has(key)) { seen.add(key); origins.push(item); }
    }
    item.version_history?.forEach(entry => { if (entry.merged_segment) visit(entry.merged_segment); });
  };
  visit(segment);
  return origins;
}

export default function ReferenceOrigin({ segment, onOpen }: { segment: ReferenceOriginData; onOpen: (analysisId: string) => void }) {
  const origins = collectOrigins(segment);
  if (!origins.length) return null;
  return <details className="director-operation-panel">
    <summary>参考来源 · {origins.length > 1 ? `${origins.length} 个来源段` : origins[0].source_segment ?? '时间段'}</summary>
    {origins.length > 1 && <p>合并前各段的来源分别保留；下列时间均属于参考视频。</p>}
    {origins.map((origin, index) => <div className="director-segment-edit-grid" key={index}>
      <p><strong>{origin.source_segment ?? '时间段'}</strong> · 拆解：{origin.source_analysis} · 来源版本 {origin.source_revision ?? '未记录'}</p>
      <p>参考时间：{origin.source_range_seconds ? `${origin.source_range_seconds[0]}–${origin.source_range_seconds[1]} 秒` : '未记录'} · 校订{origin.source_note_status === 'reviewed' ? '已完成' : '待复核'}</p>
      {origin.source_observation && <p><strong>参考观察（模型）</strong><br />{origin.source_observation}</p>}
      {origin.source_user_note && <p><strong>转换时的改编备注</strong><br />{origin.source_user_note}</p>}
      {origin.reference_frame && <img className="director-material-preview" src={`/media-file?path=${encodeURIComponent(origin.reference_frame)}`} alt={`${origin.source_segment ?? '来源'} 参考时间段画面`} loading="lazy" />}
      <button className="button secondary compact" onClick={() => onOpen(origin.source_analysis!)}>打开来源拆解</button>
    </div>)}
    <small>转换时的来源记录；本作关键帧与审核独立保存。</small>
  </details>;
}
