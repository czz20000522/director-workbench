import { useEffect, useState } from 'react';
import { ApiError, request } from './api/client';

type Entry = { name: string; params: Record<string, unknown>; saved_at?: number };
type Kind = 'music' | 'dubbing';
const kindLabels: Record<Kind, string> = { music: '配乐设定', dubbing: '配音设定' };

// A named setting is only ever applied when the user clicks 载入; nothing here fills a form by itself.
export default function CreativeSettingsBar({ kind, values, onApply }: {
  kind: Kind; values: Record<string, unknown>; onApply: (params: Record<string, unknown>) => void;
}) {
  const [entries, setEntries] = useState<Entry[]>([]);
  const [selected, setSelected] = useState('');
  const [name, setName] = useState('');
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);
  async function refresh(): Promise<Entry[]> {
    const data = await request<{ settings: Record<Kind, Entry[]> }>('/api/creative-settings');
    const list = data.settings[kind] || [];
    setEntries(list);
    setSelected(current => list.some(item => item.name === current) ? current : '');
    return list;
  }
  useEffect(() => {
    let active = true;
    request<{ settings: Record<Kind, Entry[]> }>('/api/creative-settings')
      .then(data => { if (active) setEntries(data.settings[kind] || []); })
      .catch(reason => { if (active) setMessage(reason instanceof ApiError ? `无法读取${kindLabels[kind]}：${reason.message}` : `无法读取${kindLabels[kind]}`); });
    return () => { active = false; };
  }, [kind]);
  async function act(action: 'load' | 'save' | 'delete') {
    if (busy) return;
    setBusy(true); setMessage('');
    try {
      if (action === 'load') {
        const entry = entries.find(item => item.name === selected);
        if (!entry) throw new Error('先选择一条设定');
        onApply(entry.params);
        setMessage(`已载入「${entry.name}」的参数。`);
      } else if (action === 'save') {
        const label = name.trim();
        if (!label) throw new Error('请先填写设定名称');
        await request(`/api/creative-settings/${kind}/${encodeURIComponent(label)}`, { json: { params: values } });
        const list = await refresh();
        setSelected(label);
        setName('');
        setMessage(`已保存「${label}」，共 ${list.length} 条${kindLabels[kind]}。`);
      } else {
        if (!selected) throw new Error('先选择要删除的设定');
        await request(`/api/creative-settings/${kind}/${encodeURIComponent(selected)}`, { init: { method: 'DELETE' } });
        await refresh();
        setMessage(`已删除「${selected}」。`);
      }
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : `${kindLabels[kind]}操作未完成`);
    } finally { setBusy(false); }
  }
  return <div className="sf-post-subblock">
    <div className="sf-post-section-head"><h3 className="sf-post-subtitle">{kindLabels[kind]}</h3><p className="sf-post-meta">{entries.length} 条 · 只在你点击时载入</p></div>
    <div className="sf-post-fields">
      <div className="sf-post-field"><label htmlFor={`settings-${kind}-pick`}>已保存的设定</label>
        <select id={`settings-${kind}-pick`} value={selected} onChange={event => setSelected(event.target.value)}>
          <option value="">选择设定</option>{entries.map(entry => <option key={entry.name} value={entry.name}>{entry.name}</option>)}</select></div>
      <div className="sf-post-field"><label htmlFor={`settings-${kind}-name`}>把当前参数存为</label>
        <input id={`settings-${kind}-name`} maxLength={40} value={name} placeholder="例如：夜景追逐 88拍" onChange={event => setName(event.target.value)} /></div>
    </div>
    <div className="sf-post-actions">
      <button className="button secondary compact" type="button" disabled={busy || !selected} onClick={() => void act('load')}>载入设定</button>
      <button className="button secondary compact" type="button" disabled={busy || !name.trim()} onClick={() => void act('save')}>保存为设定</button>
      <button className="button secondary compact" type="button" disabled={busy || !selected} onClick={() => void act('delete')}>删除设定</button>
    </div>
    {message && <p className="sf-post-message" role="status" aria-live="polite">{message}</p>}
  </div>;
}
