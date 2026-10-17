import { useEffect, useState } from 'react';

type Preset = { id: string; title: string; available: boolean; reason?: string; description?: string; verification?: string; missing_models: string[] };

export default function ProductionPresetPicker({ projectId, installed, onInstalled }: {
  projectId: string; installed?: string; onInstalled: () => Promise<void>;
}) {
  const [presets, setPresets] = useState<Preset[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => {
    const controller = new AbortController();
    fetch('/api/production-presets', { signal: controller.signal }).then(async response => {
      if (!response.ok) throw new Error('制作方案读取失败');
      setPresets((await response.json()).presets ?? []);
    }).catch(reason => { if (reason.name !== 'AbortError') setError(reason.message); });
    return () => controller.abort();
  }, [projectId]);
  async function install(id: string) {
    setBusy(true); setError('');
    try {
      const response = await fetch(`/api/projects/${encodeURIComponent(projectId)}/production-presets/${encodeURIComponent(id)}`, { method: 'POST' });
      const data = await response.json();
      if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '配置制作方案失败');
      await onInstalled();
    } catch (reason) { setError(reason instanceof Error ? reason.message : '配置失败'); }
    finally { setBusy(false); }
  }
  return <details className="director-preset-compact">
    <summary>首次生成设置 <span>{installed ? '已设置' : '选择生成方式'}</span></summary>
    {presets.map(preset => <section key={preset.id} className="director-inspector-body">
      <h3>{preset.title}</h3><p>{preset.description ?? preset.reason}</p>
      <p className="director-muted">{preset.verification}</p>
      {preset.missing_models.length > 0 && <details><summary>缺少 {preset.missing_models.length} 个模型，补齐后才能生成</summary><ul>{preset.missing_models.map(name => <li key={name}>{name}</li>)}</ul></details>}
      <button type="button" className="button primary compact" disabled={!preset.available || busy || installed === preset.id} onClick={() => void install(preset.id)}>{installed === preset.id ? '已选用' : busy ? '设置中…' : '选用'}</button>
    </section>)}
    {!presets.length && !error && <p>正在读取制作方案…</p>}
    {error && <p role="alert">{error}</p>}
  </details>;
}
