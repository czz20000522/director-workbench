export type OutputResolution = 'native' | '2k' | '4k' | '8k';
export type AspectRatio = '16:9' | '9:16' | '1:1';

export default function ResolutionPicker({ label, aspect, resolution = 'native', nativeSize, disabled = false, onChange }: {
  label: string; aspect: AspectRatio; resolution?: OutputResolution; nativeSize: string; disabled?: boolean;
  onChange: (aspect: AspectRatio, resolution: OutputResolution) => void;
}) {
  function size(value: OutputResolution) {
    if (value === 'native') return nativeSize;
    const edge = { '2k': 2048, '4k': 3840, '8k': 7680 }[value];
    const short = edge * 9 / 16;
    return aspect === '1:1' ? `${edge} × ${edge}` : aspect === '16:9' ? `${edge} × ${short}` : `${short} × ${edge}`;
  }
  return <details className="resolution-picker">
    <summary>{label}规格 · {aspect} · {resolution === 'native' ? '1K · 原生' : resolution.toUpperCase()} <small>{size(resolution)}</small></summary>
    <div className="resolution-levels">
      <label>1 · 画幅比例<select aria-label={`${label}画幅`} disabled={disabled} value={aspect} onChange={event => onChange(event.target.value as AspectRatio, resolution)}>
        <option value="16:9">横屏 · 16:9</option><option value="9:16">竖屏 · 9:16</option><option value="1:1">方形 · 1:1</option>
      </select></label>
      <label>2 · 输出分辨率<select aria-label={`${label}输出分辨率`} disabled={disabled} value={resolution} onChange={event => onChange(aspect, event.target.value as OutputResolution)}>
        {(['native', '2k', '4k', '8k'] as OutputResolution[]).map(value => <option key={value} value={value}>{value === 'native' ? '1K · 原生生成' : `${value.toUpperCase()} · 尺寸放大`} · {size(value)}</option>)}
      </select></label>
    </div>
    <p>先选比例，再选像素。2K / 4K / 8K 在生成后放大输出，保留原始文件；不增加模型原生细节。比例有细微差异时居中裁切。</p>
    {resolution === '8k' && <p>8K 输出文件更大，处理与下载更慢。</p>}
  </details>;
}
