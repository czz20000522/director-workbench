import { useEffect, useState } from 'react';
import { Lock } from 'lucide-react';
import { request } from './api/client';
import './style-block.css';

type Block = { id: string; name: string; note: string };

const STORAGE_KEY = 'director-workbench:style-block:v1';
let cached: Block[] | null = null;

/** 选中的风格锁存在本机：下次打开还是同一块，不用每次重选。 */
export function readStyleBlock(): string {
  try { return localStorage.getItem(STORAGE_KEY) || ''; } catch { return ''; }
}

function remember(id: string) {
  try { localStorage.setItem(STORAGE_KEY, id); } catch { /* 隐私模式下写不进就算了 */ }
}

/** 风格锁选择器：只决定「送进模型的那一份」加不加约束块，不改用户写的字。 */
export default function StyleBlockPicker({ value, onChange, disabled, hint = true }: {
  value: string;
  onChange: (id: string) => void;
  disabled?: boolean;
  /** 关掉一句话说明时只留下拉，用在已经很挤的表单里。 */
  hint?: boolean;
}) {
  const [blocks, setBlocks] = useState<Block[]>(cached || []);

  useEffect(() => {
    if (cached) return;
    let alive = true;
    void request<{ blocks: Block[] }>('/api/style-blocks')
      .then(data => {
        cached = Array.isArray(data.blocks) ? data.blocks : [];
        if (alive) setBlocks(cached);
      })
      .catch(() => { /* 规则库读不到就当没这个功能，不挡生成 */ });
    return () => { alive = false; };
  }, []);

  const current = blocks.find(block => block.id === value);

  return <span className="sb">
    <Lock size={12} />
    <select value={value} disabled={disabled} aria-label="风格锁"
      title="只把固定的质感约束拼进模型输入，不修改你写的提示词"
      onChange={event => { onChange(event.target.value); remember(event.target.value); }}>
      <option value="">不套用风格锁</option>
      {blocks.map(block => <option key={block.id} value={block.id}>{block.name}</option>)}
    </select>
    {hint && <small>{current ? current.note : '只约束质感，不改你的文字'}</small>}
  </span>;
}
