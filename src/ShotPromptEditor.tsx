import { useMemo, useRef, useState } from 'react';

type Props = { value: string; onChange: (value: string) => void; savedValue: string };

export default function ShotPromptEditor({ value, onChange, savedValue }: Props) {
  const editor = useRef<HTMLTextAreaElement>(null);
  const [query, setQuery] = useState('');
  const [matchIndex, setMatchIndex] = useState(0);
  const matches = useMemo(() => {
    if (!query) return [];
    const result: number[] = [];
    const text = value.toLocaleLowerCase();
    const needle = query.toLocaleLowerCase();
    let offset = 0;
    while (offset < text.length) {
      const found = text.indexOf(needle, offset);
      if (found < 0) break;
      result.push(found);
      offset = found + Math.max(needle.length, 1);
    }
    return result;
  }, [value, query]);

  function jump(direction: number) {
    if (!matches.length) return;
    const next = (matchIndex + direction + matches.length) % matches.length;
    setMatchIndex(next);
    editor.current?.focus();
    editor.current?.setSelectionRange(matches[next], matches[next] + query.length);
  }

  function insertHeading(heading: string) {
    const field = editor.current;
    const start = field?.selectionStart ?? value.length;
    const end = field?.selectionEnd ?? start;
    const insertion = `${start > 0 && value[start - 1] !== '\n' ? '\n' : ''}${heading}\n`;
    onChange(value.slice(0, start) + insertion + value.slice(end));
    requestAnimationFrame(() => {
      field?.focus();
      field?.setSelectionRange(start + insertion.length, start + insertion.length);
    });
  }

  return <div className="shot-prompt-editor">
    <div className="shot-prompt-tools"><label>查找提示词<input value={query} onChange={event => { setQuery(event.target.value); setMatchIndex(0); }} placeholder="查找角色、场景或台词" /></label><span aria-live="polite">{query ? `${matches.length ? matchIndex + 1 : 0} / ${matches.length}` : '输入关键词查找'}</span><button type="button" className="button secondary compact" disabled={!matches.length} onClick={() => jump(-1)}>上一个</button><button type="button" className="button secondary compact" disabled={!matches.length} onClick={() => jump(1)}>下一个</button></div>
    <label className="shot-prompt-label" htmlFor="current-shot-prompt">当前分镜提示词</label>
    <textarea id="current-shot-prompt" ref={editor} value={value} onChange={event => onChange(event.target.value)} placeholder="写下画面、角色动作、台词、声音情绪和限制。修改后保存，再重新生成。" spellCheck={false} />
    <div className="shot-prompt-footer"><div className="shot-prompt-headings"><span>插入小标题</span>{['画面', '动作', '角色台词', '声音与情绪', '限制'].map(title => <button key={title} type="button" onClick={() => insertHeading(`【${title}】`)}>{title}</button>)}</div><span>{value.length.toLocaleString('zh-CN')} 字 · {value !== savedValue ? '有未保存修改' : '已保存'}</span></div>
  </div>;
}
