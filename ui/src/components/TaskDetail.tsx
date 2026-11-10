import { useEffect, useState } from "react";
import type { ApprovalView, RunView, TaskView } from "../api/types";
import { formatDuration, formatRelative } from "../lib/format";
import {
  effectStateLabel,
  effectStateTone,
  isOpenApproval,
  outcomeLabel,
  outcomeTone,
  runStateLabel,
  runStateTone,
  taskStatusLabel,
  workspaceModeLabel,
} from "../lib/humanize";
import { IconFolder, IconInfo, IconPlay } from "./Icons";
import { EmptyState, StatusPill } from "./StatusPill";

export interface TaskDetailProps {
  task: TaskView;
  runs: RunView[];
  approvals: ApprovalView[];
  selectedRunId: string | null;
  busy: string | null;
  now: number;
  onSelectRun: (runId: string) => void;
  onStartRun: (taskId: string) => void;
  onRevise: (taskId: string, goal: string) => Promise<void>;
  onRequestCancel: (run: RunView) => void;
}

export function TaskDetail({
  task,
  runs,
  approvals,
  selectedRunId,
  busy,
  now,
  onSelectRun,
  onStartRun,
  onRevise,
  onRequestCancel,
}: TaskDetailProps) {
  const [editing, setEditing] = useState(false);
  const [draftGoal, setDraftGoal] = useState(task.goal);
  const [reviseError, setReviseError] = useState<string | null>(null);

  useEffect(() => {
    setDraftGoal(task.goal);
    setEditing(false);
    setReviseError(null);
  }, [task.task_id, task.goal_revision, task.goal]);

  const pendingApprovals = approvals.filter(
    (approval) => approval.task_id === task.task_id && isOpenApproval(approval.state),
  );
  const activeRun = task.active_run_id
    ? runs.find((run) => run.run_id === task.active_run_id) ?? null
    : null;
  const starting = busy === `start-run:${task.task_id}`;
  const saving = busy === `revise:${task.task_id}`;

  const submitRevision = async (): Promise<void> => {
    const nextGoal = draftGoal.trim();
    if (nextGoal === "") {
      setReviseError("目标不能为空。");
      return;
    }
    setReviseError(null);
    try {
      await onRevise(task.task_id, nextGoal);
      setEditing(false);
    } catch (caught) {
      setReviseError(caught instanceof Error ? caught.message : "修改目标失败，请重试。");
    }
  };

  return (
    <section className="pane detail-pane">
      <div className="pane-head">
        <div className="pane-heading">
          <h2>任务详情</h2>
          <span className="subtle">更新于 {formatRelative(task.updated_at ?? task.created_at, now)}</span>
        </div>
        <div className="head-actions">
          {activeRun === null ? (
            <button
              type="button"
              className="btn btn-primary btn-sm"
              onClick={() => onStartRun(task.task_id)}
              disabled={starting}
              title="让 Agent 在后台开始一次新的运行"
            >
              <IconPlay size={14} />
              {starting ? "正在启动…" : "开始运行"}
            </button>
          ) : (
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              onClick={() => onRequestCancel(activeRun)}
              disabled={busy === "control:cancel"}
              title="停止当前运行（已经生效的修改可以在运行结束后撤销）"
            >
              停止运行
            </button>
          )}
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            onClick={() => {
              setDraftGoal(task.goal);
              setReviseError(null);
              setEditing((previous) => !previous);
            }}
            disabled={saving}
          >
            {editing ? "取消修改" : "修改目标"}
          </button>
        </div>
      </div>

      <div className="pane-scroll detail-scroll">
        <section className="detail-block">
          <div className="section-title">目标</div>
          {editing ? (
            <form
              className="revise-form"
              onSubmit={(event) => {
                event.preventDefault();
                void submitRevision();
              }}
            >
              <textarea
                className="textarea"
                rows={3}
                value={draftGoal}
                autoFocus
                onChange={(event) => setDraftGoal(event.target.value)}
                disabled={saving}
              />
              {activeRun !== null ? (
                <p className="warn-text">
                  保存后，当前正在进行的运行会被停止（目标已经变了，旧运行不能继续按旧目标做事）。
                </p>
              ) : null}
              {reviseError !== null ? <p className="form-error">{reviseError}</p> : null}
              <div className="form-actions">
                <button type="button" className="btn btn-ghost btn-sm" onClick={() => setEditing(false)} disabled={saving}>
                  取消
                </button>
                <button type="submit" className="btn btn-primary btn-sm" disabled={saving}>
                  {saving ? "正在保存…" : "保存目标"}
                </button>
              </div>
            </form>
          ) : (
            <p className="goal-text">{task.goal}</p>
          )}
          <div className="meta-row">
            <StatusPill tone={runStateTone(task.status)} dot>
              {taskStatusLabel(task.status)}
            </StatusPill>
            {activeRun !== null ? (
              <span className="subtle">当前运行：{runStateLabel(activeRun.state)}</span>
            ) : (
              <span className="subtle">当前没有进行中的运行</span>
            )}
            {pendingApprovals.length > 0 ? (
              <span className="approval-hint">
                <IconInfo size={13} />
                {pendingApprovals.length} 项操作等待你批准
              </span>
            ) : null}
          </div>
        </section>

        <section className="detail-block">
          <div className="section-title">
            绑定目录
            <span className="section-hint">决定 Agent 能读写哪里；目录外的访问会单独请求你批准</span>
          </div>
          {task.workspaces.length === 0 ? (
            <p className="subtle">没有绑定目录，这个任务不能修改任何文件。</p>
          ) : (
            <ul className="workspace-list">
              {task.workspaces.map((workspace, index) => (
                <li key={`${workspace.root_path}-${index}`}>
                  <span className="workspace-icon">
                    <IconFolder size={14} />
                  </span>
                  <span className="workspace-path mono" title={workspace.root_path}>
                    {workspace.root_path}
                  </span>
                  <StatusPill tone={workspace.mode === "writable" ? "warn" : "neutral"}>
                    {workspaceModeLabel(workspace.mode)}
                  </StatusPill>
                </li>
              ))}
            </ul>
          )}
        </section>

        <section className="detail-block">
          <div className="section-title">
            运行历史
            <span className="count">{runs.length}</span>
          </div>
          {runs.length === 0 ? (
            <EmptyState
              title="还没有运行记录"
              hint="点右上角「开始运行」，Agent 会在后台推进这个任务。"
            />
          ) : (
            <ul className="run-list">
              {runs.map((run, index) => (
                <li key={run.run_id}>
                  <button
                    type="button"
                    className={`run-item${selectedRunId === run.run_id ? " is-selected" : ""}`}
                    onClick={() => onSelectRun(run.run_id)}
                    aria-current={selectedRunId === run.run_id ? "true" : undefined}
                  >
                    <span className="run-item-main">
                      <span className="run-item-title">
                        <StatusPill tone={runStateTone(run.state)} dot>
                          {runStateLabel(run.state)}
                        </StatusPill>
                        <span className="run-index">第 {runs.length - index} 次运行</span>
                        {run.control_requested !== null ? (
                          <span className="subtle">已请求处理</span>
                        ) : null}
                      </span>
                      <span className="run-item-meta">
                        <span>{formatRelative(run.started_at, now)}</span>
                        <span>·</span>
                        <span>{formatDuration(run.started_at, run.ended_at)}</span>
                        {run.outcome !== null ? (
                          <StatusPill tone={outcomeTone(run.outcome)}>{outcomeLabel(run.outcome)}</StatusPill>
                        ) : null}
                        {run.effect_state !== null ? (
                          <span className="run-effect">
                            <span className={`effect-dot tone-${effectStateTone(run.effect_state)}`} aria-hidden="true" />
                            {effectStateLabel(run.effect_state)}
                          </span>
                        ) : null}
                        {run.undoable ? <span className="undo-mark">可撤销</span> : null}
                      </span>
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>
    </section>
  );
}