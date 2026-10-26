// 字幕（SRT / WebVTT）→ 锚点配音的时间轴。
// 只认时间轴明确的行；没有时间轴就不猜位置——那是替用户编造，不是导入。
export type SubtitleCue = { at_seconds: number; text: string };
export type SubtitleParse = { cues: SubtitleCue[]; total: number; dropped: number; error?: string };

const TIMING = /^\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})/;

function seconds(hours: string, minutes: string, secs: string, millis: string): number {
  return Number(hours) * 3600 + Number(minutes) * 60 + Number(secs) + Number(millis.padEnd(3, '0')) / 1000;
}

function clean(lines: string[]): string {
  return lines
    .map(line => line
      .replace(/<[^>]*>/g, '')          // <i> / <font> 之类的行内标签
      .replace(/\{\\?[^}]*\}/g, '')    // SRT 对齐标记 {\an8}
      .replace(/&nbsp;/g, ' ')
      .replace(/&amp;/g, '&')
      .replace(/&lt;/g, '<')
      .replace(/&gt;/g, '>'))
    .join(' ')
    .replace(/\s+/g, ' ')
    .trim();
}

export function parseSubtitles(raw: string, limit: number): SubtitleParse {
  const text = raw.replace(/^\uFEFF/, '').replace(/\r\n?/g, '\n');
  const blocks = text.split(/\n{2,}/).map(block => block.split('\n').filter(line => line.trim() !== ''));
  const cues: SubtitleCue[] = [];
  let total = 0;
  for (const lines of blocks) {
    const timing = lines.findIndex(line => TIMING.test(line));
    if (timing < 0) continue;
    const match = TIMING.exec(lines[timing])!;
    const at = seconds(match[1], match[2], match[3], match[4]);
    const body = clean(lines.slice(timing + 1));
    if (!Number.isFinite(at) || at < 0) continue;
    total += 1;
    if (!body) continue;
    cues.push({ at_seconds: Math.round(at * 1000) / 1000, text: body });
  }
  if (!total) {
    return { cues: [], total: 0, dropped: 0, error: '没有找到时间轴（SRT 的 00:00:01,000 --> 00:00:03,000 这种行）。纯文本字幕不猜位置。' };
  }
  cues.sort((left, right) => left.at_seconds - right.at_seconds);
  const kept = cues.slice(0, limit);
  return { cues: kept, total, dropped: cues.length - kept.length };
}
