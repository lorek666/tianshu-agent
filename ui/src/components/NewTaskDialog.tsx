import { useState } from "react";
import type { CreateTaskInput, WorkspaceMode } from "../api/types";
import { Modal } from "./Modal";
import { IconPlus, IconX } from "./Icons";

export interface NewTaskDialogProps {
  open: boolean;
  busy: boolean;
  onClose: () => void;
  onSubmit: (input: CreateTaskInput) => Promise<void>;
}

interface WorkspaceDraft {
  root_path: string;
  mode: WorkspaceMode;
}

const DEFAULT_WORKSPACE: WorkspaceDraft = { root_path: "", mode: "writable" };

export function NewTaskDialog({ open, busy, onClose, onSubmit }: NewTaskDialogProps) {
  const [goal, setGoal] = useState("");
  const [workspaces, setWorkspaces] = useState<WorkspaceDraft[]>([{ ...DEFAULT_WORKSPACE }]);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  if (!open) return null;

  const updateRow = (index: number, patch: Partial<WorkspaceDraft>) => {
    setWorkspaces((previous) =>
      previous.map((row, rowIndex) => (rowIndex === index ? { ...row, ...patch } : row)),
    );
  };

  const removeRow = (index: number) => {
    setWorkspaces((previous) => {
      if (previous.length <= 1) return previous;
      return previous.filter((_, rowIndex) => rowIndex !== index);
    });
  };

  const handleSubmit = async (): Promise<void> => {
    const trimmedGoal = goal.trim();
    if (trimmedGoal === "") {
      setError("请用一句话描述你想让 Agent 做什么。");
      return;
    }
    const validWorkspaces = workspaces
      .map((row) => ({ ...row, root_path: row.root_path.trim() }))
      .filter((row) => row.root_path !== "");
    if (validWorkspaces.length === 0) {
      setError("至少绑定一个目录，Agent 才能读写文件。");
      return;
    }
    setError(null);
    setSubmitting(true);
    try {
      await onSubmit({ goal: trimmedGoal, workspaces: validWorkspaces });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "创建任务失败，请重试。");
    } finally {
      setSubmitting(false);
    }
  };

  const pending = busy || submitting;

  return (
    <Modal
      title="新建任务"
      subtitle="用一句话说明目标，并指定 Agent 可以读写的目录。删除类操作仍会单独请求你批准。"
      onClose={onClose}
      closeDisabled={pending}
      wide
      footer={
        <>
          <span className="modal-foot-hint">任务创建后会立刻在后台开始第一次运行。</span>
          <span className="modal-foot-actions">
            <button type="button" className="btn btn-ghost" onClick={onClose} disabled={pending}>
              取消
            </button>
            <button type="button" className="btn btn-primary" onClick={() => void handleSubmit()} disabled={pending}>
              {pending ? "正在创建…" : "创建并开始运行"}
            </button>
          </span>
        </>
      }
    >
      <form
        className="form"
        onSubmit={(event) => {
          event.preventDefault();
          void handleSubmit();
        }}
      >
        <label className="field">
          <span className="field-label">目标</span>
          <textarea
            className="textarea"
            rows={3}
            value={goal}
            autoFocus
            placeholder="例如：把「下载」里 2024 年的账单归档到「账单归档」，再生成一份本月摘要。"
            onChange={(event) => setGoal(event.target.value)}
            disabled={pending}
          />
          <span className="field-hint">写得具体一点，Agent 更容易做对；之后也可以修改目标。</span>
        </label>

        <div className="field">
          <span className="field-label">绑定目录</span>
          <div className="workspace-editor">
            {workspaces.map((row, index) => (
              <div className="workspace-row" key={index}>
                <input
                  className="input mono"
                  value={row.root_path}
                  placeholder="绝对路径，例如 /Users/you/Documents/项目"
                  onChange={(event) => updateRow(index, { root_path: event.target.value })}
                  disabled={pending}
                  aria-label={`第 ${index + 1} 个目录路径`}
                />
                <div className="segmented" role="group" aria-label={`第 ${index + 1} 个目录权限`}>
                  <button
                    type="button"
                    className={`segmented-item${row.mode === "readonly" ? " is-active" : ""}`}
                    onClick={() => updateRow(index, { mode: "readonly" })}
                    disabled={pending}
                  >
                    只读
                  </button>
                  <button
                    type="button"
                    className={`segmented-item${row.mode === "writable" ? " is-active" : ""}`}
                    onClick={() => updateRow(index, { mode: "writable" })}
                    disabled={pending}
                  >
                    可写
                  </button>
                </div>
                <button
                  type="button"
                  className="icon-btn"
                  aria-label="移除这个目录"
                  title="移除这个目录"
                  onClick={() => removeRow(index)}
                  disabled={pending || workspaces.length <= 1}
                >
                  <IconX size={14} />
                </button>
              </div>
            ))}
            <button
              type="button"
              className="link-btn"
              onClick={() => setWorkspaces((previous) => [...previous, { ...DEFAULT_WORKSPACE }])}
              disabled={pending}
            >
              <IconPlus size={13} />
              再加一个目录
            </button>
          </div>
          <span className="field-hint">
            只读：只能查看；可写：允许修改和新建。改动会记录在运行快照里，运行结束后可以在「运行详情」里撤销。
          </span>
        </div>

        {error !== null ? (
          <div className="form-error" role="alert">
            {error}
          </div>
        ) : null}
      </form>
    </Modal>
  );
}