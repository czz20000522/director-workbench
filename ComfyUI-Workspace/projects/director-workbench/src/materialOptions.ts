import type { MaterialOption } from './MaterialSelect';
import { assetLabel, assetTitle, type TreeKind } from './shotLabels';

type SegmentMaterials = {
  id: string; keyframes?: { first?: string | null; last?: string | null };
  reference_frame?: string | null;
  audio?: { guide?: string; delivery_master?: string }; video?: { path?: string };
};

export function projectMaterialOptions(project: {
  media?: Record<string, string>; assets?: Array<Record<string, unknown>>;
} | null, segments: SegmentMaterials[]): MaterialOption[] {
  const options = new Map<string, MaterialOption>();
  function add(path: unknown, label: string, kind?: MaterialOption['kind']) {
    if (typeof path !== 'string' || !path || /^(https?:|blob:|data:|\/media)/i.test(path)) return;
    const extension = path.split('.').pop()?.toLowerCase() ?? '';
    const inferred = /^(png|jpe?g|webp|gif|bmp)$/.test(extension) ? 'image'
      : /^(wav|mp3|flac|m4a|aac|ogg)$/.test(extension) ? 'audio'
      : /^(mp4|mov|webm|mkv)$/.test(extension) ? 'video' : undefined;
    if (!kind && !inferred) return;
    const key = path.replace(/\\/g, '/').toLowerCase();
    if (!options.has(key)) options.set(key, { value: path, label, kind: kind ?? inferred! });
  }
  for (const asset of project?.assets ?? []) {
    const kind = asset.kind === 'audio' || asset.kind === 'image' ? asset.kind as TreeKind : 'video';
    const label = assetTitle(String(asset.id ?? ''), String(asset.title ?? asset.id ?? '项目素材'), kind);
    add(asset.image_path, `${label} · 图片`, 'image');
    if (asset.sources && typeof asset.sources === 'object') {
      for (const [version, path] of Object.entries(asset.sources)) add(path, `${label} · ${version}`);
    }
  }
  for (const [name, path] of Object.entries(project?.media ?? {})) add(path, assetLabel(name));
  for (const segment of segments) {
    add(segment.reference_frame, `${assetLabel(segment.id, 'image')} · 参考画面`, 'image');
    add(segment.keyframes?.first, `${assetLabel(segment.id, 'image')} · 首帧`, 'image');
    add(segment.keyframes?.last, `${assetLabel(segment.id, 'image')} · 尾帧`, 'image');
    add(segment.audio?.guide, `${assetLabel(segment.id, 'audio')} · 表演引导`, 'audio');
    add(segment.audio?.delivery_master, `${assetLabel(segment.id, 'audio')} · 成片音频`, 'audio');
    add(segment.video?.path, `${assetLabel(segment.id)} · 视频`, 'video');
  }
  return [...options.values()];
}
