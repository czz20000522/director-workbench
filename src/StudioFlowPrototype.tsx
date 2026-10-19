import { useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import {
  ArrowRight, Check, CheckCircle2, ChevronLeft, ChevronRight, Clapperboard, Download,
  FileText, Film, Image, Layers3, Play, Plus, Scissors, Sparkles, Upload, Wand2,
  Volume2, X,
} from 'lucide-react';
import './studio-flow-prototype.css';

type Step = 1 | 2 | 3 | 4 | 5 | 6;
type QuickTool = 'text-video' | 'image-video' | 'text-image' | 'enhance';

const steps = [
  { id: 1, label: '愿望', hint: '故事与规格' },
  { id: 2, label: '方案', hint: '显式选择' },
  { id: 3, label: '分镜', hint: '逐段准备' },
  { id: 4, label: '候选', hint: '逐段采用' },
  { id: 5, label: '装配', hint: '检查与成片' },
  { id: 6, label: '交付', hint: '预览与下载' },
] as const;

const presets = [
  { id: 'h3-text', title: '纯文本 · H3 原生音画', meta: '16:9 · 4–15 秒', detail: '只需提示词，适合概念片和单镜头测试。', tone: 'amber' },
  { id: 'h3-first', title: '首帧 · H3 图生视频', meta: '16:9 / 9:16 · 4–15 秒', detail: '已有首帧时，控制起始画面与动作。', tone: 'sage' },
  { id: 'fast-kitchen', title: '快速图生 · Kitchen', meta: '16:9 · 8 步加速', detail: '本机加速方案，适合快速看构图和节奏。', tone: 'blue' },
];

const shots = [
  { id: 'S01', title: '铜钟与午后', prompt: '古董店里，铜钟滴答，午后的光从百叶窗落在桌面，缓慢推近。', duration: '4.5s', state: '已采用' },
  { id: 'S02', title: '桌上的信', prompt: '镜头横移过木桌，手写信纸被阳光照亮，尘埃漂浮。', duration: '4.0s', state: '待采用' },
  { id: 'S03', title: '推门离开', prompt: '门口的风铃轻响，人物推门走入光里，镜头停住。', duration: '3.5s', state: '待生成' },
];

const recent = [
  { title: '雨停之前', series: '短片 · 概念', step: '③ 分镜', progress: 42, tone: 'rain' },
  { title: '午后的古董店', series: '短片 · 概念', step: '④ 候选', progress: 68, tone: 'amber' },
  { title: '海边的最后一班车', series: '短片 · 概念', step: '⑤ 装配', progress: 86, tone: 'blue' },
  { title: '城市清晨', series: '测试 · 练习', step: '已完成', progress: 100, tone: 'sage' },
];

function Thumb({ tone = 'amber', label, film = false }: { tone?: string; label?: string; film?: boolean }) {
  return (
    <div className={`sf-thumb sf-thumb-${tone}`} aria-hidden="true">
      <span>{label ?? '01'}</span>
      {film && <Clapperboard size={22} />}
    </div>
  );
}

function TopBar({ onHome }: { onHome: () => void }) {
  return (
    <header className="sf-topbar">
      <button className="sf-brand" onClick={onHome} aria-label="返回首页">
        <span className="sf-brand-mark"><Clapperboard size={18} /></span>
        <span><strong>导演工作台</strong><small>Director Workbench</small></span>
      </button>
      <div className="sf-topbar-center"><span className="sf-dot" /> 本机工作区 <span className="sf-sep">/</span> 原型预览</div>
      <div className="sf-topbar-actions">
        <span className="sf-account">U</span>
        <button className="sf-ghost" onClick={onHome}>首页</button>
      </div>
    </header>
  );
}

function Home({ onStart, onQuick, onOpen }: { onStart: () => void; onQuick: (tool: QuickTool) => void; onOpen: (title: string) => void }) {
  return (
    <main className="sf-page sf-home">
      <section className="sf-hero">
        <div>
          <p className="sf-eyebrow">从愿望到成片</p>
          <h1>今天想拍一部<br /><em>什么片？</em></h1>
          <p className="sf-lede">先讲清楚你想表达什么，再一步步选方案、看候选、装配成片。工作台只做确定性的事，创作决定始终在你手里。</p>
          <div className="sf-hero-actions">
            <button className="sf-primary sf-large" onClick={onStart}>新建一部影片 <ArrowRight size={17} /></button>
            <button className="sf-secondary" onClick={() => onOpen('雨停之前')}>继续上次 <ChevronRight size={15} /></button>
          </div>
        </div>
        <div className="sf-hero-art" aria-hidden="true">
          <div className="sf-hero-frame sf-hero-frame-a"><Thumb tone="amber" label="S01" /></div>
          <div className="sf-hero-frame sf-hero-frame-b"><Thumb tone="blue" label="S02" /></div>
          <div className="sf-hero-frame sf-hero-frame-c"><Thumb tone="rain" label="S03" /></div>
          <div className="sf-hero-caption"><span>正在创作的镜头</span><strong>雨停之前 · S03</strong></div>
        </div>
      </section>

      <section className="sf-section sf-continue">
        <div className="sf-section-head"><h2>继续上次</h2><span>最近编辑</span></div>
        <button className="sf-continue-card" onClick={() => onOpen('雨停之前')}>
          <Thumb tone="rain" label="雨停之前" film />
          <div className="sf-continue-copy">
            <span className="sf-chip sf-chip-live">草稿 · ③ 分镜</span>
            <h3>雨停之前</h3>
            <p>三个镜头已准备好，下一步：逐段生成候选。</p>
            <div className="sf-progress"><span style={{ width: '42%' }} /></div>
            <small>已完成 3 / 6 步</small>
          </div>
          <span className="sf-round-arrow"><ArrowRight size={18} /></span>
        </button>
      </section>

      <section className="sf-section">
        <div className="sf-section-head"><h2>最近作品</h2><button className="sf-link">查看全部 <ChevronRight size={14} /></button></div>
        <div className="sf-recent-grid">
          {recent.map(item => (
            <button className="sf-recent-card" key={item.title} onClick={() => onOpen(item.title)}>
              <Thumb tone={item.tone} label={item.title} />
              <div className="sf-recent-copy"><span className="sf-chip">{item.step}</span><h3>{item.title}</h3><p>{item.series}</p></div>
              <span className="sf-recent-progress">{item.progress}%</span>
            </button>
          ))}
        </div>
      </section>

      <section className="sf-section sf-quick-section">
        <details className="sf-quick">
          <summary>
            <span><strong>快速开始</strong><small>只做单步工具，不进入整片流程</small></span>
            <ChevronRight size={17} />
          </summary>
          <div className="sf-quick-grid">
            <button onClick={() => onQuick('text-video')}><Play size={19} /><strong>文生视频</strong><span>只生成一个镜头</span></button>
            <button onClick={() => onQuick('image-video')}><Film size={19} /><strong>图生视频</strong><span>让一张图动起来</span></button>
            <button onClick={() => onQuick('text-image')}><Image size={19} /><strong>文生图</strong><span>先生成一张素材</span></button>
            <button onClick={() => onQuick('enhance')}><Wand2 size={19} /><strong>清晰化</strong><span>处理已有图片或视频</span></button>
          </div>
          <p className="sf-quick-note">清晰化是后期动作；其余三个也可以从「新建影片」的分镜步骤里进入。成人向创作在新建时选择分区。</p>
        </details>
      </section>
    </main>
  );
}

function Intent({ onClose, onFull, onQuick }: { onClose: () => void; onFull: () => void; onQuick: (tool: QuickTool) => void }) {
  return (
    <div className="sf-modal-backdrop" role="dialog" aria-modal="true" aria-label="选择创作意图">
      <div className="sf-modal">
        <button className="sf-modal-close" onClick={onClose} aria-label="关闭"><X size={18} /></button>
        <p className="sf-eyebrow">新建</p>
        <h2>你想做什么？</h2>
        <p className="sf-modal-lede">整片流程和单步工具分开，避免在首页先面对一堆参数。</p>
        <button className="sf-intent-primary" onClick={onFull}>
          <span className="sf-intent-icon"><Clapperboard size={20} /></span>
          <span><strong>做一部完整影片</strong><small>愿望 → 方案 → 分镜 → 候选 → 装配 → 成片</small></span>
          <ArrowRight size={18} />
        </button>
        <div className="sf-intent-grid">
          <button onClick={() => onQuick('text-video')}><Play size={18} /><strong>只生成一个镜头</strong><small>文生视频 / 图生视频</small></button>
          <button onClick={() => onQuick('text-image')}><Image size={18} /><strong>只生成一张图</strong><small>素材、首帧或参考图</small></button>
          <button onClick={() => onQuick('enhance')}><Wand2 size={18} /><strong>处理已有素材</strong><small>清晰化 / 放大</small></button>
        </div>
        <div className="sf-zone-pick"><span>内容分区</span><button className="sf-zone selected">普通创作</button><button className="sf-zone">成人向创作 <small>需明确边界</small></button></div>
      </div>
    </div>
  );
}

function QuickPanel({ tool, onBack, onFull }: { tool: QuickTool; onBack: () => void; onFull: () => void }) {
  const copy: Record<QuickTool, { title: string; text: string; icon: ReactNode }> = {
    'text-video': { title: '只生成一个镜头', text: '文生视频：描述画面与动作，生成单个候选。', icon: <Play size={24} /> },
    'image-video': { title: '让一张图动起来', text: '图生视频：选择一张已登记图片作为开始画面。', icon: <Film size={24} /> },
    'text-image': { title: '只生成一张图', text: '文生图：生成一张素材，之后可作为首帧或参考。', icon: <Image size={24} /> },
    enhance: { title: '清晰化 / 放大', text: '对已有图片或视频做 2× 清晰化，不自动替换已采用候选。', icon: <Wand2 size={24} /> },
  };
  const item = copy[tool];
  return (
    <main className="sf-quick-page">
      <button className="sf-back" onClick={onBack}><ChevronLeft size={16} /> 返回首页</button>
      <section className="sf-quick-panel">
        <div className="sf-quick-icon">{item.icon}</div>
        <p className="sf-eyebrow">快速工具</p>
        <h1>{item.title}</h1>
        <p>{item.text}</p>
        <label className="sf-field"><span>提示词 / 说明</span><textarea rows={4} placeholder="用一句话描述你想要的画面或处理目标…" /></label>
        <div className="sf-quick-actions"><button className="sf-primary">生成单步候选 <ArrowRight size={16} /></button><button className="sf-secondary" onClick={onFull}>改为做一部完整影片</button></div>
        <p className="sf-fine">这是工作台原型：单步工具和整片流程共用同一套显式选择/采用边界。</p>
      </section>
    </main>
  );
}

function StepRail({ step, onStep }: { step: Step; onStep: (step: Step) => void }) {
  return (
    <aside className="sf-rail">
      <div className="sf-rail-head"><span>创作流程</span><strong>雨停之前</strong><small>草稿已保存</small></div>
      <ol>
        {steps.map(item => (
          <li key={item.id} className={item.id === step ? 'active' : item.id < step ? 'done' : ''}>
            <button onClick={() => onStep(item.id as Step)} disabled={item.id > step && item.id > 3}>
              <span className="sf-step-dot">{item.id < step ? <Check size={12} /> : item.id}</span>
              <span><strong>{item.label}</strong><small>{item.hint}</small></span>
            </button>
          </li>
        ))}
      </ol>
      <div className="sf-rail-foot"><span className="sf-chip">普通创作</span><button className="sf-link">保存草稿</button></div>
    </aside>
  );
}

function StepOne() {
  return (
    <div className="sf-step-panel">
      <p className="sf-eyebrow">① 愿望</p>
      <h1>先把欲望说清楚</h1>
      <p className="sf-lede">不需要懂模型和工作流。写下想讲什么，后面每一步都会给你明确的选择。</p>
      <div className="sf-form-grid">
        <label className="sf-field sf-wide"><span>这部片想讲什么？</span><textarea rows={4} defaultValue="雨停之前，一个在古董店等人的人，听见钟声，决定去见一个很久没见的人。" /></label>
        <label className="sf-field"><span>作品名</span><input defaultValue="雨停之前" /></label>
        <label className="sf-field"><span>内容分区</span><select defaultValue="general"><option value="general">普通创作</option><option value="adult">成人向创作（需明确边界）</option></select></label>
        <div className="sf-field sf-wide"><span>时长</span><div className="sf-chip-row"><button className="sf-chip-toggle selected">15 秒</button><button className="sf-chip-toggle">30 秒</button><button className="sf-chip-toggle">60 秒</button><button className="sf-chip-toggle">自定义</button></div></div>
        <div className="sf-field sf-wide"><span>画幅</span><div className="sf-chip-row"><button className="sf-chip-toggle selected">16:9</button><button className="sf-chip-toggle">9:16</button><button className="sf-chip-toggle">1:1</button></div></div>
        <div className="sf-field sf-wide"><span>起点</span><div className="sf-choice-row"><button className="sf-choice selected"><Sparkles size={17} /><strong>原创</strong><small>从文字开始</small></button><button className="sf-choice"><Upload size={17} /><strong>参考</strong><small>从参考视频开始</small></button><button className="sf-choice"><FileText size={17} /><strong>剧本</strong><small>已有文字</small></button></div></div>
      </div>
    </div>
  );
}

function StepTwo() {
  const [selected, setSelected] = useState('h3-text');
  return (
    <div className="sf-step-panel">
      <p className="sf-eyebrow">② 方案</p>
      <h1>选择制作方案</h1>
      <p className="sf-lede">只展示事实和限制，不替你推荐。选中的方案决定后面可用的输入和输出。</p>
      <div className="sf-preset-grid">
        {presets.map(preset => (
          <button key={preset.id} className={`sf-preset ${selected === preset.id ? 'selected' : ''}`} onClick={() => setSelected(preset.id)}>
            <Thumb tone={preset.tone} label={preset.id === 'h3-text' ? 'TXT' : preset.id === 'h3-first' ? 'IMG' : 'FAST'} />
            <span className={`sf-chip sf-chip-${preset.tone}`}>{preset.meta}</span>
            <strong>{preset.title}</strong>
            <small>{preset.detail}</small>
            <span className="sf-preset-state">{selected === preset.id ? <><Check size={13} /> 已选择</> : '选择这个方案'}</span>
          </button>
        ))}
      </div>
      <div className="sf-note"><Sparkles size={16} /><span>本片只使用一个方案；需要不同画幅/声音策略时，请在方案选择里显式更换。</span></div>
    </div>
  );
}

function StepThree() {
  return (
    <div className="sf-step-panel">
      <p className="sf-eyebrow">③ 分镜</p>
      <h1>把故事拆成可拍的镜头</h1>
      <p className="sf-lede">每一段都可以独立生成、审核和重做；不要一次面对整部片。</p>
      <div className="sf-shot-list">
        {shots.map((shot, index) => (
          <article className="sf-shot" key={shot.id}>
            <div className="sf-shot-number">{String(index + 1).padStart(2, '0')}</div>
            <Thumb tone={index === 0 ? 'amber' : index === 1 ? 'sage' : 'rain'} label={shot.id} />
            <div className="sf-shot-copy"><div className="sf-shot-head"><strong>{shot.title}</strong><span className="sf-chip">{shot.duration}</span></div><p>{shot.prompt}</p></div>
            <div className="sf-shot-side"><span className={`sf-state ${shot.state === '已采用' ? 'ok' : ''}`}>{shot.state}</span><button className="sf-icon-button" aria-label={`编辑${shot.id}`}><ChevronRight size={15} /></button></div>
          </article>
        ))}
      </div>
      <button className="sf-add-shot"><Plus size={16} /> 添加一个镜头</button>
      <div className="sf-note"><Scissors size={16} /><span>转场和接点不需要现在决定；到装配步骤再逐接点审核。</span></div>
    </div>
  );
}

function StepFour() {
  const [picked, setPicked] = useState<'A' | 'B'>('A');
  return (
    <div className="sf-step-panel">
      <p className="sf-eyebrow">④ 候选</p>
      <h1>逐段做决定</h1>
      <p className="sf-lede">这里只展示候选和事实，不自动选“最佳”。看画面、声音和动作，再决定采用或重做。</p>
      <div className="sf-candidate-row">
        <div className="sf-candidate-main">
          <Thumb tone="amber" label="S01 · 候选 A" film />
          <div className="sf-candidate-meta"><span>1344 × 768 · 4.46s</span><span>1 条音轨</span><span>生成于刚刚</span></div>
        </div>
        <div className="sf-candidate-aside">
          <Thumb tone="sage" label="候选 B" film />
          <p>候选 B 为同方案重做，画面更稳，但声音需要再听。</p>
        </div>
      </div>
      <div className="sf-candidate-actions"><button className={`sf-choice ${picked === 'A' ? 'selected' : ''}`} onClick={() => setPicked('A')}><Check size={16} /><strong>采用候选 A</strong><small>当前版本</small></button><button className={`sf-choice ${picked === 'B' ? 'selected' : ''}`} onClick={() => setPicked('B')}><Check size={16} /><strong>采用候选 B</strong><small>重做版本</small></button><button className="sf-choice"><Wand2 size={16} /><strong>再重做一次</strong><small>保留现有候选</small></button></div>
      <div className="sf-note"><CheckCircle2 size={16} /><span>“采用”只影响这一段，不会自动采用其他镜头或成片。</span></div>
    </div>
  );
}

function StepFive() {
  return (
    <div className="sf-step-panel">
      <p className="sf-eyebrow">⑤ 装配</p>
      <h1>检查，然后生成成片</h1>
      <p className="sf-lede">装配前的决定集中在这一页，避免生成完才发现漏项。</p>
      <div className="sf-checklist">
        <div className="sf-check done"><Check size={16} /><span><strong>3 个镜头已明确采用</strong><small>候选身份与版本已绑定</small></span></div>
        <div className="sf-check done"><Check size={16} /><span><strong>2 个接点已审核</strong><small>S01→S02 · S02→S03</small></span></div>
        <div className="sf-check active"><Volume2 size={16} /><span><strong>音轨方式</strong><small>保留各段原音 · 统一完整音轨 · 全片静音</small></span><select defaultValue="native"><option value="native">保留各段原音</option><option value="master">统一完整音轨</option><option value="mute">全片静音</option></select></div>
        <div className="sf-check done"><Check size={16} /><span><strong>连续性检查通过</strong><small>只读检查，不代替你的艺术判断</small></span></div>
      </div>
      <button className="sf-primary sf-large"><Layers3 size={17} /> 生成成片 <ArrowRight size={16} /></button>
    </div>
  );
}

function StepSix() {
  return (
    <div className="sf-step-panel sf-final">
      <p className="sf-eyebrow">⑥ 交付</p>
      <h1>成片在这里</h1>
      <div className="sf-final-grid">
        <div className="sf-film"><Thumb tone="rain" label="雨停之前" film /><button className="sf-play" aria-label="播放成片"><Play size={22} /></button><span className="sf-film-duration">00:12</span></div>
        <div className="sf-final-meta">
          <span className="sf-chip sf-chip-ok">成片候选</span>
          <h2>雨停之前</h2>
          <p>12.0 秒 · 1344 × 768 · 16:9 · 1 条音轨</p>
          <div className="sf-final-actions"><button className="sf-primary"><Check size={16} /> 通过全片审核</button><button className="sf-secondary"><Download size={16} /> 下载</button></div>
          <button className="sf-secondary sf-wide-button">返回修改</button>
          <p className="sf-fine">下载不等于采用；采用后仍可继续迭代，历史版本会保留。</p>
        </div>
      </div>
    </div>
  );
}

function ContextPanel({ step }: { step: Step }) {
  const content = useMemo(() => {
    if (step === 1) return { title: '本步需要你决定', body: '故事方向、时长、画幅、原创/参考。工作台不会替你补默认值。', items: ['故事一句话', '目标时长', '画幅', '起点'] };
    if (step === 2) return { title: '本步需要你决定', body: '选择一个制作方案。方案只给事实，不推荐，也不自动安装。', items: ['已安装方案', '可用输入', '模型状态'] };
    if (step === 3) return { title: '本步需要你决定', body: '每个镜头的文字、时长和素材。可以只先生成一个镜头。', items: ['3 个镜头', '逐段生成', '可随时保存草稿'] };
    if (step === 4) return { title: '本步需要你决定', body: '逐段比较候选，采用或重做。没有自动“最佳候选”。', items: ['候选 A', '候选 B', '重做'] };
    if (step === 5) return { title: '本步需要你决定', body: '音轨方式和接点审核是装配前最后的人工决定。', items: ['音轨', '接点', '连续性'] };
    return { title: '完成', body: '成片候选需要你最终确认。通过全片审核后才算交付。', items: ['预览', '全片审核', '下载'] };
  }, [step]);
  return (
    <aside className="sf-context">
      <span className="sf-context-kicker">上下文</span>
      <h3>{content.title}</h3>
      <p>{content.body}</p>
      <ul>{content.items.map(item => <li key={item}><span />{item}</li>)}</ul>
      <div className="sf-context-note">工作台是确定性工具：不替你理解、推荐、自动改编或自动采用。</div>
    </aside>
  );
}

function Wizard({ step, onStep, onHome }: { step: Step; onStep: (step: Step) => void; onHome: () => void }) {
  const next = () => onStep(Math.min(6, step + 1) as Step);
  const prev = () => onStep(Math.max(1, step - 1) as Step);
  return (
    <div className="sf-workspace">
      <StepRail step={step} onStep={onStep} />
      <main className="sf-main">
        <div className="sf-workspace-top">
          <button className="sf-back" onClick={onHome}><ChevronLeft size={15} /> 全部作品</button>
          <div className="sf-stepper">{steps.map(item => <span key={item.id} className={item.id === step ? 'active' : item.id < step ? 'done' : ''}>{item.id < step ? <Check size={11} /> : item.id}</span>)}</div>
          <span className="sf-save-state"><span className="sf-dot" /> 已保存</span>
        </div>
        {step === 1 && <StepOne />}
        {step === 2 && <StepTwo />}
        {step === 3 && <StepThree />}
        {step === 4 && <StepFour />}
        {step === 5 && <StepFive />}
        {step === 6 && <StepSix />}
        <div className="sf-bottom-bar">
          <button className="sf-secondary" onClick={prev} disabled={step === 1}><ChevronLeft size={15} /> 上一步</button>
          <span>{step < 6 ? '每一步都可以保存后离开' : '确认后完成交付'}</span>
          {step < 6 ? <button className="sf-primary" onClick={next}>继续 <ChevronRight size={15} /></button> : <button className="sf-primary" onClick={onHome}><Check size={15} /> 完成</button>}
        </div>
      </main>
      <ContextPanel step={step} />
    </div>
  );
}

export default function StudioFlowPrototype() {
  const [screen, setScreen] = useState<'home' | 'intent' | 'wizard' | 'quick'>('home');
  const [step, setStep] = useState<Step>(1);
  const [quickTool, setQuickTool] = useState<QuickTool>('text-video');
  const openQuick = (tool: QuickTool) => { setQuickTool(tool); setScreen('quick'); };
  const start = () => { setScreen('intent'); };
  const openWizard = () => { setStep(1); setScreen('wizard'); };
  return (
    <div className="sf-root">
      <TopBar onHome={() => setScreen('home')} />
      {screen === 'home' && <Home onStart={start} onQuick={openQuick} onOpen={() => openWizard()} />}
      {screen === 'intent' && <Intent onClose={() => setScreen('home')} onFull={openWizard} onQuick={openQuick} />}
      {screen === 'quick' && <QuickPanel tool={quickTool} onBack={() => setScreen('home')} onFull={openWizard} />}
      {screen === 'wizard' && <Wizard step={step} onStep={setStep} onHome={() => setScreen('home')} />}
      <span className="sf-prototype-badge">UI 原型 · 仅用于评审，不连接真实项目</span>
    </div>
  );
}
