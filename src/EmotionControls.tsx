import { useId } from 'react';
import MaterialSelect, { type MaterialOption } from './MaterialSelect';

export type EmotionMode = 'none' | 'reference' | 'text' | 'vector';
export type Emotion = { mode: EmotionMode; alpha: number; reference: string; text: string; vector: number[] };
export const emotionKeys = ['happy', 'angry', 'sad', 'afraid', 'disgusted', 'melancholic', 'surprised', 'calm'] as const;
const emotionLabels = ['喜', '怒', '哀', '惧', '厌', '郁', '惊', '平'];
export const noEmotion: Emotion = { mode: 'none', alpha: 1, reference: '', text: '', vector: [0, 0, 0, 0, 0, 0, 0, 0] };

// What actually reaches the engine: none sends nothing, the rest maps to one IndexTTS argument set.
export function emotionPayload(emotion: Emotion | null | undefined): Record<string, unknown> | null {
  if (!emotion || emotion.mode === 'none') return null;
  if (emotion.mode === 'reference') return { mode: 'reference', alpha: emotion.alpha, reference: emotion.reference };
  if (emotion.mode === 'text') return { mode: 'text', text: emotion.text.trim() };
  return { mode: 'vector', vector: emotion.vector.map(value => Number(value.toFixed(4))) };
}
export function readEmotion(value: unknown): Emotion {
  if (!value || typeof value !== 'object') return { ...noEmotion, vector: [...noEmotion.vector] };
  const raw = value as Partial<Emotion>;
  const mode = ['none', 'reference', 'text', 'vector'].includes(raw.mode || '') ? raw.mode as EmotionMode : 'none';
  const vector = Array.isArray(raw.vector) && raw.vector.length === 8
    ? raw.vector.map(item => typeof item === 'number' && Number.isFinite(item) ? item : 0) : [...noEmotion.vector];
  return { mode, alpha: typeof raw.alpha === 'number' ? raw.alpha : 1,
    reference: typeof raw.reference === 'string' ? raw.reference : '',
    text: typeof raw.text === 'string' ? raw.text : '', vector };
}
// An emotion mode is only submittable when its own required knob is filled in.
export function emotionReady(emotion: Emotion): boolean {
  if (emotion.mode === 'none') return true;
  if (emotion.mode === 'reference') return Boolean(emotion.reference) && Number.isFinite(emotion.alpha) && emotion.alpha >= 0 && emotion.alpha <= 1;
  if (emotion.mode === 'text') return Boolean(emotion.text.trim());
  return emotion.vector.length === 8 && emotion.vector.every(value => Number.isFinite(value) && value >= 0 && value <= 1);
}

export default function EmotionControls({ emotion, modes, unsupported, options, locked, onChange, onBrowse, subtitle, textNote }: {
  emotion: Emotion; modes?: Record<string, boolean>; unsupported?: string[]; options: MaterialOption[]; locked: boolean;
  onChange: (next: Emotion) => void; onBrowse: () => void; subtitle?: string; textNote?: string;
}) {
  const prefix = useId();
  const available = { none: true, reference: true, vector: true, text: false, ...(modes || {}) };
  const note = emotion.mode === 'reference' ? '情绪参考音频会被引擎截到前 15 秒；强度 0 等于不改变。'
    : emotion.mode === 'text' ? (textNote ?? '文本情绪由本机 qwen0.6bemo4-merge 判断，只影响“怎么说”，不会换音色。')
      : emotion.mode === 'vector' ? '八个强度按官方界面的偏差系数缩放，并保证总和不大于 0.8。'
        : '不指定情绪时，只按台词与音色参考生成。';
  return <div className="sf-post-subblock">
    <div className="sf-post-section-head"><h3 className="sf-post-subtitle">情绪</h3><p className="sf-post-meta">{subtitle ?? '可选 · 只改变说法，不改变音色来源'}</p></div>
    <div className="sf-post-fields">
      <div className="sf-post-field"><label htmlFor={`${prefix}-mode`}>情绪方式</label>
        <select id={`${prefix}-mode`} disabled={locked} value={emotion.mode}
          onChange={event => { const mode = event.target.value as EmotionMode; onChange({ ...emotion, mode, reference: mode === 'reference' ? emotion.reference : '' }); }}>
          {Object.entries(available).map(([mode, ok]) => <option key={mode} value={mode} disabled={!ok}>
            {mode === 'none' ? '不控制情绪' : mode === 'reference' ? '情绪参考音频' : mode === 'text' ? '文本描述情绪' : '八维情绪强度'}{ok ? '' : (unsupported || []).includes(mode) ? '（这个引擎不吃这种情绪）' : '（本机未就绪）'}
          </option>)}</select></div>
      {emotion.mode === 'reference' && <>
        <fieldset className="sf-post-field sf-post-field--wide" disabled={locked}>
          <MaterialSelect label="情绪参考音频" kind="audio" value={emotion.reference} options={options} onChange={value => onChange({ ...emotion, reference: value })} onBrowse={onBrowse} />
        </fieldset>
        <div className="sf-post-field"><label htmlFor={`${prefix}-alpha`}>情绪强度（0–1）</label>
          <input id={`${prefix}-alpha`} type="number" min={0} max={1} step="any" disabled={locked} value={emotion.alpha}
            onChange={event => onChange({ ...emotion, alpha: Number(event.target.value) })} /></div>
      </>}
      {emotion.mode === 'text' && <div className="sf-post-field sf-post-field--wide"><label htmlFor={`${prefix}-text`}>情绪描述</label>
        <textarea id={`${prefix}-text`} rows={2} maxLength={200} disabled={locked} value={emotion.text} placeholder="例如：克制的悲伤，压着嗓子说" onChange={event => onChange({ ...emotion, text: event.target.value })} /></div>}
      {emotion.mode === 'vector' && emotionKeys.map((key, index) => <div className="sf-post-field" key={key}>
        <label htmlFor={`${prefix}-${key}`}>{emotionLabels[index]}（{key}）</label>
        <input id={`${prefix}-${key}`} type="number" min={0} max={1} step="any" disabled={locked} value={emotion.vector[index]}
          onChange={event => { const vector = [...emotion.vector]; vector[index] = Number(event.target.value); onChange({ ...emotion, vector }); }} /></div>)}
    </div>
    <p className="sf-post-hint">{note}</p>
  </div>;
}
