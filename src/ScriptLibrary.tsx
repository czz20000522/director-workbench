import { useEffect, useState } from 'react';
import { Download, FilePlus2, FolderOpen, LoaderCircle, Save, Trash2, Upload } from 'lucide-react';
import { ApiError, mediaUrl, request } from './api/client';

// 剧本库：草稿只在本机；归档才写进 素材库\剧本。导出由浏览器直接下载正文。
type Draft = { id: string; title: string; content?: string; updated_at?: number };
type Archived = { name: string; path: string; size: number; modified: number };
type Listing = { drafts: Draft[]; archived: Archived[] };

export function ScriptLibraryEntry({ onOpen }: { onOpen: () => void }) {
  const [count, setCount] = useState<number | null>(null);
  useEffect(() => {
    let active = true;
    request<Listing>('/api/script-library')
      .then(data => { if (active) setCount((data.drafts || []).length); })
      .catch(() => { if (active) setCount(null); });
    return () => { active = false; };
  }, []);
  return <section className="pl-panel sl-entry" aria-label="剧本库">
    <header className="pl-head"><div><h2>剧本库</h2><p>草稿在本机 · 归档进素材库\剧本</p></div><span className="pl-icon"><FolderOpen size={16} /></span></header>
    <div className="pl-card-actions" style={{ padding: '0 12px 12px' }}>
      <small>{count === null ? '正在读取…' : `${count} 份草稿`}</small>
      <button className="sf-ghost" type="button" onClick={onOpen}>打开剧本库</button>
    </div>
  </section>;
}

