import { useEffect, useRef, useState } from 'react';
import './FirstUseGuide.css';

type GuideStep = { id: string; title: string; purpose: string; prepare: string; result: string; action: string; completed?: boolean; applicable?: boolean; state_label?: string };
type Guide = { status: 'active' | 'skipped'; project_id?: string | null; view_project_id?: string | null; route?: 'idea' | 'reference'; completed_steps: string[]; revision: number; steps?: GuideStep[] };

export default function FirstUseGuide({ projectId, refreshKey, onAction, onOpenProject }: {
  projectId?: string; refreshKey: number | string; onAction: (action: string, route?: 'idea' | 'reference') => void; onOpenProject: (id: string) => void;
}) {
  const [guide, setGuide] = useState<Guide | null>(null);
  const [expanded, setExpanded] = useState(true);
  const [stepId, setStepId] = useState('');
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);
  const activeProject = useRef(projectId);
  activeProject.current = projectId;
  useEffect(() => {
    const controller = new AbortController();
    fetch(`/api/settings/guide${projectId ? `?project_id=${encodeURIComponent(projectId)}` : ''}`, { signal: controller.signal })
      .then(async response => { if (!response.ok) throw new Error('使用指引暂时无法读取，请稍后重试。'); return response.json(); })
      .then(data => { if (!controller.signal.aborted && Array.isArray(data.completed_steps) && ['active', 'skipped'].includes(data.status)) { setGuide(data); setError(''); } })
      .catch(reason => { if (!controller.signal.aborted) setError(String(reason.message)); });
    return () => controller.abort();
  }, [projectId, refreshKey]);
  async function save(changes: Partial<Guide>) {
    if (!guide) return;
    setSaving(true); setError('');
    try {
      const response = await fetch('/api/settings/guide', { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ...changes, expected_revision: guide.revision }) });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail?.message || '指引进度保存失败，请刷新后重试。');
      if (activeProject.current !== projectId) return;
      if (projectId && data.view_project_id !== projectId) {
        const latest = await fetch(`/api/settings/guide?project_id=${encodeURIComponent(projectId)}`);
        if (!latest.ok) throw new Error('指引进度已保存，请刷新以读取当前作品状态。');
        const current = await latest.json();
        if (activeProject.current === projectId) setGuide(current);
      } else setGuide(data);
    } catch (reason) { setError(reason instanceof Error ? reason.message : '指引进度保存失败。'); }
    finally { setSaving(false); }
  }
  const steps = (guide?.steps ?? []).filter(step => step.applicable !== false);
  const understood = (id: string) => Boolean(guide?.completed_steps.includes(id));
  const selected = steps.find(step => step.id === stepId) ?? steps.find(step => !step.completed && !understood(step.id)) ?? steps[0];
  if (!guide) return error ? <aside className="first-use-guide"><p role="alert">{error}</p></aside> : null;
  if (guide.status === 'skipped') return <aside className="first-use-guide is-collapsed"><button className="text-button" disabled={saving} onClick={() => { setExpanded(true); void save({ status: 'active' }); }}>重新打开使用指引</button>{error && <p role="alert">{error}</p>}</aside>;
  return <aside className="first-use-guide" aria-label="首次使用指引">
    <div className="first-use-guide-heading"><button className="text-button" aria-expanded={expanded} onClick={() => setExpanded(value => !value)}><strong>创作指引</strong><span>已了解 {steps.filter(step => understood(step.id)).length}/{steps.length} 步 · 已完成 {steps.filter(step => step.completed).length} 步</span></button><button className="text-button" disabled={saving} onClick={() => void save({ status: 'skipped' })}>跳过指引</button></div>
    {expanded && <>
      <div className="first-use-guide-route" role="group" aria-label="指引创作路径"><button aria-pressed={guide.route !== 'reference'} className="button secondary compact" disabled={saving} onClick={() => void save({ route: 'idea' })}>从想法创作</button><button aria-pressed={guide.route === 'reference'} className="button secondary compact" disabled={saving} onClick={() => void save({ route: 'reference' })}>从参考开始</button></div>
      {guide.project_id && guide.project_id !== projectId && <p>指引上次使用另一部作品。<button className="text-button" onClick={() => onOpenProject(guide.project_id!)}>继续上次作品</button></p>}
      {projectId && guide.project_id !== projectId && <button className="text-button" disabled={saving} onClick={() => void save({ project_id: projectId })}>在当前作品继续指引</button>}
      <div className="first-use-guide-content"><nav aria-label="指引步骤">{steps.map((step, index) => <button key={step.id} aria-current={selected?.id === step.id ? 'step' : undefined} onClick={() => setStepId(step.id)}><span>{step.completed ? '✓' : index + 1}</span>{step.title}{understood(step.id) && !step.completed && <small>已了解</small>}</button>)}</nav>
        {selected && <section aria-label={`指引：${selected.title}`}><h3>{selected.title}</h3><p>{selected.purpose}</p>{selected.state_label && <p role="status">{selected.state_label}</p>}<dl><div><dt>准备</dt><dd>{selected.prepare}</dd></div><div><dt>完成后</dt><dd>{selected.result}</dd></div></dl><div className="first-use-guide-actions"><button className="button primary compact" onClick={() => onAction(selected.action, guide.route)}>打开这一步</button><button className="button secondary compact" disabled={saving || understood(selected.id)} onClick={() => { void save({ completed_steps: [...guide.completed_steps, selected.id] }); setStepId(steps[steps.indexOf(selected) + 1]?.id ?? selected.id); }}>{understood(selected.id) ? '已了解' : '我已了解这一步'}</button></div><small>“已了解”只保存阅读进度；作品是否准备好以服务器预检和审核结果为准。浏览指引不会生成素材。</small></section>}
      </div>
    </>}
    {error && <p role="alert">{error}</p>}
  </aside>;
}
