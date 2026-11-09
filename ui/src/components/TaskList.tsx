import type { ApprovalView, RunView, TaskView } from "../api/types";
import { formatRelative } from "../lib/format";
import { isOpenApproval, runStateTone, taskGoalSummary, taskStatusLabel } from "../lib/humanize";
import { IconPlus } from "./Icons";
import { EmptyState, StatusPill } from "./StatusPill";

export interface TaskListProps {
  tasks: TaskView[];
  runs: RunView[];
  approvals: ApprovalView[];
  selectedTaskId: string | null;
  now: number;
  onSelectTask: (taskId: string) => void;
  onNewTask: () => void;
}

export function TaskList({
  tasks,
  runs,
  approvals,
  selectedTaskId,
  now,
  onSelectTask,
  onNewTask,
}: TaskListProps) {
  return (
    <aside className="pane tasks-pane">
      <div className="pane-head">
        <div className="pane-heading">
          <h2>任务</h2>
          <span className="count">{tasks.length}</span>
        </div>
        <button type="button" className="btn btn-primary btn-sm" onClick={onNewTask}>
          <IconPlus size={14} />
          新建任务
        </button>
      </div>
      <div className="pane-scroll">
        {tasks.length === 0 ? (
          <EmptyState
            title="还没有任务"
            hint="用一句话说明你想让 Agent 做什么，再绑定它允许读写的目录。"
            action={
              <button type="button" className="btn btn-primary btn-sm" onClick={onNewTask}>
                新建第一个任务
              </button>
            }
          />
        ) : (
          <ul className="task-list">
            {tasks.map((task) => {
              const pending = approvals.filter(
                (approval) => approval.task_id === task.task_id && isOpenApproval(approval.state),
              ).length;
              const activeRun = task.active_run_id
                ? runs.find((run) => run.run_id === task.active_run_id) ?? null
                : null;
              const updatedAt = task.updated_at ?? task.created_at;
              return (
                <li key={task.task_id}>
                  <button
                    type="button"
                    className={`task-item${selectedTaskId === task.task_id ? " is-selected" : ""}`}
                    onClick={() => onSelectTask(task.task_id)}
                    aria-current={selectedTaskId === task.task_id ? "true" : undefined}
                  >
                    <span className={`task-state-dot tone-${runStateTone(task.status)}`} aria-hidden="true" />
                    <span className="task-item-body">
                      <span className="task-goal">{taskGoalSummary(task, 58)}</span>
                      <span className="task-item-meta">
                        <StatusPill tone={runStateTone(task.status)}>{taskStatusLabel(task.status)}</StatusPill>
                        {activeRun !== null ? (
                          <span className="task-run-hint">运行中</span>
                        ) : null}
                        {pending > 0 ? (
                          <span className="task-approval-hint">{pending} 项待批准</span>
                        ) : null}
                        <span className="task-time">{formatRelative(updatedAt, now)}</span>
                      </span>
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </div>
      <div className="pane-footnote">任务状态由守护进程持有，界面关闭后运行仍在后台继续。</div>
    </aside>
  );
}