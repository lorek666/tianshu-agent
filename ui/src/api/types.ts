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
  /** 控制面任务（人签字产生的记忆写等）：不是用户任务，界面不显示。 */
  control: boolean;
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

export interface IsolationView {
  backend: string;
  declared: string;
  measured: string | null;
  level: string;
  unverified: boolean;
  contradicted: boolean;
  boundaries: Record<string, string>;
  enforced_boundaries: string[];
}

export interface ApprovalView {
  approval_id: string;
  run_id: string;
  task_id: string;
  action: string;
  resources: string[];
  reason: string | null;
  rule_id: string | null;
  /** F2：审批前可见的隔离视图；null = 这条动作不涉及执行隔离。 */
  isolation: IsolationView | null;
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
  // 记忆控制面（G2 记忆阶段 M1）：UI 是人机界面入口，与模型工具面是两条路。
  memoryList: "memory.list",
  memorySearch: "memory.search",
  memoryGet: "memory.get",
  memoryRemember: "memory.remember",
  memoryConfirm: "memory.confirm",
  memoryForget: "memory.forget",
  memoryErase: "memory.erase",
} as const;
// ---------------------------------------------------------------- 记忆（M1）

/** 记忆可见作用域：默认 `user`（人自己的记忆），其余要显式点名。 */
export interface MemoryScopeChoice {
  scope: "user" | "task" | "workspace";
  scope_id: string | null;
}

/** 一条记忆对界面投影的六项判据 + 可选正文/排序解释。 */
export interface MemoryView {
  memory_id: string;
  revision: number;
  scope: string;
  scope_id: string | null;
  kind: string;
  content_ref: string | null;
  origin: string;
  status: string;
  source_refs: string[];
  proof_count: number;
  layer: string;
  erased_at: number | null;
  created_at: number | null;
  updated_at: number | null;
  content: string | null;
  content_state: string | null;
  score: number | null;
  score_kind: string | null;
  /** 三态：true 陈旧 / false 已确认新鲜 / undefined 不可判定（后端省略=判不了）。 */
  stale: boolean | undefined;
}

export interface MemoryListResult {
  items: MemoryView[];
  scopes: string[];
}

export interface MemorySearchResult extends MemoryListResult {
  query: string;
  /** 这一路到底有没有语义能力：false 不是"没找到"，是"只有词面召回"。 */
  semantic: boolean;
  /** `semantic=false` 的原因（未配置 provider / 索引未构建 / provider 不匹配）。 */
  degraded: string | null;
  fused: boolean;
  limit: number | null;
}
