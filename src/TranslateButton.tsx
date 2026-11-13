import { useEffect, useRef, useState } from 'react';
import { ChevronDown, Languages, LoaderCircle } from 'lucide-react';
import { request } from './api/client';
import './translate-button.css';

type Channel = 'local' | 'online';

/** 把当前提示词一键译成英文。
 *
 * H3 / Qwen 对英文提示更敏感，所以每个提示词输入框旁边都放一个。走的是写词通道的
 * `mode: 'translate'`：本地 Ollama 或在线接口都能用，译完直接替换原文，并留一个撤销口子。
 *
 * 面板本身有「本地 / 在线」切换时把 provider 传进来，按钮跟着它走；没有的话点右侧箭头
 * 指定这一次走哪条通道，主按钮沿用上次的选择。
 */
export default function TranslateButton({ text, disabled, provider, onTranslated, onError }: {
  text: string;
  disabled?: boolean;
  /** 面板已有通道选择时传进来，保持和识图/写词同一个通道。 */
  provider?: Channel;
  onTranslated: (english: string) => void;
  onError?: (message: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState(false);
  const [channel, setChannel] = useState<Channel>(provider || 'local');
  /** 译前原文：整段被覆盖后还能退回去。 */
  const [backup, setBackup] = useState('');
  /** 没传 onError 的面板，就在按钮旁边自己报一句，不把面板的错误区管道改一遍。 */
  const [failure, setFailure] = useState('');
  const boxRef = useRef<HTMLSpanElement>(null);

  useEffect(() => { if (provider) setChannel(provider); }, [provider]);

  useEffect(() => {
    if (!open) return;
    const onDoc = (event: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', onDoc);
    return () => document.removeEventListener('mousedown', onDoc);
  }, [open]);

  const run = async (chan: Channel) => {
    const source = text.trim();
    if (!source || busy) return;
    setBusy(true); setOpen(false); setFailure('');
    try {
      const data = await request<{ prompt: string; provider?: string }>('/api/prompt-writer/draft', {
        json: { idea: source, provider: chan, mode: 'translate', temperature: 0.2 },
        // 首次调用要加载本机模型，30 秒不够。
        timeoutMs: 300_000,
      });
      const next = String(data.prompt || '').trim();
      if (!next) throw new Error('翻译没有返回内容，稍后再试');
      setChannel(chan);
      setBackup(source);
      onTranslated(next);
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : '翻译失败';
      setFailure(message);
      onError?.(message);
    } finally { setBusy(false); }
  };

  const undo = () => {
    if (!backup) return;
    onTranslated(backup);
    setBackup('');
  };

  return <span className="tb" ref={boxRef}>
    <button type="button" className="tb-main" disabled={disabled || busy || !text.trim()}
      title={`译成英文（${channel === 'local' ? '本机模型' : '在线接口'}）：英文提示对 H3 / Qwen 更友好`}
      onClick={() => void run(channel)}>
      {busy ? <LoaderCircle size={12} className="sf-spin" /> : <Languages size={12} />}
      {busy ? '翻译中…' : '译英'}
    </button>
    <button type="button" className="tb-caret" aria-label="选择翻译通道" aria-expanded={open}
      disabled={disabled || busy} onClick={() => setOpen(value => !value)}><ChevronDown size={11} /></button>
    {open && <span className="tb-menu" role="menu">
      <button type="button" role="menuitem" onClick={() => void run('local')}>
        本地模型{channel === 'local' ? ' · 当前' : ''}
      </button>
      <button type="button" role="menuitem" onClick={() => void run('online')}>
        在线接口{channel === 'online' ? ' · 当前' : ''}
      </button>
    </span>}
    {backup && <button type="button" className="tb-undo" onClick={undo} title="换回翻译前的中文">撤销译文</button>}
    {failure && <span className="tb-error" title={failure}>{failure}</span>}
  </span>;
}
