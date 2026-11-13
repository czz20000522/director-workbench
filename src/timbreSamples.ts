import { mediaUrl } from './api/client';

// 音色小样本地索引：把「哪段音色描述 → 哪个可播放文件」记在浏览器里。
// 后端 preset-library 目前只存起点参数（_validate_timbre 的字段白名单里没有小样），
// 所以小样先由前端自己关联：生成成功后记一条，卡片按描述命中，就能直接试听。
// 后端以后把 sample 字段返回到卡片里时，PresetLibraryPanel 会优先用它，这里作兜底。
export type TimbreSample = {
  /** 交给 /media-file?path= 的路径；也可能已经是完整的媒体 URL。 */
  path: string;
  /** 归一化后的音色描述，作为索引键。 */
  description: string;
  /** 来自音色库卡片时记下卡片名，便于精确命中同名卡片。 */
  name?: string;
  duration_seconds?: number;
  text?: string;
  created_at: number;
  source?: 'timbre-panel' | 'companion' | 'library';
};

const SAMPLES_KEY = 'director-workbench:timbre-samples:v1';
const HIDDEN_KEY = 'director-workbench:timbre-hidden:v1';
const STAGED_KEY = 'director-workbench:timbre-staged:v1';
const CHANGED = 'director:timbre-samples-changed';
const PRESET_CHANGED = 'director:preset-library-changed';
const MAX_SAMPLES = 60;

/** 任一面板存入/改名/删掉预设卡片后广播，库面板据此重新拉一次，不用刷新页面。 */
export function notifyPresetLibraryChanged() { window.dispatchEvent(new Event(PRESET_CHANGED)); }
export function onPresetLibraryChanged(handler: () => void): () => void {
  window.addEventListener(PRESET_CHANGED, handler);
  return () => window.removeEventListener(PRESET_CHANGED, handler);
}

export const normalizeDescription = (value: string) => value.trim().replace(/\s+/g, ' ').toLowerCase();

function readSamples(): TimbreSample[] {
  try {
    const raw = JSON.parse(localStorage.getItem(SAMPLES_KEY) || '[]');
    if (!Array.isArray(raw)) return [];
    return raw.filter((item): item is TimbreSample =>
      Boolean(item) && typeof item.path === 'string' && typeof item.description === 'string');
  } catch { return []; }
}

function writeSamples(items: TimbreSample[]) {
  try { localStorage.setItem(SAMPLES_KEY, JSON.stringify(items)); } catch { /* 存储不可用时不阻断生成 */ }
  window.dispatchEvent(new Event(CHANGED));
}

export function listSamples(): TimbreSample[] {
  const hidden = new Set(readHidden());
  return readSamples()
    .filter(item => !hidden.has(item.path))
    .sort((a, b) => b.created_at - a.created_at);
}

export function sampleFor(description: string, name?: string): TimbreSample | null {
  const key = normalizeDescription(description || '');
  if (!key) return null;
  const items = listSamples();
  const byName = name ? items.find(item => item.name === name && normalizeDescription(item.description) === key) : null;
  return byName || items.find(item => normalizeDescription(item.description) === key) || null;
}

/** 同一段描述只留最新一条：描述没改 → 覆盖原卡的小样；描述改了 → 自然变成另一条音色。 */
export function rememberSample(sample: Omit<TimbreSample, 'created_at'> & { created_at?: number }) {
  const description = normalizeDescription(sample.description);
  if (!description || !sample.path.trim()) return;
  const next = readSamples().filter(item =>
    !(normalizeDescription(item.description) === description && (!sample.name || item.name === sample.name)));
  next.unshift({ ...sample, description, created_at: Date.now() });
  writeSamples(next.slice(0, MAX_SAMPLES));
}

export function forgetSample(path: string) {
  writeSamples(readSamples().filter(item => item.path !== path));
}

/** 从音色库点「带入」时留一份小样，音色设计面板挂载后按描述认领（不清空，改了描述自然不再显示）。 */
export function stageSample(sample: TimbreSample) {
  try { localStorage.setItem(STAGED_KEY, JSON.stringify(sample)); } catch { /* noop */ }
}

export function stagedSample(): TimbreSample | null {
  try {
    const raw = JSON.parse(localStorage.getItem(STAGED_KEY) || 'null');
    return raw && typeof raw.path === 'string' && typeof raw.description === 'string' ? raw as TimbreSample : null;
  } catch { return null; }
}

/** 只想让某条已生成样本不再出现在列表里时记隐藏名单，随时可以恢复。 */
export function readHidden(): string[] {
  try {
    const raw = JSON.parse(localStorage.getItem(HIDDEN_KEY) || '[]');
    return Array.isArray(raw) ? raw.filter((item): item is string => typeof item === 'string') : [];
  } catch { return []; }
}

export function hideSample(id: string) {
  try { localStorage.setItem(HIDDEN_KEY, JSON.stringify([...new Set([...readHidden(), id])])); } catch { /* noop */ }
  window.dispatchEvent(new Event(CHANGED));
}

export function restoreHidden() {
  try { localStorage.setItem(HIDDEN_KEY, '[]'); } catch { /* noop */ }
  window.dispatchEvent(new Event(CHANGED));
}

export function sampleSrc(path: string): string {
  if (/^(https?:\/\/|\/media-file|\/material-file|data:|blob:)/i.test(path)) return path;
  return mediaUrl(path) || path;
}

export function onSamplesChanged(handler: () => void): () => void {
  window.addEventListener(CHANGED, handler);
  return () => window.removeEventListener(CHANGED, handler);
}