export default function ScriptLibraryDialog({ onClose, onChanged }: { onClose: () => void; onChanged?: () => void }) {
  const [listing, setListing] = useState<Listing | null>(null);
  const [tab, setTab] = useState<'drafts' | 'archived'>('drafts');
  const [activeId, setActiveId] = useState('');
  const [title, setTitle] = useState('');
  const [content, setContent] = useState('');
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');

  async function refresh(keep?: string): Promise<Listing | null> {
    try {
      const data = await request<Listing>('/api/script-library');
      setListing(data);
      const drafts = data.drafts || [];
      const next = keep && drafts.some(item => item.id === keep) ? keep : (drafts[0]?.id || '');
      setActiveId(next);
      const active = drafts.find(item => item.id === next);
      setTitle(active?.title || '');
      setContent(active?.content || '');
      return data;
    } catch (reason) {
      setMessage(reason instanceof ApiError ? reason.message : '无法读取剧本库');
      return null;
    }
  }
  useEffect(() => { void refresh(); }, []);

  function selectDraft(draft: Draft) {
    setActiveId(draft.id); setTitle(draft.title); setContent(draft.content || ''); setMessage('');
  }
  function newDraft() {
    setActiveId(''); setTitle(''); setContent(''); setMessage('新草稿：写好标题后点「保存草稿」。');
  }
  async function save() {
    if (busy) return;
    if (!title.trim()) { setMessage('先写一个标题。'); return; }
    setBusy(true); setMessage('');
    try {
      const body = { title: title.trim(), content };
      const data = activeId
        ? await request<{ draft: Draft }>(`/api/script-library/drafts/${activeId}`, { init: { method: 'PUT' }, json: body })
        : await request<{ draft: Draft }>('/api/script-library/drafts', { init: { method: 'POST' }, json: body });
      await refresh(data.draft.id);
      setMessage('草稿已保存（还不会进素材库）。');
    } catch (reason) { setMessage(reason instanceof ApiError ? reason.message : '保存未完成'); }
    finally { setBusy(false); }
  }
  async function archive() {
    if (busy || !activeId) { setMessage('先选一份草稿再归档。'); return; }
    setBusy(true); setMessage('');
    try {
      const data = await request<{ name: string; path: string }>(`/api/script-library/drafts/${activeId}/archive`, { init: { method: 'POST' } });
      await refresh();
      setMessage(`已归档：${data.path}`);
      onChanged?.();
    } catch (reason) { setMessage(reason instanceof ApiError ? reason.message : '归档未完成'); }
    finally { setBusy(false); }
  }
  async function remove() {
    if (busy || !activeId) return;
    setBusy(true); setMessage('');
    try {
      await request(`/api/script-library/drafts/${activeId}`, { init: { method: 'DELETE' } });
      await refresh();
      setMessage('草稿已删除。');
    } catch (reason) { setMessage(reason instanceof ApiError ? reason.message : '删除未完成'); }
    finally { setBusy(false); }
  }
  function exportDraft() {
    if (!title.trim()) { setMessage('先写标题再导出。'); return; }
    const blob = new Blob([content], { type: 'text/markdown;charset=utf-8' });
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = `${title.trim().slice(0, 40)}.md`;
    link.click();
    URL.revokeObjectURL(link.href);
    setMessage('已导出为 .md（草稿仍留在剧本库）。');
  }
  async function importFile(file: File) {
    try {
      const text = await file.text();
      const label = file.name.replace(/\.[^.]+$/, '').slice(0, 80) || '导入的剧本';
      const data = await request<{ draft: Draft }>('/api/script-library/drafts', { init: { method: 'POST' }, json: { title: label, content: text } });
      await refresh(data.draft.id);
      setMessage(`已导入「${file.name}」作为草稿；归档后才会进素材库。`);
    } catch (reason) { setMessage(reason instanceof ApiError ? reason.message : '导入未完成'); }
  }

  return <div className="sf-modal-mask" role="dialog" aria-modal="true" aria-label="剧本库">
    <div className="sf-modal sl-modal">
      <div className="sf-post-section-head"><h2>剧本库</h2><p className="sf-post-meta">草稿只在本机 · 归档才进 素材库\剧本</p></div>
      <div className="sl-tabs"><button className={tab === 'drafts' ? 'selected' : ''} onClick={() => setTab('drafts')}>草稿（{listing?.drafts.length ?? 0}）</button><button className={tab === 'archived' ? 'selected' : ''} onClick={() => setTab('archived')}>已归档（{listing?.archived.length ?? 0}）</button></div>
      {tab === 'drafts' ? <div className="sl-grid">
        <div className="sl-list">
          {!listing ? <p className="sf-post-hint"><LoaderCircle size={14} className="sf-spin" /> 正在读取…</p> : listing.drafts.length === 0 ? <p className="sf-post-hint">还没有草稿。</p> : listing.drafts.map(draft => <button key={draft.id} className={`sl-item${draft.id === activeId ? ' selected' : ''}`} type="button" onClick={() => selectDraft(draft)}>
            <strong>{draft.title}</strong><small>{draft.updated_at ? new Date(draft.updated_at * 1000).toLocaleString('zh-CN') : ''}</small>
          </button>)}
          <button className="sf-ghost sl-new" type="button" onClick={newDraft}><FilePlus2 size={13} /> 新草稿</button>
        </div>
        <div className="sl-editor">
          <label className="sf-field"><span>标题</span><input value={title} maxLength={80} onChange={event => setTitle(event.target.value)} placeholder="例如：第一场 雨夜独白" /></label>
          <label className="sf-field"><span>正文</span><textarea rows={12} value={content} onChange={event => setContent(event.target.value)} placeholder="先写下来，不用讲究格式。归档前它只在这台机器上。" /></label>
          <div className="sf-post-actions">
            <button className="button primary compact" type="button" disabled={busy} onClick={() => void save()}><Save size={14} /> 保存草稿</button>
            <button className="button secondary compact" type="button" disabled={busy || !activeId} onClick={() => void archive()}><FolderOpen size={14} /> 归档到剧本库</button>
            <button className="button secondary compact" type="button" disabled={!title.trim()} onClick={exportDraft}><Download size={14} /> 导出 .md</button>
            <label className="button secondary compact sf-file-button"><Upload size={14} /> 导入文件<input type="file" accept=".md,.txt,text/plain" hidden onChange={event => { const file = event.target.files?.[0]; if (file) void importFile(file); event.target.value = ''; }} /></label>
            <button className="button secondary compact" type="button" disabled={busy || !activeId} onClick={() => void remove()}><Trash2 size={14} /> 删除草稿</button>
          </div>
          {message && <p className="sf-post-message" role="status" aria-live="polite">{message}</p>}
        </div>
      </div> : <div className="sl-grid single">
        {!listing ? <p className="sf-post-hint">正在读取…</p> : listing.archived.length === 0 ? <p className="sf-post-hint">还没有归档的剧本。归档后会写进 素材库\剧本，文件名带日期。</p> : <ul className="sf-post-history">{listing.archived.map(item => <li key={item.path}>
          <a href={mediaUrl(item.path)} download>{item.name}</a>
          <time>{(item.size / 1024).toFixed(1)} KB · {new Date(item.modified * 1000).toLocaleString('zh-CN')}</time>
        </li>)}</ul>}
      </div>}
      <div className="sf-step-actions"><button className="sf-secondary" type="button" onClick={onClose}>关闭</button></div>
    </div>
  </div>;
}
