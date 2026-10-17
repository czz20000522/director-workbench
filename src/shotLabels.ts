export type TreeKind = 'video' | 'audio' | 'image';

export function assetLabel(id: string, kind: TreeKind = 'video'): string {
  if (id === 'empty-project') return '尚无分镜';
  const shot = /^S0*(\d+)$/i.exec(id);
  if (shot) return `${kind === 'audio' ? '音频' : kind === 'image' ? '关键帧' : '分镜'}${Number(shot[1])}`;
  if (id === 'assembly' || /^master$/i.test(id)) return '完整成片';
  return id;
}

export function assetTitle(id: string, title: string, kind: TreeKind = 'video'): string {
  const label = assetLabel(id, kind);
  if (id === 'empty-project') return label;
  const prefix = `${id} · `;
  const withoutId = title.startsWith(prefix) ? title.slice(prefix.length) : title;
  if (/^reused-[a-f\d]+$/i.test(id)) {
    const source = /^S0*(\d+)\s*·\s*(.+)$/i.exec(withoutId);
    return source ? `${assetLabel(`S${source[1]}`, kind)} · ${source[2]}` : withoutId || '复用素材';
  }
  if (/^asset-[a-f\d]{16,}$/i.test(id) && withoutId && withoutId !== id && !withoutId.startsWith(id.slice(6))) return withoutId;
  return withoutId && withoutId !== id ? `${label} · ${withoutId}` : label;
}
