/** Workbench top chrome: quick-workspace switch, section navigation and account actions.
 *
 * Extracted from `App.tsx`; it owns no state. The parent keeps the navigation state and
 * passes one callback per destination so open menus/drafts are coordinated in one place.
 */
import { ChevronRight, LogOut } from 'lucide-react';
import { QUICK_SERIES } from '../app/helpers';

export type WorkspacePage = 'creative' | 'script' | 'shots' | 'delivery' | 'enhance';

type Props = {
  project?: { series?: string; title?: string } | null;
  archive: { series: string; work: string } | null;
  privateMode: boolean;
  quickShell: boolean;
  quickCategory: 'general' | 'adult';
  workspaceMode: 'production' | 'reverse';
  workspacePage: WorkspacePage;
  comfyOnline: boolean;
  logoutBusy: boolean;
  sessionError: string;
  onLogout?: () => void;
  onQuickGenerate: () => void;
  onWorkspace: () => void;
  onNavigate: (page: WorkspacePage) => void;
  onOpenReverse: () => void;
  onToggleCreate: () => void;
  onToggleImport: () => void;
};

export default function WorkbenchTopbar({
  project, archive, privateMode, quickShell, quickCategory, workspaceMode, workspacePage,
  comfyOnline, logoutBusy, sessionError, onLogout, onQuickGenerate, onWorkspace,
  onNavigate, onOpenReverse, onToggleCreate, onToggleImport,
}: Props) {
  return <>
    <div className="quick-navigation"><button className="text-button" onClick={onQuickGenerate}>快速生成</button><button className="text-button" onClick={onWorkspace}>作品、片段与更多功能</button><span>{project ? `新产物会存进：「${project.series || '我的作品'}」/ ${project.title}` : `还没有作品；生成时会先新建到「${QUICK_SERIES[quickCategory]}」`}</span>{privateMode && quickShell && <button className="text-button" onClick={onLogout} disabled={logoutBusy}>退出登录</button>}</div>
    <header className="director-topbar"><div className="director-breadcrumb"><span>{archive?.series ?? project?.series ?? '我的作品'}</span><ChevronRight size={13} /><strong>{archive?.work ?? project?.title ?? '选择或新建作品'}</strong></div><nav className="director-topnav" aria-label="工作台功能"><button className={!archive && workspaceMode === 'production' && workspacePage === 'script' ? 'active' : ''} onClick={() => onNavigate('script')}>剧本与场次</button><button className={!archive && workspaceMode === 'production' && workspacePage === 'shots' ? 'active' : ''} onClick={() => onNavigate('shots')}>分镜制作</button><button className={!archive && workspaceMode === 'production' && workspacePage === 'creative' ? 'active' : ''} onClick={() => onNavigate('creative')}>创作设定</button><button className={!archive && workspaceMode === 'reverse' ? 'active' : ''} onClick={onOpenReverse}>参考拆解</button><button className={!archive && workspaceMode === 'production' && workspacePage === 'delivery' ? 'active' : ''} onClick={() => onNavigate('delivery')}>任务与成片</button></nav><div className="director-topbar-status"><span className={comfyOnline ? 'online-dot' : 'offline-dot'} />{comfyOnline ? '引擎已连接' : '引擎离线'}</div><button className="button primary compact" onClick={onToggleCreate}>新建作品</button><button className="button secondary compact" onClick={onToggleImport}>导入素材</button>{privateMode && <button className="director-logout" onClick={onLogout} disabled={logoutBusy} title={sessionError || '退出登录'}><LogOut size={14} />退出登录</button>}</header>
  </>;
}
