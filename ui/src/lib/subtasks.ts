/**
 * 子任务面板的选择器：从父 Run 的事件流里重建"这个 Run 派了哪些子任务、各自怎么样"。
 *
 * 事实源纪律：`subtasks` 表只是投影（"行是当前结论，事件是当时结论"，
 * src/tianshu/tasks.py），父 Run 事件流里的 `subtask.spawned` / `subtask.finished`
 * 才是 Append-only 的事实。UI 读事件，不发明第二份委派台账。
 *
 * 只有 `subtask.spawned` 没有 `subtask.finished` = 进行中（不冒充终态）；
 * `finished.usage` 缺失 = 用量未知（D1 时代不写这条事件），不当 0。
 */

import type { EventView, RunView } from "../api/types";
import { asRecord, readNumber, readString } from "./values";

export interface SubtaskLimitsView {
  maxTurns: number | null;
  maxTokens: number | null;
  deadlineMs: number | null;
}

export interface SubtaskUsageView {
  turns: number;
  tokens: number;
}

export interface SubtaskRow {
  subtaskId: string;
  childTaskId: string | null;
  childRunId: string | null;
  /** 目标正文不进父 Run（契约只有 sha256）；展示截短摘要，正文在子 Task 里。 */
  goalDigest: string | null;
  limits: SubtaskLimitsView | null;
  /** `subtask.finished` 的 state；null = 只有 spawned（进行中）。 */
  finishedState: string | null;
  resultRef: string | null;
  error: string | null;
  usage: SubtaskUsageView | null;
  spawnedSeq: number;
  finishedSeq: number | null;
  /** `childRunId` 能在 runs 里对上时的子 Run 状态；对不上为 null（不猜）。 */
  childRunState: string | null;
}

function readLimits(event: EventView): SubtaskLimitsView | null {
  const payload = asRecord(event?.payload);
  const raw = asRecord(payload.limits);
  if (raw === null) return null;
  const maxTurns = readNumber(raw, "max_turns", "maxTurns");
  const maxTokens = readNumber(raw, "max_tokens", "maxTokens");
  const deadlineMs = readNumber(raw, "deadline_ms", "deadlineMs");
  if (maxTurns === null && maxTokens === null && deadlineMs === null) return null;
  return { maxTurns, maxTokens, deadlineMs };
}

function readUsage(event: EventView): SubtaskUsageView | null {
  const payload = asRecord(event?.payload);
  const usage = asRecord(payload.usage);
  if (usage === null) return null;
  const turns = readNumber(usage, "turns");
  const tokens = readNumber(usage, "tokens");
  if (turns === null && tokens === null) return null;
  return {
    turns: turns ?? 0,
    tokens: tokens ?? 0,
  };
}

/**
 * 父 Run 事件流 + state 里已有的 Run 列表 → 子任务行（按派发顺序升序）。
 * 没有任何委派事件时返回空数组（面板整个不渲染）。
 */
export function buildSubtaskViews(events: EventView[], runs: RunView[]): SubtaskRow[] {
  const runStateById = new Map<string, string>();
  for (const run of Array.isArray(runs) ? runs : []) {
    if (typeof run?.run_id === "string" && typeof run.state === "string") {
      runStateById.set(run.run_id, run.state);
    }
  }

  const byId = new Map<string, SubtaskRow>();
  const order: string[] = [];

  for (const event of Array.isArray(events) ? events : []) {
    const type = typeof event?.type === "string" ? event.type : "";
    const payload = asRecord(event?.payload);
    if (type === "subtask.spawned") {
      const subtaskId = readString(payload, "subtask_id");
      if (subtaskId === null) continue;
      const existing = byId.get(subtaskId);
      if (existing !== undefined && existing.spawnedSeq !== 0) continue;
      const childRunId = readString(payload, "child_run_id");
      if (existing !== undefined) {
        // 异常序列（finished 先到）建过占位行：spawned 迟到时把派发事实补进去，
        // 已读到的收尾结论保留。
        existing.childTaskId = readString(payload, "child_task_id");
        existing.childRunId = childRunId;
        existing.goalDigest = readString(payload, "goal_digest");
        existing.limits = readLimits(event) ?? existing.limits;
        existing.spawnedSeq =
          typeof event?.seq === "number" && Number.isFinite(event.seq) ? event.seq : 0;
        existing.childRunState =
          childRunId !== null ? runStateById.get(childRunId) ?? null : null;
        continue;
      }
      const row: SubtaskRow = {
        subtaskId,
        childTaskId: readString(payload, "child_task_id"),
        childRunId,
        goalDigest: readString(payload, "goal_digest"),
        limits: readLimits(event),
        finishedState: null,
        resultRef: null,
        error: null,
        usage: null,
        spawnedSeq: typeof event?.seq === "number" && Number.isFinite(event.seq) ? event.seq : 0,
        finishedSeq: null,
        childRunState: childRunId !== null ? runStateById.get(childRunId) ?? null : null,
      };
      byId.set(subtaskId, row);
      order.push(subtaskId);
      continue;
    }
    if (type === "subtask.finished") {
      const subtaskId = readString(payload, "subtask_id");
      const row = subtaskId !== null ? byId.get(subtaskId) : undefined;
      if (row === undefined) {
        // finished 先于 spawned 不该发生（收尾必然在派发之后）；真遇到就按未知
        // 委派建一行占位，不静默丢事件。
        if (subtaskId === null) continue;
        const ghost: SubtaskRow = {
          subtaskId,
          childTaskId: null,
          childRunId: null,
          goalDigest: null,
          limits: null,
          finishedState: readString(payload, "state") ?? "unknown",
          resultRef: null,
          error: null,
          usage: null,
          spawnedSeq: 0,
          finishedSeq: typeof event?.seq === "number" && Number.isFinite(event.seq) ? event.seq : 0,
          childRunState: null,
        };
        byId.set(subtaskId, ghost);
        order.push(subtaskId);
        continue;
      }
      row.finishedState = readString(payload, "state") ?? "unknown";
      row.finishedSeq =
        typeof event?.seq === "number" && Number.isFinite(event.seq) ? event.seq : 0;
      row.resultRef = readString(payload, "result_ref");
      row.error = readString(payload, "error");
      row.usage = readUsage(event);
    }
  }

  return order
    .map((id) => byId.get(id)!)
    .sort((left, right) => left.spawnedSeq - right.spawnedSeq || left.subtaskId.localeCompare(right.subtaskId));
}
