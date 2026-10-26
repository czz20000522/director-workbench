import type { OutputResolution } from './ResolutionPicker';
import rules from '../shared/quality-intent.json';

type Tier = { resolution: OutputResolution; label: string; phrases: string[] };
type Config = {
  tiers: Tier[];
  negations: { prefixes: string[]; chars: string[]; words: string[] };
  video: { max_resolution: OutputResolution };
};

const config = rules as unknown as Config;
const tiers = config.tiers;
const order = new Map(tiers.map((tier, index) => [tier.resolution, index]));
const labels = new Map(tiers.map(tier => [tier.resolution, tier.label]));
const terms = tiers
  .flatMap(tier => tier.phrases.map(phrase => ({ resolution: tier.resolution, phrase })))
  .sort((left, right) => right.phrase.length - left.phrase.length);
const ascii = (value: string) => /^[\x00-\x7f]+$/.test(value);

export type QualityIntent = { resolution: OutputResolution; label: string; phrase: string };

export const qualityLabel = (value: OutputResolution) => labels.get(value) ?? value.toUpperCase();

function negated(prefix: string) {
  const probe = prefix.replace(/[\s，,。.;；:：!！?？]+$/, '');
  const { prefixes, chars, words } = config.negations;
  if (prefixes.some(item => probe.endsWith(item))) return true;
  if (probe && chars.includes(probe.slice(-1))) return true;
  return words.length > 0 && new RegExp(`(?<![a-z])(?:${words.join('|')})(?![a-z])`).test(probe);
}

/** The same rule the backend enforces: the highest tier named by the prompt wins. */
export function detectQualityIntent(prompt: string): QualityIntent | null {
  const text = (prompt || '').toLowerCase();
  if (!text.trim()) return null;
  const taken: [number, number][] = [];
  const found: { resolution: OutputResolution; phrase: string; position: number }[] = [];
  for (const term of terms) {
    const needle = term.phrase.toLowerCase();
    let from = 0;
    while (from <= text.length - needle.length) {
      const index = text.indexOf(needle, from);
      if (index < 0) break;
      const end = index + needle.length;
      from = end;
      if (ascii(term.phrase)) {
        if (/[0-9a-z]/.test(text[index - 1] || '') || /[0-9a-z]/.test(text[end] || '')) continue;
      }
      if (taken.some(([start, stop]) => index < stop && start < end)) continue;
      if (negated(text.slice(Math.max(0, index - 6), index))) continue;
      taken.push([index, end]);
      found.push({ resolution: term.resolution, phrase: prompt.slice(index, end), position: index });
    }
  }
  if (!found.length) return null;
  found.sort((left, right) => (order.get(left.resolution)! - order.get(right.resolution)!) || left.position - right.position);
  const best = found[0];
  return { resolution: best.resolution, label: qualityLabel(best.resolution), phrase: best.phrase };
}

export function capVideoQuality(value: OutputResolution): OutputResolution {
  return order.get(value)! < order.get(config.video.max_resolution)! ? config.video.max_resolution : value;
}
