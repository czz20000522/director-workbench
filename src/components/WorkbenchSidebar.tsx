/** Workspace sidebar shell: brand, project tree, connection links and the drag resizer.
 *
 * Extracted from `App.tsx`; it owns no state. The parent keeps the session, project list
 * and sidebar width, and passes callbacks so menu/selection coordination stays in one place.
 */
import type { PointerEvent as ReactPointerEvent } from 'react';
import { Clapperboard, FolderOpen, Settings2 } from 'lucide-react';
import WorkspaceExplorer from '../WorkspaceExplorer';
import type { TreeKind } from '../shotLabels';
import type { PlanSegment } from '../app/types';

type ProjectSummary = { id: string; title: string; series?: string; selected?: boolean };
type Archive = { series: string; work: string };

type Props = {
  projects: ProjectSummary[];
  archive: Archive | null;
  projectId?: string;
  planSegments: PlanSegment[];
  assets: Array<Record<string, unknown>>;
  selectedKind: TreeKind;
  selectedId: string;
  privateMode: boolean;
  refreshKey: number;
  onOpenProject: (projectId: string, item?: { kind: TreeKind; id: string }) => void;
  onOpenItem: (kind: TreeKind, id: string) => void;
  onOpenArchive: (archive: Archive | null) => void;
  onCreateWork: (series: string) => void;
  onDeleteDirectory: (target: { series: string; work?: string; projectId?: string; title?: string; resource?: boolean }) => void;
  onRefreshProjects: () => void;
  comfyOnline: boolean;
  comfyVersion?: string;
  onOpenMaterialBridge: () => void;
  onToggleResources: () => void;
  sidebarWidth: number;
  onBeginResize: (event: ReactPointerEvent<HTMLDivElement>) => void;
  onResize: (next: number) => void;
};

export default function WorkbenchSidebar({
  projects, archive, projectId, planSegments, assets, selectedKind, selectedId, privateMode, refreshKey,
  onOpenProject, onOpenItem, onOpenArchive, onCreateWork, onDeleteDirectory, onRefreshProjects,
  comfyOnline, comfyVersion, onOpenMaterialBridge, onToggleResources,
  sidebarWidth, onBeginResize, onResize,
}: Props) {
  return <>
    <aside className="director-sidebar">
      <div className="director-brand"><span className="director-brand-mark"><Clapperboard size={20} /></span><div><strong>片场</strong><small>导演工作台</small></div></div>
      <WorkspaceExplorer projects={projects} activeProjectId={archive ? undefined : projectId} activeSegments={planSegments} activeAssets={assets} selectedKind={selectedKind} selectedId={selectedId} onOpenProject={onOpenProject} onOpenItem={onOpenItem} privateMode={privateMode} refreshKey={refreshKey} onOpenArchive={onOpenArchive} onCreateWork={onCreateWork} onDeleteDirectory={onDeleteDirectory} onRefreshProjects={onRefreshProjects} />
      <div className="director-sidebar-bottom"><div className="director-connection"><span className={comfyOnline ? 'online' : ''} />{comfyOnline ? `ComfyUI ${comfyVersion ?? '已连接'}` : '引擎待启动'}</div><button className="director-resource-link" onClick={onOpenMaterialBridge}><FolderOpen size={14} />跨设备素材桥</button><button className="director-resource-link" onClick={onToggleResources}><Settings2 size={14} />资源与连接</button></div>
    </aside>
    <div className="director-sidebar-resizer" role="separator" aria-label="调整作品目录宽度" aria-orientation="vertical" aria-valuemin={240} aria-valuemax={Math.floor(window.innerWidth / 2)} aria-valuenow={sidebarWidth} tabIndex={0} onPointerDown={onBeginResize} onKeyDown={event => { if (event.key === 'ArrowLeft') onResize(sidebarWidth - 24); if (event.key === 'ArrowRight') onResize(sidebarWidth + 24); }} />
  </>;
}
