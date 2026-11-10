/**
 * Host ↔ 界面的数据契约。
 *
 * 字段以 `docs/PRD-v1.md` M5 定稿的视图为准；界面不发明后端没有的字段。
 * 机器枚举（state / outcome / effect_state / action / rule_id）只在折叠详情
 * 里原样出现，主界面一律经过 `lib/humanize.ts` 翻成人话。
 */

export type WorkspaceMode = "readonly" | "writable";

export interface WorkspaceView {
  root_path: string;
  /** 契约是 readonly / writable；保留 string 是为了未知值不崩、有兜底文案。 */
  mode: WorkspaceMode | string;
}

export interface TaskView {
  task_id: string;
  goal: string;
  goal_revision: number;
  status: string;
  active_run_id: string | null;
  workspaces: WorkspaceView[];
  created_at: number | null;
  updated_at: number | null;
}

export interface RunView {
  run_id: string;
  task_id: string;
  state: string;
  outcome: string | null;
  effect_state: string | null;
  control_requested: string | null;
  goal_revision: number;
  lease_epoch: number;
  started_at: number | null;
  ended_at: number | null;
  n_applied: number;
  n_unknown: number;
  undoable: boolean;
}

export interface ApprovalView {
  approval_id: string;
  run_id: string;
  task_id: string;
  action: string;
  resources: string[];
  reason: string | null;
  rule_id: string | null;
  state: string;
  created_at: number | null;
  expires_at: number | null;
}

export interface EventView {
  seq: number;
  type: string;
  run_id: string | null;
  task_id: string | null;
  ts: number;
  actor_kind: string;
  payload: Record<string, unknown>;
}

export interface DaemonView {
  version: string | null;
  started_at: number | null;
  data_root: string | null;
}

export interface StateView {
  tasks: TaskView[];
  runs: RunView[];
  approvals: ApprovalView[];
  event_seq_head: number;
  daemon: DaemonView;
}

export interface EventsResult {
  events: EventView[];
  seq_head: number;
  has_more: boolean;
}

export interface TaskEnvelope {
  task: TaskView;
}

export interface RunEnvelope {
  run: RunView;
}

export interface TasksEnvelope {
  tasks: TaskView[];
}

export interface RunsEnvelope {
  runs: RunView[];
}

export interface ApprovalsEnvelope {
  approvals: ApprovalView[];
}

export interface ApprovalDecisionResult {
  approval: ApprovalView;
  /** true = 允许后运行被唤醒；false = 没有需要唤醒的运行（例如已结束）。 */
  resumed: boolean;
}

export interface UndoChange {
  path: string;
  /** "restore" = 恢复原内容；"delete" = 删除本次新建的文件。 */
  kind: string;
  reason?: string | null;
}

export interface UndoPlanResult {
  changes: UndoChange[];
}

export interface UndoApplyResult {
  applied: number;
  refused?: string | null;
}

export interface CreateTaskInput {
  goal: string;
  workspaces: WorkspaceView[];
}

/** 界面会调用的方法名。字符串常量集中在这里，避免各处手写拼错。 */
export const RPC = {
  state: "state",
  taskList: "task.list",
  taskCreate: "task.create",
  taskRevise: "task.revise",
  runStart: "run.start",
  runList: "run.list",
  runStatus: "run.status",
  runPause: "run.pause",
  runResume: "run.resume",
  runCancel: "run.cancel",
  approvalsList: "approvals.list",
  approvalsDecide: "approvals.decide",
  eventsRun: "events.run",
  undoPlan: "undo.plan",
  undoApply: "undo.apply",
} as const;