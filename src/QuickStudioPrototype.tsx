import React, { useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { ArrowLeft, ArrowRight, Check, ChevronDown, Clapperboard, Download, Film, Image as ImageIcon, Layers, LoaderCircle, Maximize2, Pause, Play, Plus, Sparkles, Upload, X } from 'lucide-react';
import './quick-studio-prototype.css';

type Mode = 'animate' | 'image' | 'frames';
type Candidate = { id: string; image: string; prompt: string; first: string; last: string; basedOn?: number; duration: number; ratio: string; version: number };
type Shot = { id: string; title: string; prompt: string; first: string; last: string; duration: number; ratio: string; candidates: Candidate[]; adopted?: string; selected?: string; compareLeft?: string; editingFrom?: number };
const templates: { id: Mode; title: string; subtitle: string; tag: string; number: string }[] = [
  { id: 'animate', title: '让图片动起来', subtitle: '一张图片，一点想象，一段视频。', tag: '图生视频', number: '01' },
  { id: 'image', title: '把想法变成图片', subtitle: '描述你想看到的画面，就从这里开始。', tag: '文生图', number: '02' },
  { id: 'frames', title: '连接两个画面', subtitle: '放入首尾帧，让故事在中间发生。', tag: '首尾帧视频', number: '03' },
];
const examplePrompts = ['海边的白色灯塔，镜头缓缓向前，海浪轻轻起伏，温暖的傍晚，保持原有构图。', '一间坐落在山谷中的小木屋，橙色灯光，远山与松林，安静的蓝调时刻，电影感插画。', '从远处海岸缓缓靠近灯塔，光线从蓝调过渡到日落，平稳运镜。'];
const uid = () => globalThis.crypto.randomUUID();

// Original vector fixtures: no private work, external images, or generation services.
function scene(index = 0, variant = 0) {
  const cabin = index === 1;
  const warm = variant % 2 === 1;
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="800" viewBox="0 0 1280 800"><defs>
    <linearGradient id="sky" x2="0" y2="1"><stop stop-color="${cabin ? warm ? '#6b5264' : '#182e49' : warm ? '#a88a99' : '#7cabb5'}"/><stop offset="1" stop-color="${cabin ? warm ? '#dbbb9b' : '#adb4b1' : '#f7dcc1'}"/></linearGradient>
    <linearGradient id="sea" x2="0" y2="1"><stop stop-color="#80a6ae"/><stop offset="1" stop-color="#234f64"/></linearGradient>
    <linearGradient id="cliff" x2="0.6" y2="1"><stop stop-color="#78857d"/><stop offset="1" stop-color="#253f44"/></linearGradient>
    <linearGradient id="light"><stop stop-color="#fff4c2" stop-opacity=".8"/><stop offset="1" stop-color="#fff4c2" stop-opacity="0"/></linearGradient>
    <filter id="grain"><feTurbulence baseFrequency=".55" numOctaves="3" stitchTiles="stitch"/><feColorMatrix type="saturate" values="0"/><feComponentTransfer><feFuncA type="linear" slope=".07"/></feComponentTransfer><feBlend in="SourceGraphic" mode="soft-light"/></filter>
  </defs><g filter="url(#grain)"><path fill="url(#sky)" d="M0 0h1280v800H0z"/><circle cx="${cabin ? 930 : 940}" cy="${warm ? 355 : 225}" r="${cabin ? 32 : 66}" fill="#ffe6bd" opacity=".83"/>
  <path d="M0 414L170 264 319 394 504 210 707 407 874 335 1080 440 1280 285V800H0" fill="#5d8291" opacity=".36"/>
  ${cabin ? `<path d="M0 568L180 451 345 532 493 405 746 519 969 374 1280 520V800H0" fill="#325967"/><path d="M0 666Q330 501 605 636T1280 590V800H0" fill="#1d3945"/><path d="M-20 800L220 648 335 724 515 606 733 705 1024 599 1300 710V800" fill="#152d37"/><path d="M510 629V500L690 405 849 510V637Z" fill="#304a51"/><path d="M475 503L683 380 885 501 858 520 686 426 500 532Z" fill="#182b35"/><path d="M545 519H800V620H545Z" fill="#f1bd72"/><path d="M608 512V625M685 481V625M752 512V625" stroke="#384a4c" stroke-width="12"/><path d="M680 643L869 800H729L650 643Z" fill="#b49b7d" opacity=".5"/>${[120,250,370,920,1070,1160].map((x,i)=>`<g transform="translate(${x},${420+i%3*40})"><path d="M0 0L-58 185H-35L-80 252H80L35 185H58Z" fill="#173e48"/><path d="M-5 216H5V315H-5Z" fill="#17313c"/></g>`).join('')}` : `<path fill="url(#sea)" d="M0 437H1280V800H0z"/><path d="M0 455Q270 415 444 463T780 454 1280 447" fill="none" stroke="#e2d9c8" opacity=".5" stroke-width="3"/>${[493,544,611,688,748].map((y,i)=>`<path d="M${i%2*100} ${y}Q230 ${y-24} 420 ${y+6}T850 ${y} 1260 ${y+9}" fill="none" stroke="#bfd5ce" opacity="${.24+i*.03}" stroke-width="${i+2}"/>`).join('')}<path d="M0 765L167 641 304 620 443 470 660 461 751 540 942 586 1090 716 1280 768V800H0" fill="url(#cliff)"/><path d="M170 679L335 648 465 507 646 498 732 559 609 569 418 577 325 711Z" fill="#abb19b"/><path d="M443 493L543 436 649 470 672 501 538 492Z" fill="#d5c4a7"/><path d="M513 454L533 239H582L603 454Z" fill="#f3ebd6"/><path d="M551 253H582L603 454H556Z" fill="#d0d8ce"/><path d="M535 344H586V357H533Z" fill="#e1b79a"/><path d="M522 237H593V252H522Z" fill="#354e52"/><path d="M530 198H585V235H530Z" fill="#ffe9a4"/><path d="M526 198L557 177 589 198Z" fill="#304850"/><path d="M539 202V233M558 201V234M576 202V233" stroke="#4b6260" stroke-width="4"/><path d="M587 203L1080 137V286L587 224Z" fill="url(#light)" opacity=".24"/><path d="M624 473V416H697V476Z" fill="#dfcdb0"/><path d="M611 416L659 381 710 416Z" fill="#7e6559"/><path d="M651 440H668V475H651Z" fill="#425d5f"/><path d="M743 261q14 -12 28 0q15 -12 28 0M863 312q10 -9 20 0q10 -9 20 0" fill="none" stroke="#516974" stroke-width="3"/>`}
  </g></svg>`;
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
}
function newShot(index: number, mode: Mode): Shot {
  return { id: uid(), title: mode === 'image' ? `图片 ${index}` : `片段 ${String(index).padStart(2, '0')}`, prompt: '', first: '', last: '', duration: 5, ratio: '16:9', candidates: [] };
}
function Prototype() {
  const [mode, setMode] = useState<Mode | null>(null);
  const [shots, setShots] = useState<Shot[]>([]);
  const [activeId, setActiveId] = useState('');
  const [busyId, setBusyId] = useState<string | null>(null);
  const [playing, setPlaying] = useState(false);
  const [playAll, setPlayAll] = useState(false);
  const [compare, setCompare] = useState(false);
  const [editing, setEditing] = useState(false);
  const [inputsOpen, setInputsOpen] = useState(false);
  const [exportOpen, setExportOpen] = useState(false);
  const [notice, setNotice] = useState('');
  const [organized, setOrganized] = useState('');
  const [organizeOpen, setOrganizeOpen] = useState(false);
  const [workName, setWorkName] = useState('海岸来信');
  const [seriesName, setSeriesName] = useState('');
  const [expandedPreview, setExpandedPreview] = useState(false);
  const generationTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const workspace = useRef<HTMLDivElement>(null);
  const active = shots.find(s => s.id === activeId);
  const selected = active?.candidates.find(c => c.id === active.selected) ?? active?.candidates.at(-1);
  const adopted = active?.candidates.find(c => c.id === active.adopted);
  const selectedIndex = active?.candidates.findIndex(c => c.id === selected?.id) ?? -1;
  const baseline = active?.candidates.find(c => c.id === active.compareLeft && c.id !== selected?.id)
    ?? active?.candidates[selectedIndex - 1] ?? active?.candidates[selectedIndex + 1];
  const ready = Boolean(active && active.prompt.trim() && (mode === 'image' || active.first) && (mode !== 'frames' || active.last));
  const allAdopted = shots.length > 0 && shots.every(s => s.adopted);
  const pending = shots.filter(s => !s.adopted).length;
  const currentIndex = shots.findIndex(s => s.id === activeId);
  const step = exportOpen ? 5 : editing ? 3 : selected ? 4 : ready ? 2 : mode ? 1 : 0;
  useEffect(() => () => clearTimeout(generationTimer.current), []);
  useEffect(() => { if (!notice) return; const timer = setTimeout(() => setNotice(''), 5000); return () => clearTimeout(timer); }, [notice]);
  useEffect(() => {
    if (!playing || !selected || !active) return;
    const timer = setTimeout(() => {
      if (playAll && currentIndex < shots.length - 1) setActiveId(shots[currentIndex + 1].id);
      else { setPlaying(false); setPlayAll(false); }
    }, selected.duration * 1000);
    return () => clearTimeout(timer);
  }, [playing, playAll, activeId, selected?.id, selected?.duration, currentIndex, shots]);
  useEffect(() => {
    const close = (event: KeyboardEvent) => { if (event.key === 'Escape') { setExportOpen(false); setOrganizeOpen(false); setExpandedPreview(false); } };
    window.addEventListener('keydown', close); return () => window.removeEventListener('keydown', close);
  }, []);
  function patch(changes: Partial<Shot>) { setShots(previous => previous.map(s => s.id === activeId ? { ...s, ...changes } : s)); }
  function start(template: Mode) {
    const shot = newShot(1, template); setMode(template); setShots([shot]); setActiveId(shot.id); setCompare(false); setEditing(false); setPlaying(false); setPlayAll(false); setInputsOpen(false); setOrganized('');
  }
  function sample() {
    if (!mode) return;
    const index = mode === 'image' ? 1 : mode === 'frames' ? 2 : 0;
    patch({ first: scene(index), last: mode === 'frames' ? scene(2, 1) : '', prompt: examplePrompts[index] });
    setNotice('已放入示例，可以直接体验生成。');
  }
  async function upload(file: File | undefined, field: 'first' | 'last') {
    if (!file || !active) return;
    if (!/^image\/(png|jpeg|webp|gif)$/.test(file.type)) { setNotice('请选择 PNG、JPG、WebP 或 GIF 图片。'); return; }
    if (file.size > 12 * 1024 * 1024) { setNotice('原型支持 12 MB 以内的图片。'); return; }
    const target = active.id;
    try {
      const url = await new Promise<string>((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(String(reader.result)); reader.onerror = reject; reader.readAsDataURL(file); });
      setShots(previous => previous.map(s => s.id === target ? { ...s, [field]: url } : s));
    } catch { setNotice('图片读取失败，请重新选择。'); }
  }
  function generate() {
    if (!active || busyId || !ready) return;
    const snapshot = { ...active }; const version = snapshot.candidates.length + 1;
    setBusyId(active.id); setPlaying(false); setPlayAll(false); setCompare(false);
    generationTimer.current = setTimeout(() => {
      const result: Candidate = { id: uid(), image: mode === 'image' ? scene(1, version - 1) : snapshot.first, prompt: snapshot.prompt, first: snapshot.first, last: snapshot.last, basedOn: editing ? snapshot.editingFrom : undefined, duration: snapshot.duration, ratio: snapshot.ratio, version };
      setShots(previous => previous.map(s => s.id === snapshot.id ? { ...s, candidates: [...s.candidates, result], selected: result.id, compareLeft: snapshot.candidates.at(-1)?.id, editingFrom: undefined } : s));
      setBusyId(null); setEditing(false); setCompare(version > 1); setNotice(version > 1 ? '新候选已就绪。原来采用的版本仍然保留。' : '第一版已就绪。看看效果，或修改提示词再生成。');
    }, 1400);
  }
  function addShot() {
    if (!mode) return;
    const shot = newShot(shots.length + 1, mode);
    if (active) { shot.first = adopted?.last || adopted?.image || selected?.image || active.first; shot.ratio = active.ratio; }
    setShots(previous => [...previous, shot]); setActiveId(shot.id); setEditing(false); setCompare(false); setPlaying(false); setPlayAll(false);
    setNotice('新片段已接在后面，沿用上一段可用画面作为首帧。');
  }
  function chooseShot(id: string) { setActiveId(id); setCompare(false); setEditing(false); setPlaying(false); setPlayAll(false); }
  function adopt(candidate = selected) { if (!candidate) return; patch({ adopted: candidate.id }); setNotice(`已采用版本 ${candidate.version}，可以继续调整或导出。`); }
  function previewVersion(id: string, leftId?: string) {
    patch({ selected: id, compareLeft: leftId }); setPlaying(false); setPlayAll(false); setEditing(false);
  }
  function beginEdit() {
    if (!selected) return;
    patch({ prompt: selected.prompt, first: selected.first, last: selected.last, duration: selected.duration, ratio: selected.ratio, editingFrom: selected.version });
    setEditing(true); setCompare(false); setPlaying(false); setPlayAll(false);
  }
  function downloadPreview() {
    const escape = (value: string) => value.replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]!));
    const entries = shots.map(shot => {
      const c = shot.candidates.find(v => v.id === shot.adopted)!;
      return `<article><h2>${escape(shot.title)} · V${c.version}</h2><img src="${escape(c.image)}" alt="${escape(shot.title)}"/><p>${escape(c.prompt)}</p>${c.basedOn ? `<p>基于 V${c.basedOn} 修改提示词后生成</p>` : ''}<small>${escape(c.ratio)}${mode !== 'image' ? ` · ${c.duration} 秒` : ''}</small></article>`;
    }).join('');
    const html = `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>轻创作 · 演示预览</title><style>body{font:16px/1.7 system-ui;background:#f5f6f8;color:#202936;max-width:1000px;margin:40px auto;padding:24px}article{background:white;padding:24px;border-radius:16px;margin:24px 0}img{width:100%;max-height:560px;object-fit:contain;background:#17232d;border-radius:8px}small{color:#586678}</style><h1>${escape(organized || '我的轻创作')}</h1><p>交互原型演示 · 以下为示例画面及输入记录，未经模型生成，也不是正式导出的视频。</p>${entries}</html>`;
    const url = URL.createObjectURL(new Blob([html], { type: 'text/html;charset=utf-8' }));
    const link = document.createElement('a'); link.href = url; link.download = '轻创作-演示预览.html'; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    setExportOpen(false); setNotice('演示预览已下载。可以打开查看已采用的画面和输入记录。');
  }
  function FrameInput({ field, label }: { field: 'first' | 'last'; label: string }) {
    return <label className={`qs-upload ${active?.[field] ? 'qs-upload-filled' : ''}`} onDragOver={e => e.preventDefault()} onDrop={e => { e.preventDefault(); if (!busyId) void upload(e.dataTransfer.files[0], field); }}>
      {active?.[field] ? <><img src={active[field]} alt={label} /><span><Upload size={14} /> 更换{label}</span></> : <><span className="qs-upload-icon"><Plus size={22} /></span><strong>{label === '首帧' ? '放入一张图片' : '放入结束画面'}</strong><small>点击选择，或拖入这里</small></>}
      <input aria-label={`上传${label}`} type="file" accept="image/png,image/jpeg,image/webp,image/gif" disabled={Boolean(busyId)} onChange={e => void upload(e.target.files?.[0], field)} />
    </label>;
  }
  function Preview({ candidate, label }: { candidate?: Candidate; label?: string }) {
    const [previewWidth, previewHeight] = (candidate?.ratio || active?.ratio || '16:9').split(':').map(Number);
    return <div className={`qs-media ${playing ? 'qs-media-playing' : ''}`} style={{ aspectRatio: candidate?.ratio.replace(':', '/') || active?.ratio.replace(':', '/') || '16/9', '--preview-aspect': previewWidth / previewHeight } as React.CSSProperties}>
      <img src={candidate?.image || active?.first || scene(mode === 'image' ? 1 : 0)} alt={label || '当前画面预览'} style={candidate && candidate.version > 1 && mode !== 'image' ? { filter: 'saturate(.82) sepia(.12)' } : undefined} />
      {label && <span className="qs-media-label">{label}</span>}
      <span className="qs-media-watermark">{candidate ? mode === 'image' ? '示例图片 · 非模型生成' : '示例动效 · 非模型生成' : '效果示意'}</span>
      {!candidate && <div className="qs-preview-invitation"><span><Sparkles size={24} /></span><h3>让一个想法，先有画面。</h3><p>{mode === 'image' ? '写下描述，然后生成你的第一张图片' : '准备好素材后，第一版会出现在这里'}</p></div>}
      {busyId === activeId && <div className="qs-generating"><LoaderCircle className="qs-spin" size={28} /><strong>正在模拟生成…</strong><span>输入已记录，稍后就能比较效果</span></div>}
    </div>;
  }
  return <div className="qs-app">
    <header className="qs-topbar"><a className="qs-brand" href="/prototype.html" aria-label="轻创作首页"><span><Clapperboard size={20} /></span>导演工作台 <i>/</i> <b>轻创作</b></a><span className="qs-prototype"><span />交互原型 · 模拟生成</span><div className="qs-top-actions">{mode && <button onClick={() => setOrganizeOpen(true)} className="qs-text-button"><Layers size={16} /><span>{organized || '整理为作品'}</span></button>}<span className="qs-avatar">我</span></div></header>
    {!mode ? <main className="qs-home"><div className="qs-home-heading"><span className="qs-eyebrow">从一个画面开始</span><h1>有个想法？<br />先让它发生。</h1><p>选一个工具，放入素材。剩下的，边看边调整。</p></div><span className="qs-section-caption">今天想做什么 <span>无需创建作品或系列</span></span><div className="qs-template-grid">{templates.map((template, index) => <button className="qs-template" key={template.id} onClick={() => start(template.id)}><div className="qs-template-art"><img src={scene(index, index === 2 ? 1 : 0)} alt="" /><span className="qs-template-tag">{template.tag}</span>{index !== 1 && <span className="qs-template-play"><Play size={20} fill="currentColor" /></span>}<span className="qs-template-number">{template.number}</span></div><div className="qs-template-description"><div><h2>{template.title}</h2><p>{template.subtitle}</p></div><span className="qs-template-arrow"><ArrowRight size={21} /></span></div></button>)}</div><div className="qs-home-note"><span><Sparkles size={17} /> 先试一版，再把它变好。</span><p>不满意？修改当前画面、比较版本。需要更长的故事时，再把片段接起来。</p></div><footer className="qs-home-footer"><span>你的表达在前，复杂的工作流在后。</span><span>本页仅演示交互；刷新后重置，素材不会上传。</span></footer></main> : <main className="qs-studio" ref={workspace}>
      <div className="qs-work-heading"><div><button className="qs-back qs-text-button" disabled={Boolean(busyId)} onClick={() => { setMode(null); setPlaying(false); setPlayAll(false); }}><ArrowLeft size={14} />选择其他工具</button><h1>{templates.find(t => t.id === mode)?.title}<span>{organized || '未命名练习'}</span></h1></div><button className="qs-button qs-primary" disabled={!allAdopted || Boolean(busyId)} onClick={() => setExportOpen(true)} title={pending ? `还有 ${pending} 个${mode === 'image' ? '图片' : '片段'}未采用版本` : '导出已采用的结果'}><Download size={16} />导出{mode === 'image' ? '图片' : shots.length > 1 ? '成片' : '片段'}</button></div>
      <ol className="qs-steps" aria-label="创作流程">{['选择模板', '放入素材', '生成', '修改提示词', '比较采用', '导出'].map((label, i) => <li key={label} className={step === i ? 'current' : step > i && (i !== 3 || active?.candidates.some(c => c.basedOn)) ? 'completed' : ''} aria-current={step === i ? 'step' : undefined}><span>{step > i && (i !== 3 || active?.candidates.some(c => c.basedOn)) ? <Check size={12} /> : i + 1}</span>{label}{i < 5 && <i />}</li>)}</ol>
      <div className={`qs-workspace ${expandedPreview ? 'qs-expanded-preview' : ''}`}>
        <section className="qs-input-panel" aria-label="当前输入"><div className="qs-panel-title"><h2>{editing ? '修改提示词，再试一版' : '告诉我，你想看到什么'}</h2><span>{currentIndex + 1 < 10 ? '0' : ''}{currentIndex + 1}</span></div>
          {editing ? <>
            <p className="qs-panel-intro">已载入 V{active?.editingFrom} 的完整提示词。直接修改下面的内容，再生成一版。</p>
            <label className="qs-label">提示词<textarea autoFocus value={active?.prompt || ''} onChange={e => patch({ prompt: e.target.value })} placeholder="填写完整的画面、动作与镜头描述" rows={8} disabled={Boolean(busyId)} /></label>
            <div className="qs-preserve"><Check size={15} /><div><strong>原来的版本留着</strong><p>新版本保存修改后的提示词，其他素材和设置沿用 V{active?.editingFrom}。</p></div></div>
            <button className="qs-text-button" onClick={() => setEditing(false)} disabled={Boolean(busyId)}><ArrowLeft size={14} />返回素材与描述</button>
          </> : <>
          {mode !== 'image' && <div className={`qs-frame-inputs ${mode === 'frames' ? 'qs-two-frames' : ''}`}><div><span className="qs-field-caption">{mode === 'frames' ? '首帧' : '参考图片'}</span><FrameInput field="first" label="首帧" /></div>{mode === 'frames' && <div><span className="qs-field-caption">尾帧</span><FrameInput field="last" label="尾帧" /></div>}</div>}
          <button className="qs-sample" onClick={sample} disabled={Boolean(busyId)}><Sparkles size={14} />没有素材？用示例体验<ArrowRight size={14} /></button>
          <label className="qs-label">{mode === 'image' ? '描述想要的画面' : '让画面怎样动起来'}<textarea value={active?.prompt || ''} onChange={e => patch({ prompt: e.target.value })} rows={mode === 'image' ? 6 : 4} placeholder={mode === 'image' ? '例如：山谷中的小木屋，窗边透出暖光，安静的傍晚…' : '例如：镜头缓缓向前，微风吹动衣角…'} disabled={Boolean(busyId)} /></label>
          <div className="qs-specs"><label>画幅<select value={active?.ratio} onChange={e => patch({ ratio: e.target.value })} disabled={Boolean(busyId)}><option>16:9</option><option>9:16</option><option>1:1</option></select></label>{mode !== 'image' && <label>时长<select value={active?.duration} onChange={e => patch({ duration: Number(e.target.value) })} disabled={Boolean(busyId)}><option value="5">5 秒</option><option value="8">8 秒</option><option value="10">10 秒</option></select></label>}</div>
          <details className="qs-tool-details"><summary>所用工具 <span>推荐配置<ChevronDown size={13} /></span></summary><p>{templates.find(t => t.id === mode)?.tag} · 标准质量</p><p>正式接入后由模板提供默认参数。当前使用内置插画演示，不调用 ComfyUI。</p></details>
          </>}
          <div className="qs-generate-area"><button className="qs-button qs-primary qs-generate" disabled={!ready || Boolean(busyId)} onClick={generate}>{busyId === activeId ? <LoaderCircle size={18} className="qs-spin" /> : <Sparkles size={18} />}{busyId === activeId ? '正在生成…' : editing ? '用新提示词生成' : selected ? '再生成一版' : '生成第一版'}</button><small>{editing ? '使用上方完整提示词；原型仍为模拟生成。' : !ready ? mode === 'image' ? '写一段描述就能开始' : mode === 'frames' ? '放入首尾帧，再描述中间的变化' : '放入图片，再描述你想要的动作' : '原型模拟约 2 秒，不消耗算力'}</small></div>
        </section>
        <section className="qs-result-panel" aria-label="效果与候选"><div className="qs-result-top"><div><span className="qs-live-dot" />{playing ? playAll ? '正在连续预演' : '正在预演' : selected ? `${active?.title} · 版本 ${selected.version}` : '画面预览'}{selected && <span className="qs-result-state">{selected.id === active?.adopted ? '已采用' : '候选'}</span>}</div><div>{(active?.candidates.length || 0) > 1 && <button className={`qs-text-button ${compare ? 'qs-blue' : ''}`} onClick={() => setCompare(!compare)} aria-pressed={compare}><Layers size={15} />{compare ? '退出比较' : '并排比较'}</button>}<button className="qs-icon-button" aria-label={expandedPreview ? '收起预览' : '放大预览'} onClick={() => setExpandedPreview(!expandedPreview)}>{expandedPreview ? <X size={17} /> : <Maximize2 size={16} />}</button></div></div>
          {compare && baseline && selected && <div className="qs-compare-selectors" role="group" aria-label="比较版本选择">
            <div><label>左侧版本<select value={baseline.id} disabled={Boolean(busyId)} onChange={e => patch({ compareLeft: e.target.value })}>{active?.candidates.map(c => <option key={c.id} value={c.id} disabled={c.id === selected.id}>V{c.version}{c.id === active.adopted ? ' · 已采用' : ''}</option>)}</select></label><button className="qs-text-button" disabled={Boolean(busyId) || baseline.id === active?.adopted} onClick={() => adopt(baseline)}>{baseline.id === active?.adopted ? `左侧 V${baseline.version} 已采用` : `采用左侧 V${baseline.version}`}</button></div>
            <div><label>右侧版本<select value={selected.id} disabled={Boolean(busyId)} onChange={e => previewVersion(e.target.value, baseline.id)}>{active?.candidates.map(c => <option key={c.id} value={c.id} disabled={c.id === baseline.id}>V{c.version}{c.id === active.adopted ? ' · 已采用' : ''}</option>)}</select></label><span>修改提示词与主操作作用于右侧</span></div>
          </div>}
          <div className={`qs-stage ${compare && baseline ? 'qs-compare' : ''}`}>{compare && baseline && <Preview candidate={baseline} label={`左侧 · V${baseline.version}${baseline.id === active?.adopted ? ' · 已采用' : ''}`} />}<Preview candidate={selected} label={compare ? `右侧 · V${selected?.version}${selected?.id === active?.adopted ? ' · 已采用' : ''}` : undefined} /></div>
          {compare && baseline && selected && <div className="qs-compare-prompts">
            {[baseline, selected].map((candidate, i) => <details key={`${i}:${candidate.id}`}><summary>{i === 0 ? '左侧' : '右侧'} V{candidate.version} 的提示词</summary><p>{candidate.prompt}</p></details>)}
          </div>}
          <div className="qs-playback">{mode !== 'image' && shots.length > 1 && <button className="qs-icon-button" aria-label="上一片段" disabled={currentIndex <= 0 || Boolean(busyId)} onClick={() => chooseShot(shots[currentIndex - 1].id)}><ArrowLeft size={16} /></button>}<button className="qs-icon-button" aria-label={playing ? '暂停预演' : '播放预演'} disabled={!selected || mode === 'image' || Boolean(busyId)} onClick={() => { setPlaying(!playing); setPlayAll(false); }}>{playing ? <Pause size={17} /> : <Play size={17} fill="currentColor" />}</button>{mode !== 'image' && shots.length > 1 && <button className="qs-icon-button" aria-label="下一片段" disabled={currentIndex >= shots.length - 1 || Boolean(busyId)} onClick={() => chooseShot(shots[currentIndex + 1].id)}><ArrowRight size={16} /></button>}<span>{mode === 'image' ? '图片预览' : `00:00 / 00:${String(selected?.duration || active?.duration).padStart(2, '0')}`}</span><div className="qs-playback-line"><i className={playing ? 'moving' : ''} style={{ animationDuration: `${selected?.duration || 5}s` }} /></div><span>{selected?.ratio || active?.ratio}</span><span className="qs-preview-caption">{mode === 'image' ? '示例插画' : '动态预演'}</span></div>
          {selected ? <>
            <div className="qs-result-actions"><div><strong>{selected.id === active?.adopted ? '这一版，留下了。' : compare ? '喜欢哪一版，由你决定。' : '这一版怎么样？由你决定。'}</strong><p>{selected.basedOn ? `基于 V${selected.basedOn} 修改提示词后生成。` : '可以直接采用，或修改提示词再生成。'}</p></div><div><button className="qs-button" onClick={beginEdit} disabled={Boolean(busyId)}><Sparkles size={15} />修改提示词</button><button className={`qs-button ${selected.id === active?.adopted ? 'qs-adopted' : 'qs-primary'}`} onClick={() => adopt()} disabled={Boolean(busyId) || selected.id === active?.adopted}><Check size={16} />{selected.id === active?.adopted ? '已采用' : compare ? `采用右侧 V${selected.version}` : '采用这一版'}</button></div></div>
            {!compare && <details className="qs-version-prompt"><summary>V{selected.version} 的提示词</summary><p>{selected.prompt}</p></details>}
            <div className="qs-versions"><span>尝试过的版本</span>{active?.candidates.map(c => <button key={c.id} className={c.id === selected.id ? 'selected' : ''} onClick={() => previewVersion(c.id)} disabled={Boolean(busyId)} aria-label={`查看版本 ${c.version}`}><img src={c.image} alt="" /><span>V{c.version}</span>{c.id === active.adopted && <Check size={13} />}</button>)}<small>切换预览不会改变采用版本</small></div>
          </> : <div className="qs-result-empty"><ImageIcon size={18} /><p>这里留给你的画面。先生成，再慢慢打磨。</p></div>}
        </section>
      </div>
      {mode !== 'image' && <section className="qs-story" aria-label="连续片段"><div className="qs-story-heading"><div><Film size={18} /><h2>片段浏览</h2><span>{shots.length} 段 · {shots.reduce((sum, s) => sum + (s.candidates.find(c => c.id === s.adopted)?.duration || s.duration), 0)} 秒</span></div><div><button className="qs-text-button" aria-expanded={inputsOpen} onClick={() => setInputsOpen(!inputsOpen)}>{inputsOpen ? '收起输入' : '展开每段输入'}<ChevronDown size={14} /></button>{shots.length > 1 && <button className="qs-text-button" disabled={!allAdopted || Boolean(busyId)} onClick={() => { setShots(previous => previous.map(s => ({ ...s, selected: s.adopted }))); setActiveId(shots[0].id); setCompare(false); setPlaying(true); setPlayAll(true); }}><Play size={14} />连续预演</button>}</div></div><div className="qs-track">{shots.map((shot, i) => {
        const c = shot.candidates.find(v => v.id === shot.adopted) || shot.candidates.at(-1);
        return <React.Fragment key={shot.id}>{i > 0 && <div className="qs-connector"><ArrowRight size={15} /></div>}<article className={`qs-shot ${shot.id === activeId ? 'active' : ''}`}><button className="qs-shot-select" onClick={() => chooseShot(shot.id)} disabled={Boolean(busyId)} aria-label={`选择${shot.title}`}><div className="qs-shot-thumb">{c?.image || shot.first ? <img src={c?.image || shot.first} alt="" /> : <ImageIcon size={24} />}<span>{String(i + 1).padStart(2, '0')}</span><small>{c?.duration || shot.duration}s</small></div><div className="qs-shot-title"><strong>{shot.title}</strong><span>{shot.adopted ? <><Check size={12} />已采用</> : c ? '待选择' : '待生成'}</span></div></button>{inputsOpen && <div className="qs-shot-inputs"><p>{c?.prompt || shot.prompt || '还没有描述画面'}</p><div>{(['first', 'last'] as const).map(field => <div key={field}>{(c ? c[field] : shot[field]) ? <img src={c ? c[field] : shot[field]} alt={`${shot.title}${field === 'first' ? '首帧' : '尾帧'}`} /> : <span>未设置</span>}<small>{field === 'first' ? '首帧' : '尾帧'}</small></div>)}</div></div>}</article></React.Fragment>;
      })}<button className="qs-add-shot" onClick={addShot} disabled={Boolean(busyId)}><Plus size={23} /><strong>接下一段</strong><small>沿用画面，继续表达</small></button></div><p className="qs-track-note">{pending ? `还有 ${pending} 段等待采用。` : '所有片段已选好，可以连续预演或导出。'} 提示词、首尾帧和版本都跟着片段走。</p></section>}
      <footer className="qs-studio-footer"><span>练习内容仅保留在当前页面，刷新后重置。上传素材不离开浏览器。</span><span>满意了，再整理为作品。</span></footer>
    </main>}
    {notice && <div className="qs-toast" role="status"><Check size={16} />{notice}<button aria-label="关闭提示" className="qs-icon-button" onClick={() => setNotice('')}><X size={14} /></button></div>}
    {exportOpen && <Modal title="把这一版带走" onClose={() => setExportOpen(false)}><div className="qs-export-summary"><span><Download size={25} /></span><div><strong>{mode === 'image' ? '1 张图片' : `${shots.length} 个已采用片段`}</strong><p>只导出你选好的版本，其他尝试继续保留。</p></div></div><div className="qs-export-spec"><span>正式导出格式</span><strong>{mode === 'image' ? 'PNG 图片' : 'MP4 视频 · 1080p'}</strong></div><p className="qs-modal-note">这是交互原型。当前下载可独立打开的 HTML 演示预览，包含已采用的示例画面及输入记录；实际图片生成、视频渲染和媒体导出将在接入后提供。</p><button className="qs-button qs-primary qs-full" onClick={downloadPreview}><Download size={16} />下载演示预览</button></Modal>}
    {organizeOpen && <Modal title="需要的时候，再整理" onClose={() => setOrganizeOpen(false)}><p className="qs-panel-intro">把这次尝试命名为作品。系列可以以后再选。</p><label className="qs-label">作品名称<input autoFocus value={workName} onChange={e => setWorkName(e.target.value)} /></label><label className="qs-label">系列 <span className="qs-optional">可选</span><input value={seriesName} placeholder="暂时不归入系列" onChange={e => setSeriesName(e.target.value)} /></label><p className="qs-modal-note">原型仅在当前页面演示归档，不创建服务器目录。</p><button className="qs-button qs-primary qs-full" disabled={!workName.trim()} onClick={() => { setOrganized(`${seriesName.trim() ? seriesName.trim() + ' / ' : ''}${workName.trim()}`); setOrganizeOpen(false); setNotice('已在本次演示中整理为作品。'); }}><Check size={16} />完成整理</button></Modal>}
  </div>;
}
function Modal({ title, onClose, children }: { title: string; onClose: () => void; children: React.ReactNode }) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => { dialog.current?.showModal(); }, []);
  return <dialog className="qs-modal" ref={dialog} onCancel={onClose} onClick={e => { if (e.target === dialog.current) onClose(); }} aria-label={title}><div className="qs-modal-heading"><h2>{title}</h2><button className="qs-icon-button" aria-label="关闭弹窗" onClick={onClose}><X size={19} /></button></div>{children}</dialog>;
}
createRoot(document.getElementById('root')!).render(<React.StrictMode><Prototype /></React.StrictMode>);
