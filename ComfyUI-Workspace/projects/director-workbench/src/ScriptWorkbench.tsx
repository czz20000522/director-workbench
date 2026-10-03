import { useEffect, useRef, useState } from 'react';
import { BookOpen, FileUp, Pencil, Plus, Save, Trash2 } from 'lucide-react';
import './script-workbench.css';

type Scene = { id: string; title: string; text?: string | null; duration_seconds?: number | null };
type Screenplay = { project_id: string; title: string; text: string; target_duration_seconds?: number | null; scenes: Scene[]; revision: number; status: string };
type Props = {
  projectId: string;
  shots: Array<{ id: string; script_scene_id?: string }>;
  request: (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;
  onCreateShot: (sceneId: string, scriptRevision: number) => void;
  onSelectShot: (shotId: string) => void;
};
// Keep edits across workspace tabs, without persisting private screenplay content in browser storage.
const drafts = new Map<string, { base: Screenplay; value: Screenplay }>();
const fingerprint = (value: Screenplay | null) => JSON.stringify(value);
async function responseBody(response: Response) {
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = typeof body.detail === 'string' ? body.detail : Array.isArray(body.detail)
      ? `请检查以下字段：${body.detail.map((item: { loc?: Array<string | number> }) => (item.loc ?? []).filter(part => part !== 'body').join('.')).slice(0, 5).join('、')}`
      : body.detail?.message;
    throw new Error(response.status === 409 ? `${detail || '剧本已被其他操作更新。'} 你的编辑仍保留，可查看最新版本。` : detail || `请求失败（${response.status}），你的编辑仍保留。`);
  }
  return body as Screenplay;
}

