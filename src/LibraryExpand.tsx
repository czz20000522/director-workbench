import { useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { Maximize2, Minimize2, X } from 'lucide-react';
import './library-expand.css';

// 库面板的二级页面外壳：小面板照旧贴在首页左右栏，点展开后变成一张居中的大页面，
// 里面用左侧竖导航切换功能区。和提示词库 rl-panel.is-expanded 是同一套层叠思路。
export type LibrarySection = {
  id: string;
  label: string;
  icon?: ReactNode;
  note?: string;
  content: ReactNode;
};

export default function LibraryExpand({ title, subtitle, icon, compact, sections, initialSection }: {
  title: string; subtitle: string; icon: ReactNode; compact: ReactNode;
  sections: LibrarySection[]; initialSection?: string;
}) {
  const [expanded, setExpanded] = useState(false);
  const [active, setActive] = useState(initialSection || sections[0]?.id || '');

  useEffect(() => {
    if (!expanded) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      event.preventDefault();
      event.stopPropagation();
      setExpanded(false);
    };
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  }, [expanded]);

  const current = sections.find(item => item.id === active) || sections[0];
  const content = <>
    {expanded && <div className="lex-backdrop" role="presentation" onClick={() => setExpanded(false)} />}
    <section className={`pl-panel lex-panel${expanded ? ' is-expanded' : ''}`} aria-label={title}>
      <header className="pl-head lex-head">
        <div>
          <h2>{title}</h2>
          <p>{expanded && current?.note ? current.note : subtitle}</p>
        </div>
        <div className="lex-head-actions">
          <span className="pl-icon">{icon}</span>
          <button type="button" className="lex-icon-button"
            aria-label={expanded ? `收起${title}` : `展开${title}`}
            title={expanded ? '收起' : '展开成独立页面'}
            onClick={() => setExpanded(value => !value)}>
            {expanded ? <Minimize2 size={14} /> : <Maximize2 size={14} />}
          </button>
          {expanded && <button type="button" className="lex-icon-button" aria-label="关闭" title="关闭" onClick={() => setExpanded(false)}><X size={15} /></button>}
        </div>
      </header>
      {expanded
        ? <div className="lex-split">
            <nav className="lex-nav" aria-label={`${title}功能区`}>
              {sections.map(item => <button key={item.id} type="button" aria-pressed={item.id === current?.id}
                onClick={() => setActive(item.id)}>{item.icon}{item.label}</button>)}
            </nav>
            <div className="lex-main">{current?.content}</div>
          </div>
        : compact}
    </section>
  </>;

  // 展开时挂到 .sf-root：留在左右栏里会被父级的层叠上下文和固定行高关住。
  if (expanded && typeof document !== 'undefined') {
    const host = document.querySelector('.sf-root') ?? document.body;
    return createPortal(content, host);
  }
  return content;
}
