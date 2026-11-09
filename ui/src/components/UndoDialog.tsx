import type { UndoChange } from "../api/types";
import { shortenPath } from "../lib/format";
import { Modal } from "./Modal";
import { IconUndo } from "./Icons";

export interface UndoPlanState {
  runId: string;
  changes: UndoChange[];
}

export interface UndoDialogProps {
  plan: UndoPlanState;
  busy: boolean;
  error: string | null;
  onConfirm: () => void;
  onClose: () => void;
}

function changeKindLabel(kind: string): string {
  if (kind === "restore") return "恢复原内容";
  if (kind === "delete") return "删除本次新建的文件";
  return "未知类型的改动";
}

export function UndoDialog({ plan, busy, error, onConfirm, onClose }: UndoDialogProps) {
  const hasChanges = plan.changes.length > 0;

  return (
    <Modal
      title="撤销这次运行的修改"
      subtitle="只处理下面列出的文件。撤销会改动工作区，请确认后再执行。"
      onClose={onClose}
      closeDisabled={busy}
      footer={
        <>
          <span className="modal-foot-hint">
            {hasChanges ? `共 ${plan.changes.length} 处文件会被改动。` : "没有需要撤销的修改。"}
          </span>
          <span className="modal-foot-actions">
            <button type="button" className="btn btn-ghost" onClick={onClose} disabled={busy}>
              取消
            </button>
            <button type="button" className="btn btn-primary" onClick={onConfirm} disabled={busy || !hasChanges}>
              <IconUndo size={14} />
              {busy ? "正在撤销…" : "确认撤销"}
            </button>
          </span>
        </>
      }
    >
      {hasChanges ? (
        <table className="undo-table">
          <thead>
            <tr>
              <th>文件</th>
              <th>处理方式</th>
            </tr>
          </thead>
          <tbody>
            {plan.changes.map((change, index) => (
              <tr key={`${change.path}-${index}`}>
                <td>
                  <div className="undo-path mono" title={change.path}>
                    {shortenPath(change.path, 78)}
                  </div>
                  {change.reason !== undefined && change.reason !== null && change.reason !== "" ? (
                    <div className="undo-reason">{change.reason}</div>
                  ) : null}
                </td>
                <td className="undo-kind">{changeKindLabel(change.kind)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className="subtle">守护进程表示这次运行没有可撤销的修改。</p>
      )}
      {error !== null ? <div className="form-error">{error}</div> : null}
    </Modal>
  );
}