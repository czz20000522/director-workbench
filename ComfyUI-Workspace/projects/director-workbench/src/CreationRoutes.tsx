import { ArrowUpRight, BookOpen, Clapperboard, FileText, Search } from 'lucide-react';

export const creationRoutes = [
  { id: 'script', title: '已有剧本', description: '接收剧本，整理场次，再安排画面与声音。', icon: FileText },
  { id: 'original', title: '从无到有', description: '从想法、角色和世界观开始，编排分镜与声音。', icon: Clapperboard },
  { id: 'reference-video', title: '参考视频复刻', description: '导入参考，校订拆解结果，再转为自己的分镜草案。', icon: Search },
  { id: 'reference-series', title: '参考系列创作', description: '记录系列规则、角色与表达方式，设计这一集的内容。', icon: BookOpen },
];

export default function CreationRoutes({ mode, segmentCount, onMode, onReference, onSettings, onSegment, onStage, onScript }: {
  mode: string; segmentCount: number; onMode: (mode: string) => void;
  onReference: () => void; onSettings: () => void; onSegment: () => void; onStage: () => void; onScript: () => void;
}) {
  const selected = creationRoutes.find(route => route.id === mode) ?? creationRoutes.find(route => route.id === 'original')!;
  return <section className="creation-routes" aria-label="创作与复刻入口">
    <details open>
      <summary><strong>创作与复刻 · {selected.title}</strong><span>{segmentCount ? `${segmentCount} 个分镜 · 可继续调整创作方式` : '选择这部作品的起点'}</span></summary>
      <div className="creation-route-options" role="group" aria-label="创作方式">
        {creationRoutes.map(route => <button key={route.id} aria-pressed={selected.id === route.id} className={selected.id === route.id ? 'selected' : ''} onClick={() => onMode(route.id)}>
          <route.icon size={20} /><strong>{route.title}</strong><span>{route.description}</span>
        </button>)}
      </div>
      <p className="creation-route-hint">{selected.id === 'script' ? '剧本讲述故事，场次组织情节；一个场次可以安排多个分镜，镜头时长单独设置。' : selected.id === 'reference-video' ? '拆解结果先作为参考；转入后补充本作角色、关键帧和制作方案，不会自动开始生成。' : selected.id === 'reference-series' ? '先填写参考系列和本集的保留项、变化项；可用素材桥登记角色与声音参考，也可拆解代表视频。' : '没有参考也可以开始：保存创作设定，添加分镜，再准备每段的画面与声音。'}</p>
    </details>
    <div className="creation-route-actions">
      <button className={`button ${selected.id === 'script' ? 'primary' : 'secondary'} compact`} onClick={onScript}><FileText size={14} />剧本与场次</button>
      {selected.id === 'reference-video' && <button className="button primary compact" onClick={onReference}><Search size={14} />导入 / 查看参考拆解</button>}
      <button className={`button ${selected.id === 'reference-video' ? 'secondary' : 'primary'} compact`} onClick={onSettings}>{selected.id === 'reference-series' ? '编辑系列与本集设定' : '编辑创作设定'}</button>
      <button className="button secondary compact" onClick={onSegment}>+ 添加分镜</button>
      <button className="button secondary compact" onClick={onStage}>制作步骤</button>
      {selected.id === 'reference-series' && <button className="text-button" onClick={onReference}>拆解代表视频 <ArrowUpRight size={13} /></button>}
    </div>
  </section>;
}
