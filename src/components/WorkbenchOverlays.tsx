/** Floating overlays: toast, delete confirmations and the image lightbox.
 *
 * Extracted from `App.tsx`; the parent owns the state and the dialog ref, this component
 * only renders the current state and forwards the confirm/close callbacks.
 */
import type { RefObject } from 'react';
import { Check, X } from 'lucide-react';
import DeleteDirectoryDialog from '../DeleteDirectoryDialog';

export type DirectoryTarget = { series: string; work?: string; projectId?: string; title?: string; resource?: boolean };

type Props = {
  notice: string;
  deleteDirectory: DirectoryTarget | null;
  onCloseDeleteDirectory: () => void;
  onDeletedDirectory: () => void;
  deleteShotOpen: boolean;
  deleteShotId?: string;
  deletingShot: boolean;
  onCloseDeleteShot: () => void;
  onConfirmDeleteShot: () => void;
  dialogRef: RefObject<HTMLDialogElement | null>;
  lightbox: boolean;
  onCloseLightbox: () => void;
  current: { id: string; title: string; image?: string };
};

export default function WorkbenchOverlays({
  notice, deleteDirectory, onCloseDeleteDirectory, onDeletedDirectory,
  deleteShotOpen, deleteShotId, deletingShot, onCloseDeleteShot, onConfirmDeleteShot,
  dialogRef, lightbox, onCloseLightbox, current,
}: Props) {
  return <>
    {notice && <div className="director-toast" role="status"><Check size={16} /><span>{notice}</span></div>}
    {deleteDirectory && <DeleteDirectoryDialog target={deleteDirectory} onClose={onCloseDeleteDirectory} onDeleted={onDeletedDirectory} />}
    {deleteShotOpen && deleteShotId && <div className="director-delete-backdrop" role="presentation" onMouseDown={event => { if (event.target === event.currentTarget) onCloseDeleteShot(); }}><section className="director-delete-dialog" role="dialog" aria-modal="true" aria-label="删除分镜"><h2>删除分镜 {deleteShotId}？</h2><p>会从本作品的制作计划移除这一段，并重排后续时间轴。生成记录与可能被其他分镜共用的素材仍保留。</p><div className="director-delete-actions"><button type="button" className="button secondary compact" onClick={onCloseDeleteShot}>取消</button><button type="button" className="button danger compact" disabled={deletingShot} onClick={onConfirmDeleteShot}>{deletingShot ? '删除中…' : '删除分镜'}</button></div></section></div>}
    <dialog ref={dialogRef} className="lightbox" onCancel={onCloseLightbox}><div><span>{current.id} · {current.title}</span><button className="icon-button" onClick={onCloseLightbox} aria-label="关闭大图"><X size={22} /></button></div>{lightbox && <img src={current.image} alt={current.title} />}</dialog>
  </>;
}
