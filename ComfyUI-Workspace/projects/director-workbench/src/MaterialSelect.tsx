import { useId } from 'react';

export type MaterialOption = { value: string; label: string; kind: 'image' | 'audio' | 'video' };

export default function MaterialSelect({ label, kind, value, options, onChange, onBrowse }: {
  label: string; kind: MaterialOption['kind']; value: string; options: MaterialOption[];
  onChange: (value: string) => void; onBrowse: () => void;
}) {
  const id = useId();
  const available = options.filter(option => option.kind === kind);
  const existing = value && !available.some(option => option.value === value);
  return <div className="director-material-select wide">
    <label htmlFor={id}>{label}</label>
    <select id={id} value={value} onChange={event => onChange(event.target.value)}>
      <option value="">暂不选择</option>
      {existing && <option value={value}>已保存素材 · {value.split(/[\\/]/).pop()}</option>}
      {available.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}
    </select>
    <button type="button" className="button secondary compact" onClick={onBrowse}>添加素材</button>
    {value && kind === 'image' && <img className="director-material-preview" src={`/media-file?path=${encodeURIComponent(value)}`} alt={`${label}预览`} loading="lazy" />}
    {value && kind === 'audio' && <audio aria-label={`${label}试听`} src={`/media-file?path=${encodeURIComponent(value)}`} controls preload="none" />}
    <details><summary>高级：文件路径</summary><input aria-label={`${label}文件路径`} value={value} onChange={event => onChange(event.target.value)} /></details>
  </div>;
}