export default function ScriptWorkbench({ projectId, shots, request, onCreateShot, onSelectShot }: Props) {
  const [saved, setSaved] = useState<Screenplay | null>(null);
  const [draft, setDraft] = useState<Screenplay | null>(null);
  const [editing, setEditing] = useState(false);
  const [selected, setSelected] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [reload, setReload] = useState(0);
  const [latest, setLatest] = useState<Screenplay | null>(null);
  const root = useRef<HTMLElement>(null);
  const file = useRef<HTMLInputElement>(null);
  const epoch = useRef(0);
  const requestRef = useRef(request);
  requestRef.current = request;
  const dirty = !!draft && fingerprint(draft) !== fingerprint(saved);
  const current = editing ? draft : saved;
  const activeScene = current?.scenes.find(scene => scene.id === selected);

  useEffect(() => {
    const generation = ++epoch.current;
    const controller = new AbortController();
    setSaved(null); setDraft(null); setLatest(null); setError(''); setNotice(''); setBusy(true); setSelected('');
    requestRef.current(`/api/projects/${encodeURIComponent(projectId)}/script`, { signal: controller.signal })
      .then(responseBody).then(value => {
        if (generation !== epoch.current) return;
        const cached = drafts.get(projectId);
        if (cached) {
          setSaved(cached.base); setDraft(cached.value); setEditing(true);
          setNotice('已恢复此作品尚未保存的编辑。');
          if (value.revision !== cached.base.revision) setLatest(value);
        } else { setSaved(value); setDraft(value); setEditing(false); }
      }).catch(reason => { if (generation === epoch.current && !controller.signal.aborted) setError(String(reason.message || reason)); })
      .finally(() => { if (generation === epoch.current) setBusy(false); });
    return () => { controller.abort(); ++epoch.current; };
  }, [projectId, reload]);

  useEffect(() => {
    if (!saved || !draft || saved.project_id !== projectId || draft.project_id !== projectId) return;
    if (dirty) drafts.set(projectId, { base: saved, value: draft });
    else drafts.delete(projectId);
  }, [projectId, saved, draft, dirty]);

  useEffect(() => {
    if (!dirty) return;
    const unload = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ''; };
    const leave = (event: MouseEvent) => {
      const target = event.target instanceof Element ? event.target.closest('button, a') : null;
      if (!target || root.current?.contains(target)) return;
      if (!window.confirm('剧本有尚未保存的修改。离开后草稿仅在本次打开的工作台中保留，仍要离开吗？')) {
        event.preventDefault(); event.stopPropagation(); event.stopImmediatePropagation();
      }
    };
    window.addEventListener('beforeunload', unload);
    document.addEventListener('click', leave, true);
    return () => { window.removeEventListener('beforeunload', unload); document.removeEventListener('click', leave, true); };
  }, [dirty]);

  const update = (patch: Partial<Screenplay>) => setDraft(value => value ? { ...value, ...patch } : value);
  const changeScene = (id: string, patch: Partial<Scene>) => setDraft(value => value ? { ...value, scenes: value.scenes.map(scene => scene.id === id ? { ...scene, ...patch } : scene) } : value);
  const save = async () => {
    if (!draft || !saved || busy) return;
    if (!draft.title.trim() || draft.scenes.some(scene => !scene.title.trim())) { setError('请填写剧本标题和每个场次的名称。'); return; }
    if ([draft.target_duration_seconds, ...draft.scenes.map(scene => scene.duration_seconds)].some(value => value != null && (!Number.isFinite(value) || value <= 0 || value > 86400))) { setError('预计时长应大于 0 且不超过 86400 秒，也可以留空。'); return; }
    const generation = epoch.current;
    setBusy(true); setError(''); setNotice('');
    try {
      const response = await requestRef.current(`/api/projects/${encodeURIComponent(projectId)}/script`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...draft, title: draft.title.trim(), scenes: draft.scenes.map(scene => ({ ...scene, title: scene.title.trim() })), expected_revision: saved.revision }),
      });
      const value = await responseBody(response);
      if (generation !== epoch.current) return;
      drafts.delete(projectId); setSaved(value); setDraft(value); setEditing(false); setLatest(null); setNotice('剧本与场次已保存。');
    } catch (reason) { if (generation === epoch.current) setError(reason instanceof Error ? reason.message : '保存失败，你的编辑仍保留。'); }
    finally { if (generation === epoch.current) setBusy(false); }
  };
  const inspectLatest = async () => {
    const generation = epoch.current;
    setBusy(true);
    try {
      const value = await responseBody(await requestRef.current(`/api/projects/${encodeURIComponent(projectId)}/script`));
      if (generation === epoch.current) setLatest(value);
    } catch (reason) { if (generation === epoch.current) setError(reason instanceof Error ? reason.message : '读取失败'); }
    finally { if (generation === epoch.current) setBusy(false); }
  };
  const importFile = async (input: File | undefined) => {
    if (!input || !draft) return;
    if (!/\.(txt|md)$/i.test(input.name) || input.size > 1024 * 1024) { setError('请选择不超过 1 MB 的 UTF-8 TXT 或 Markdown 文件。'); return; }
    if (draft.text.trim() && !window.confirm('用文件内容替换当前剧本文字？已有场次保持不变，保存后才会写入作品。')) return;
    const generation = epoch.current;
    try {
      const text = await input.text();
      if (generation !== epoch.current) return;
      update({ text }); setEditing(true); setSelected(''); setError(''); setNotice(`已读取 ${input.name}，点击保存后写入作品。`);
    } catch { if (generation === epoch.current) setError('无法读取文件，请重新选择。'); }
  };
  const discard = () => {
    if (dirty && !window.confirm('放弃尚未保存的剧本与场次修改？')) return;
    drafts.delete(projectId); setDraft(saved); setEditing(false); setError(''); setNotice('');
  };

  return <section ref={root} className="script-workbench" aria-label="剧本与场次">
    <header className="script-header">
      <div><h2><BookOpen size={20} />剧本与场次</h2><p>先读故事，再为每一场安排分镜。</p></div>
      <div className="script-actions">
        {current && <span className="script-meta">{dirty ? '未保存' : `版本 ${saved?.revision ?? 0}`}</span>}
        <input ref={file} type="file" accept=".txt,.md,text/plain,text/markdown" hidden onChange={event => { void importFile(event.target.files?.[0]); event.target.value = ''; }} />
        <button className="button secondary compact" disabled={busy || !draft} onClick={() => file.current?.click()}><FileUp size={14} />导入 TXT / MD</button>
        {editing ? <><button className="button secondary compact" disabled={busy} onClick={discard}>取消编辑</button><button className="button primary compact" disabled={busy || !dirty} onClick={() => void save()}><Save size={14} />保存</button></> : <button className="button primary compact" disabled={busy || !saved} onClick={() => setEditing(true)}><Pencil size={14} />编辑剧本</button>}
      </div>
    </header>
    {error && <div className="script-message script-error" role="alert"><span>{error}</span><button className="text-button" disabled={busy} onClick={() => saved ? void inspectLatest() : setReload(value => value + 1)}>{saved ? '查看最新版本' : '重新读取'}</button></div>}
    {notice && <p className="script-message" role="status">{notice}</p>}
    {latest && <section className="script-latest" aria-label="服务器最新剧本"><h3>最新版本 {latest.revision} · {latest.title}</h3><p>下方只读预览不会覆盖你的编辑。采用后会放弃当前未保存修改。</p><details><summary>展开最新正文与场次</summary><div className="script-prose">{latest.text || '暂无正文'}</div><ul>{latest.scenes.map(scene => <li key={scene.id}>{scene.id} · {scene.title}{scene.duration_seconds != null ? ` · ${scene.duration_seconds} 秒` : ''}{scene.text && <p className="script-prose">{scene.text}</p>}</li>)}</ul></details><button className="button secondary compact" disabled={busy} onClick={() => { if (dirty && !window.confirm('放弃当前未保存修改，采用服务器最新版本？')) return; drafts.delete(projectId); setSaved(latest); setDraft(latest); setLatest(null); setEditing(false); setError(''); setNotice('已采用最新版本。'); setSelected(''); }}>采用最新版本</button></section>}
    {!current ? <p className="script-empty">{busy ? '正在读取剧本…' : '暂时无法读取剧本。'}</p> : <div className="script-layout">
      <aside className="script-scenes" aria-label="场次目录">
        <button className={`script-scene-item ${!selected ? 'active' : ''}`} aria-pressed={!selected} onClick={() => setSelected('')}><strong>完整剧本</strong><small>{current.scenes.length} 场{current.target_duration_seconds ? ` · 预计 ${current.target_duration_seconds} 秒` : ''}</small></button>
        {current.scenes.map((scene, index) => <button key={scene.id} className={`script-scene-item ${selected === scene.id ? 'active' : ''}`} aria-pressed={selected === scene.id} onClick={() => setSelected(scene.id)}><span className="script-scene-order">{String(index + 1).padStart(2, '0')}</span><span><strong>{scene.title || '未命名场次'}</strong><small>{scene.duration_seconds != null ? `${scene.duration_seconds} 秒 · ` : ''}{shots.filter(shot => shot.script_scene_id === scene.id).length} 个分镜</small></span></button>)}
        {editing && <button className="button secondary compact script-add" disabled={busy} onClick={() => { const id = `SC-${crypto.randomUUID().slice(0, 8)}`; update({ scenes: [...current.scenes, { id, title: `第 ${current.scenes.length + 1} 场`, text: '' }] }); setSelected(id); }}><Plus size={14} />添加场次</button>}
        {!current.scenes.length && <p className="script-meta">编辑剧本时可添加场次，不会自动生成分镜。</p>}
      </aside>
      <main className="script-reading" aria-busy={busy}>
        {!activeScene ? <>
          {editing ? <fieldset disabled={busy} className="script-fields"><div className="script-fields-row"><label>剧本标题<input aria-label="剧本标题" maxLength={200} value={current.title} onChange={event => update({ title: event.target.value })} /></label><label>目标时长（秒，可选）<input aria-label="目标时长（秒，可选）" type="number" min="1" max="86400" value={current.target_duration_seconds ?? ''} onChange={event => update({ target_duration_seconds: event.target.value ? Number(event.target.value) : null })} /></label></div><label>剧本正文<textarea aria-label="剧本正文" className="script-text-editor" value={current.text} onChange={event => update({ text: event.target.value })} placeholder="粘贴已有剧本，或导入 TXT / Markdown 文件" /></label><p className="script-meta">完整正文与场次摘录分别维护。场次用于组织制作，分镜提示词在分镜中编写。</p></fieldset> : <><h3>{current.title || '尚未添加剧本'}</h3><div className="script-prose">{current.text || '点击“编辑剧本”粘贴文字，或导入已有文件。'}</div></>}
        </> : <>
          {editing ? <fieldset disabled={busy} className="script-fields"><div className="script-fields-row"><label>场次名称<input aria-label="场次名称" maxLength={200} value={activeScene.title} onChange={event => changeScene(activeScene.id, { title: event.target.value })} /></label><label>预计时长（秒，可选）<input aria-label="预计时长（秒，可选）" type="number" min="1" max="86400" value={activeScene.duration_seconds ?? ''} onChange={event => changeScene(activeScene.id, { duration_seconds: event.target.value ? Number(event.target.value) : null })} /></label></div><label>本场剧本（可选）<textarea aria-label="本场剧本（可选）" className="script-text-editor" value={activeScene.text ?? ''} onChange={event => changeScene(activeScene.id, { text: event.target.value })} placeholder="摘录本场的动作、对白与情绪；也可以只登记场次，保留全文阅读。" /></label><button className="button secondary compact script-remove" type="button" disabled={shots.some(shot => shot.script_scene_id === activeScene.id)} title={shots.some(shot => shot.script_scene_id === activeScene.id) ? '此场仍有关联分镜，暂不能删除' : undefined} onClick={() => { if (!window.confirm(`删除场次“${activeScene.title}”？完整剧本文字不会改变。`)) return; update({ scenes: current.scenes.filter(scene => scene.id !== activeScene.id) }); setSelected(''); }}><Trash2 size={14} />删除场次</button>{shots.some(shot => shot.script_scene_id === activeScene.id) && <p className="script-meta">此场仍有关联分镜，暂不能删除。</p>}</fieldset> : <><span className="script-meta">{activeScene.id}{activeScene.duration_seconds != null ? ` · 预计 ${activeScene.duration_seconds} 秒` : ''}</span><h3>{activeScene.title}</h3><div className="script-prose">{activeScene.text || '此场尚未单独摘录正文，可从左侧阅读完整剧本。'}</div></>}
          <section className="script-linked"><h4>本场分镜</h4><div className="script-actions">{shots.filter(shot => shot.script_scene_id === activeScene.id).map(shot => <button key={shot.id} className="button secondary compact" disabled={dirty || busy} onClick={() => onSelectShot(shot.id)}>{shot.id}</button>)}<button className="button secondary compact" disabled={dirty || busy || !saved?.scenes.some(scene => scene.id === activeScene.id)} onClick={() => saved && onCreateShot(activeScene.id, saved.revision)}><Plus size={14} />为本场添加分镜</button></div><p className="script-meta">{dirty ? '请先保存剧本与场次，再进入分镜制作。' : '一场可以包含多个分镜；每个分镜单独设置时长与提示词。'}</p></section>
        </>}
      </main>
    </div>}
  </section>;
}
