/** Host 返回值 → 界面内部形状。契约字段照抄，缺失/类型不对时给安全默认值。 */

import type {
  ApprovalView,
  DaemonView,
  EventView,
  IsolationView,
  RunView,
  StateView,
  TaskView,
  UndoChange,
  WorkspaceView,
} from "./types";
import { asArray, asBoolean, asNumber, asRecord, asString, readString } from "../lib/values";

function normalizeWorkspace(raw: unknown): WorkspaceView | null {
  if (typeof raw === "string") {
    const root = raw.trim();
    return root === "" ? null : { root_path: root, mode: "readonly" };
  }
  const record = asRecord(raw);
  const root = readString(record, "root_path", "root", "path");
  if (root === null) return null;
  return { root_path: root, mode: readString(record, "mode") ?? "readonly" };
}

export function normalizeTask(raw: unknown): TaskView | null {
  const record = asRecord(raw);
  const taskId = readString(record, "task_id", "id");
  if (taskId === null) return null;
  return {
    task_id: taskId,
    goal: asString(record.goal) ?? "",
    goal_revision: asNumber(record.goal_revision) ?? 1,
    status: asString(record.status) ?? "unknown",
    active_run_id: readString(record, "active_run_id"),
    workspaces: asArray(record.workspaces)
      .map(normalizeWorkspace)
      .filter((workspace): workspace is WorkspaceView => workspace !== null),
    created_at: asNumber(record.created_at),
    updated_at: asNumber(record.updated_at),
  };
}

export function normalizeRun(raw: unknown): RunView | null {
  const record = asRecord(raw);
  const runId = readString(record, "run_id", "id");
  const taskId = readString(record, "task_id");
  if (runId === null || taskId === null) return null;
  return {
    run_id: runId,
    task_id: taskId,
    state: asString(record.state) ?? "unknown",
    outcome: readString(record, "outcome"),
    effect_state: readString(record, "effect_state"),
    control_requested: readString(record, "control_requested"),
    goal_revision: asNumber(record.goal_revision) ?? 0,
    lease_epoch: asNumber(record.lease_epoch) ?? 0,
    started_at: asNumber(record.started_at),
    ended_at: asNumber(record.ended_at),
    n_applied: asNumber(record.n_applied) ?? 0,
    n_unknown: asNumber(record.n_unknown) ?? 0,
    undoable: asBoolean(record.undoable) ?? false,
  };
}

function normalizeResources(raw: unknown): string[] {
  const resources: string[] = [];
  for (const item of asArray(raw)) {
    if (typeof item === "string") {
      if (item.trim() !== "") resources.push(item);
      continue;
    }
    const record = asRecord(item);
    const path = readString(record, "path", "file_path", "target", "name");
    if (path !== null) resources.push(path);
  }
  return resources;
}

function normalizeIsolation(raw: unknown): IsolationView | null {
  const record = asRecord(raw);
  // 没有这个字段 = 非 exec 动作 / 旧事件；**字段存在但读不懂**不能当成
  // "没有隔离需要说明"——那是把畸形视图隐藏掉。保留下面的未知视图，让 UI
  // 显示"隔离情况未知"。
  if (Object.keys(record).length === 0) return null;
  return {
    backend: asString(record.backend) ?? "",
    declared: readString(record, "declared") ?? "none",
    measured: readString(record, "measured"),
    level: readString(record, "level") ?? "",
    // 缺字段时按更保守的方向默认：没有明确"已实测"一律当未验证。
    unverified: asBoolean(record.unverified) ?? true,
    contradicted: asBoolean(record.contradicted) ?? false,
    boundaries: Object.fromEntries(
      Object.entries(asRecord(record.boundaries)).filter(
        (entry): entry is [string, string] => typeof entry[1] === "string",
      ),
    ),
    enforced_boundaries: asArray(record.enforced_boundaries).filter(
      (item): item is string => typeof item === "string",
    ),
  };
}

export function normalizeApproval(raw: unknown): ApprovalView | null {
  const record = asRecord(raw);
  const approvalId = readString(record, "approval_id", "id");
  if (approvalId === null) return null;
  return {
    approval_id: approvalId,
    run_id: asString(record.run_id) ?? "",
    task_id: asString(record.task_id) ?? "",
    action: asString(record.action) ?? "",
    resources: normalizeResources(record.resources),
    reason: readString(record, "reason"),
    rule_id: readString(record, "rule_id"),
    isolation: normalizeIsolation(record.isolation),
    state: asString(record.state) ?? "unknown",
    created_at: asNumber(record.created_at),
    expires_at: asNumber(record.expires_at),
  };
}

function normalizePayload(raw: unknown): Record<string, unknown> {
  if (typeof raw === "object" && raw !== null && !Array.isArray(raw)) {
    return raw as Record<string, unknown>;
  }
  if (typeof raw === "string") {
    try {
      const parsed: unknown = JSON.parse(raw);
      if (typeof parsed === "object" && parsed !== null && !Array.isArray(parsed)) {
        return parsed as Record<string, unknown>;
      }
    } catch {
      // payload 不是 JSON 字符串：按空对象处理，事件句子会走兜底。
    }
  }
  return {};
}

export function normalizeEvent(raw: unknown): EventView | null {
  const record = asRecord(raw);
  const seq = asNumber(record.seq);
  const type = asString(record.type);
  if (seq === null || type === null) return null;
  return {
    seq,
    type,
    run_id: readString(record, "run_id"),
    task_id: readString(record, "task_id"),
    ts: asNumber(record.ts) ?? 0,
    actor_kind: asString(record.actor_kind) ?? "system",
    payload: normalizePayload(record.payload),
  };
}

export function normalizeEvents(raw: unknown): EventView[] {
  return asArray(raw)
    .map(normalizeEvent)
    .filter((event): event is EventView => event !== null);
}

function normalizeDaemon(raw: unknown): DaemonView {
  const record = asRecord(raw);
  return {
    // Host 实际字段是 daemon_version（src/tianshu/control.py 的 _daemon_view）；
    // 兼容早期写成 version 的投影。
    version: readString(record, "daemon_version", "version"),
    started_at: asNumber(record.started_at),
    data_root: readString(record, "data_root"),
  };
}

export function normalizeState(raw: unknown): StateView {
  const record = asRecord(raw);
  return {
    tasks: asArray(record.tasks)
      .map(normalizeTask)
      .filter((task): task is TaskView => task !== null),
    runs: asArray(record.runs)
      .map(normalizeRun)
      .filter((run): run is RunView => run !== null),
    approvals: asArray(record.approvals)
      .map(normalizeApproval)
      .filter((approval): approval is ApprovalView => approval !== null),
    event_seq_head: asNumber(record.event_seq_head) ?? 0,
    daemon: normalizeDaemon(record.daemon),
  };
}
/** 撤销计划：兼容 `{changes: [...]}` 与裸数组；缺 path 的条目丢弃。 */
export function normalizeUndoChanges(raw: unknown): UndoChange[] {
  const outer = asRecord(raw);
  const list = Array.isArray(outer.changes) ? outer.changes : Array.isArray(raw) ? raw : [];
  const changes: UndoChange[] = [];
  for (const item of list) {
    const record = asRecord(item);
    const path = readString(record, "path", "file_path", "target");
    if (path === null) continue;
    changes.push({
      path,
      kind: readString(record, "kind") ?? "unknown",
      reason: readString(record, "reason"),
    });
  }
  return changes;
}
