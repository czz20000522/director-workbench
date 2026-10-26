import { useEffect, useState } from 'react';
import { AudioLines, FolderOpen, LoaderCircle, Music, Sparkles } from 'lucide-react';
import { ApiError, mediaUrl, request } from './api/client';
import { emitBringIn } from './libraryBus';

type Card = { name: string; params: Record<string, unknown>; builtin?: boolean };
type FileItem = { name: string; path: string; size: number; modified: number };
const engineLabel: Record<string, string> = { 'voxcpm-2': 'VoxCPM2', 'indextts-2.5': 'IndexTTS 2.5' };

// 项目内的「库」：只做显式带入。音色→锚点配音，配乐→配乐；其余库给出所在位置。
export default function ProjectLibraryTool() {
  const [timbre, setTimbre] = useState<Card[] | null>(null);
  const [music, setMusic] = useState<Card[] | null>(null);
  const [files, setFiles] = useState<FileItem[] | null>(null);
  const [message, setMessage] = useState('');

  useEffect(() => {
    let active = true;
    request<{ library: { timbre: Card[]; music: Card[] } }>('/api/preset-library')
      .then(data => { if (active) { setTimbre(data.library.timbre || []); setMusic(data.library.music || []); } })
      .catch(() => { if (active) { setTimbre([]); setMusic([]); } });
    request<{ items: FileItem[] }>('/api/enhance-library')
      .then(data => { if (active) setFiles(data.items || []); })
      .catch(reason => { if (!active) return; setFiles([]); setMessage(reason instanceof ApiError ? reason.message : ''); });
    return () => { active = false; };
  }, []);

  function bring(kind: 'timbre' | 'music', card: Card) {
    emitBringIn({ kind, params: card.params });
    setMessage(kind === 'timbre'
      ? `已带入「${card.name}」：去 后期 → 锚点配音 查看，生成前请确认台词与时间点。`
      : `已带入「${card.name}」：去 后期 → 配乐 查看，随机编号仍由你自己定。`);
  }

  return <div className="sf-step-panel sf-tool-panel">
    <p className="sf-step-label">项目工具</p>
    <h1>库</h1>
    <p className="sf-lede">这里的每个库都要你点「带入」才会生效，不会自动套用到任何步骤。素材参考库、提示词库、剧本库在首页左右栏与剧本库弹窗里。</p>
    {message && <p className="sf-post-message" role="status" aria-live="polite">{message}</p>}
    <div className="sf-post-subblock">
      <div className="sf-post-section-head"><h3 className="sf-post-subtitle"><AudioLines size={14} /> 音色库</h3><p className="sf-post-meta">带入到 后期 → 锚点配音</p></div>
      {!timbre ? <p className="sf-post-hint"><LoaderCircle size={14} className="sf-spin" /> 正在读取…</p> : timbre.length === 0 ? <p className="sf-post-hint">音色库还是空的。</p> : <div className="lib-grid">{timbre.map(card => <article className="lib-card" key={card.name}>
        <strong>{card.name}</strong>
        <span>{String(card.params.voice_description || card.params.voice_reference || '')}</span>
        <small>{engineLabel[String(card.params.generated_by || card.params.engine || '')] || '未记录引擎'}</small>
        <button className="button secondary compact" type="button" onClick={() => bring('timbre', card)}>带入锚点配音</button>
      </article>)}</div>}
    </div>
    <div className="sf-post-subblock">
      <div className="sf-post-section-head"><h3 className="sf-post-subtitle"><Music size={14} /> 配乐库</h3><p className="sf-post-meta">带入到 后期 → 配乐</p></div>
      {!music ? <p className="sf-post-hint">正在读取…</p> : music.length === 0 ? <p className="sf-post-hint">配乐库还是空的。</p> : <div className="lib-grid">{music.map(card => <article className="lib-card" key={card.name}>
        <strong>{card.name}</strong>
        <span>{String(card.params.tags || '')}</span>
        <small>{String(card.params.bpm ?? '—')} BPM · {String(card.params.keyscale ?? '—')} · {String(card.params.duration_seconds ?? '—')} 秒</small>
        <button className="button secondary compact" type="button" onClick={() => bring('music', card)}>带入配乐</button>
      </article>)}</div>}
    </div>
    <div className="sf-post-subblock">
      <div className="sf-post-section-head"><h3 className="sf-post-subtitle"><Sparkles size={14} /> 清晰化库</h3><p className="sf-post-meta">素材库\清晰化 · 只读</p></div>
      {!files ? <p className="sf-post-hint">正在读取…</p> : files.length === 0 ? <p className="sf-post-hint">还没有清晰化成品；去 后期 → 清晰化 处理素材。</p> : <ul className="sf-post-history">{files.map(item => <li key={item.path}>
        <a href={mediaUrl(item.path)} download>{item.name}</a><time>{(item.size / 1048576).toFixed(1)} MB</time>
      </li>)}</ul>}
    </div>
    <div className="sf-post-subblock">
      <div className="sf-post-section-head"><h3 className="sf-post-subtitle"><FolderOpen size={14} /> 提示词库 / 素材参考库 / 剧本库</h3><p className="sf-post-meta">首页左栏 / 右栏 / 剧本库弹窗</p></div>
      <p className="sf-post-hint">这三个库的内容需要先落到本作：提示词库「带入」生成提示词；素材参考库「用作开始画面」；剧本库把草稿写进镜头提示词。回到首页即可操作，不会自动写入本作。</p>
    </div>
  </div>;
}
