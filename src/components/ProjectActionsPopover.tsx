import type { InputHTMLAttributes, RefObject } from 'react';
import { ArrowUpRight, X } from 'lucide-react';
import { creationRoutes } from '../CreationRoutes';

type ProjectSummary = { id: string; title: string; series?: string };

type Props = {
  mode: 'create' | 'import';
  newSeries: string;
  newTitle: string;
  newCreationMode: string;
  newReferenceProject: string;
  projectList: ProjectSummary[];
  creatingProject: boolean;
  importFiles: File[];
  hasProject: boolean;
  folderInputRef: RefObject<HTMLInputElement | null>;
  onSeriesChange: (value: string) => void;
  onTitleChange: (value: string) => void;
  onCreationModeChange: (value: string) => void;
  onReferenceProjectChange: (value: string) => void;
  onImportFiles: (files: File[]) => void;
  onCreate: () => void;
  onUpload: () => void;
  onClose: () => void;
};

export default function ProjectActionsPopover({
  mode, newSeries, newTitle, newCreationMode, newReferenceProject, projectList, creatingProject,
  importFiles, hasProject, folderInputRef, onSeriesChange, onTitleChange, onCreationModeChange,
  onReferenceProjectChange, onImportFiles, onCreate, onUpload, onClose,
}: Props) {
  return <section className="director-top-popover" aria-label={mode === 'create' ? '新建作品' : '导入素材'}>
    {mode === 'create' ? <div className="director-mini-form">
      <strong>新建作品</strong>
      <label>系列名称<input value={newSeries} onChange={event => onSeriesChange(event.target.value)} placeholder="例如：家庭短片" /></label>
      <label>作品名称<input value={newTitle} onChange={event => onTitleChange(event.target.value)} placeholder="例如：第一集" /></label>
      <label>创作起点<select value={newCreationMode} onChange={event => onCreationModeChange(event.target.value)}>{creationRoutes.map(route => <option key={route.id} value={route.id}>{route.title}</option>)}</select></label>
      {newCreationMode === 'reference-series' && <label>沿用作品设定<select value={newReferenceProject} onChange={event => onReferenceProjectChange(event.target.value)}><option value="">手动填写系列设定</option>{projectList.map(item => <option key={item.id} value={item.id}>{item.series} · {item.title}</option>)}</select></label>}
      <button className="button primary compact" disabled={creatingProject || !newSeries.trim() || !newTitle.trim()} onClick={onCreate}>{creatingProject ? '正在创建…' : '创建并进入'}</button>
    </div> : <div className="director-import">
      <strong>导入工作目录</strong>
      <input ref={folderInputRef} className="visually-hidden-file" type="file" multiple {...({ webkitdirectory: '' } as unknown as InputHTMLAttributes<HTMLInputElement>)} onChange={event => onImportFiles(Array.from(event.target.files ?? []))} />
      <button className="button secondary compact" onClick={() => folderInputRef.current?.click()}><ArrowUpRight size={13} />选择文件夹</button>
      <span>{importFiles.length ? `已选择 ${importFiles.length} 个文件` : '未选择文件夹'}</span>
      <button className="button primary compact" disabled={!hasProject || !importFiles.length} onClick={onUpload}>导入当前作品</button>
    </div>}
    <button className="icon-button" aria-label="关闭面板" onClick={onClose}><X size={16} /></button>
  </section>;
}
