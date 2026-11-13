import { useMemo, useState } from 'react';
import { Check, Cpu, Info, LockKeyhole } from 'lucide-react';

/** 单步视频工具的方案选择器。
 *
 * 原来是一个 17 项的下拉：技术名混着「0.4MP + Kitchen」「待小样验证」，
 * 而且图生视频里也列着一半纯文字方案，选了必 422。
 * 现在按「你手头有什么」分组，一次只显示一组，选中前先把需要准备的素材讲清楚。
 */
export type VideoPreset = {
  id: string; title: string; available?: boolean; reason?: string;
  aspect_ratio?: '16:9' | '9:16' | '1:1';
  group?: 'first' | 'audio' | 'frames' | 'text';
  needs?: string[]; summary?: string; badge?: string; caveat?: string;
  speed?: 'fast' | 'standard'; missing_models?: string[];
  /** 这套方案默认走不走 8 步 Turbo 分支；界面据此初始化「渲染档位」。 */
  turbo?: boolean; steps?: number;
  /** 方案的原生采样档（百万像素）：0.4≈480p、0.6≈768p，界面据此初始化原生档按钮。 */
  megapixels?: number;
  description?: string;
};

type GroupKey = NonNullable<VideoPreset['group']>;

export type HardwareSummary = {
  key: string; label: string; gpu_name: string; vram_gib: number; compute_cap: string; source: string;
  video_megapixels: number; fast_megapixels: number; quality_megapixels: number;
  steps_standard: number; steps_fast: number; steps_quality: number; reserve_vram_gb: number;
};

const IMAGE_GROUPS: { key: GroupKey; label: string; note: string }[] = [
  { key: 'first', label: '只要一张开始画面', note: '最省事，声音由模型自己生成' },
  { key: 'audio', label: '还要一段表演音频', note: '用声音带动作或对口型' },
  { key: 'frames', label: '还要一张结束画面', note: '控制动作从哪里到哪里' },
];
const TEXT_GROUPS: { key: GroupKey; label: string; note: string }[] = [
  { key: 'text', label: '只写文字', note: '不用准备任何图片' },
];

const groupOf = (preset: VideoPreset): GroupKey => preset.group || 'first';

export default function VideoPresetPicker({ presets, mode, value, onChange, disabled, hardware }: {
  presets: VideoPreset[]; mode: 'image-video' | 'text-video';
  value: string; onChange: (id: string) => void; disabled?: boolean;
  hardware?: HardwareSummary | null;
}) {
  const groups = mode === 'image-video' ? IMAGE_GROUPS : TEXT_GROUPS;
  const [active, setActive] = useState<GroupKey>(groups[0].key);
  // 选好之后把整列收起来：六张卡片有 900px 高，会把真正要填的「开始画面」顶到屏幕外。
  const [expanded, setExpanded] = useState(false);
  // 切工具时分组列表会变；保证 active 始终在当前模式里，否则一个卡片都不显示。
  const safeGroup = groups.some(item => item.key === active) ? active : groups[0].key;
  const counts = useMemo(() => {
    const result: Record<string, number> = {};
    for (const preset of presets) {
      const key = groupOf(preset);
      result[key] = (result[key] || 0) + 1;
    }
    return result;
  }, [presets]);
  const shown = presets.filter(preset => groupOf(preset) === safeGroup);
  const selected = presets.find(preset => preset.id === value);
  const collapsed = Boolean(selected) && !expanded;

  const card = (preset: VideoPreset) => {
    const usable = preset.available !== false;
    const chosen = preset.id === value;
    return <button key={preset.id} type="button"
      className={`vpp-card${chosen ? ' chosen' : ''}${usable ? '' : ' blocked'}`}
      aria-pressed={chosen} disabled={disabled || !usable}
      onClick={() => { onChange(preset.id); setExpanded(false); }}>
      <span className="vpp-card-top">
        <span className="vpp-card-name">{preset.title}</span>
        {preset.badge && <span className={`vpp-badge${preset.speed === 'fast' ? ' fast' : ''}`}>{preset.badge}</span>}
        {chosen && <Check size={15} className="vpp-check" />}
      </span>
      <span className="vpp-card-summary">{preset.summary || preset.description}</span>
      <span className="vpp-card-tags">
        {preset.aspect_ratio && <span>{preset.aspect_ratio}</span>}
        {(preset.needs || []).map(need => <span key={need}>{need}</span>)}
        {preset.steps ? <span>{preset.steps} 步{preset.turbo ? ' · Turbo' : ''}</span> : null}
        {preset.speed === 'fast' && <span>较快</span>}
      </span>
      {preset.caveat && <span className="vpp-caveat"><Info size={11} /> {preset.caveat}</span>}
      {!usable && <span className="vpp-reason"><LockKeyhole size={11} />
        {preset.reason || `缺少 ${(preset.missing_models || []).length} 个模型，补齐后才能用`}
      </span>}
    </button>;
  };

  return <div className="vpp">
    <div className="vpp-head">
      <span className="vpp-title">制作方案 · 一次只选一套</span>
      {selected
        ? <span className="vpp-chosen"><Check size={12} /> 已选：{selected.title}</span>
        : <span className="vpp-unselected-tag">还没选</span>}
    </div>
    <p className="vpp-explain">
      下面三个标签是<b>筛选条件</b>——按「你手头有什么素材」把方案分堆，不用选满三个。
      一次出片只用一套方案，它就是这次的配方：画幅、步数、声音模式都由它决定。
      想比较两套，就各跑一次，再用任务上的「编辑重生成」换方案重跑。
    </p>
    {hardware && <p className="vpp-hardware"><Cpu size={12} />
      本机档位：{hardware.gpu_name} · {hardware.vram_gib} GiB · {hardware.key}
      ｜ 标准 {hardware.video_megapixels}MP / 快速 {hardware.fast_megapixels}MP / 精细 {hardware.steps_quality} 步
      {hardware.source === 'fallback' && ' · 未探测到显卡，按主流档兜底'}
    </p>}

    {collapsed ? <div className="vpp-collapsed">
      {card(selected as VideoPreset)}
      <button type="button" className="vpp-switch" disabled={disabled} onClick={() => setExpanded(true)}>
        换方案（共 {presets.length} 个可选）
      </button>
    </div> : <>
      <div className="vpp-tabs" role="group" aria-label="按要准备的素材筛选方案（单选，只影响看到哪些方案）">
        {groups.map(group => <button key={group.key} type="button" disabled={disabled}
          aria-pressed={safeGroup === group.key}
          onClick={() => setActive(group.key)}>
          {group.label}<span>{counts[group.key] || 0}</span>
        </button>)}
      </div>
      <p className="vpp-group-note">正在看：{groups.find(item => item.key === safeGroup)?.note}（换标签只是换一屏，不会改变已选方案）</p>

      <div className="vpp-list">
        {shown.map(card)}
        {!shown.length && <p className="vpp-empty">这一组暂时没有可用方案。</p>}
      </div>

      {selected
        ? <p className="vpp-footnote">
            {selected.needs?.length ? `「${selected.title}」需要：${selected.needs.join('、')}。` : `「${selected.title}」不需要额外素材。`}
            {mode === 'image-video' ? ' 这里只列需要开始画面的方案；纯文字方案在文生视频里。' : ''}
          </p>
        : <p className="vpp-footnote vpp-unselected">还没选方案：上面每张卡片点一下就选中，选中的那张会带勾。</p>}
      {selected && <button type="button" className="vpp-switch" disabled={disabled} onClick={() => setExpanded(false)}>收起，只留已选的这套</button>}
    </>}
  </div>;
}
