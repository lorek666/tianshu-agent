import type { HostClient } from "./host";
import type { RunExecutionPolicyV1, RunView, TaskView } from "./types";
import { normalizeRun } from "./normalize";
import { asRecord } from "../lib/values";
import { humanizeHostError } from "./errors";

export const GOAL_STATUSES = [
  "ready", "running", "awaiting_review", "exhausted", "blocked", "stale",
  "completed", "cancelled",
] as const;
export type GoalStatus = typeof GOAL_STATUSES[number];
export interface GoalConfig {
  criteria: string[];
  max_rounds: number;
}
export interface GoalView extends GoalConfig {
  format_version: 1;
  goal_id: string;
  task_id: string;
  goal_revision: number;
  execution_policy: RunExecutionPolicyV1;
  rounds_started: number;
  latest_run_id: string | null;
  status: GoalStatus;
  confirmed_criteria: number[];
}
export interface GoalsApi {
  get(taskId: string): Promise<GoalView | null>;
  create(task: TaskView, config: GoalConfig, policy: RunExecutionPolicyV1, requestId: string): Promise<GoalView>;
  advance(goalId: string, requestId: string): Promise<{ goal: GoalView; run: RunView }>;
  close(goalId: string, outcome: "completed" | "cancelled", confirmed: number[], requestId: string): Promise<GoalView>;
}

function goalView(raw: unknown): GoalView {
  const v = asRecord(raw);
  const policy = asRecord(v.execution_policy);
  if (v.format_version !== 1 || typeof v.goal_id !== "string" || typeof v.task_id !== "string"
      || !Number.isInteger(v.goal_revision) || !Number.isInteger(v.max_rounds)
      || !Number.isInteger(v.rounds_started) || !GOAL_STATUSES.includes(v.status as GoalStatus)
      || !Array.isArray(v.criteria) || !v.criteria.every((c) => typeof c === "string")
      || !Array.isArray(v.confirmed_criteria) || !v.confirmed_criteria.every(Number.isInteger)
      || !(v.latest_run_id === null || typeof v.latest_run_id === "string")
      || policy.format_version !== 1 || !["effects", "pure_only"].includes(String(policy.mode))) {
    throw new Error("目标数据格式无效，请更新天枢并刷新后重试。");
  }
  return v as unknown as GoalView;
}

export function criteriaFromText(value: string): string[] {
  const criteria = value.split("\n").map((line) => line.trim()).filter(Boolean);
  if (criteria.length < 1 || criteria.length > 12
      || criteria.some((line) => new TextEncoder().encode(line).length > 1024)) {
    throw new Error("请填写 1–12 条验收条件，每行一条，每条不超过 1024 UTF-8 字节。");
  }
  return criteria;
}

export function createGoalsApi(host: HostClient): GoalsApi {
  async function call(method: string, params: Record<string, unknown>) {
    try {
      return asRecord(await host.rpc(method, params));
    } catch (error) {
      throw new Error(humanizeHostError(error));
    }
  }
  return {
    async get(taskId) {
      const result = await call("goal.get", { task_id: taskId });
      return result.goal === null ? null : goalView(result.goal);
    },
    async create(task, config, executionPolicy, requestId) {
      const result = await call("goal.create", {
        task_id: task.task_id, goal_revision: task.goal_revision, ...config,
        execution_policy: executionPolicy, request_id: requestId,
      });
      return goalView(result.goal);
    },
    async advance(goalId, requestId) {
      const result = await call("goal.advance", { goal_id: goalId, request_id: requestId });
      const run = normalizeRun(result.run);
      if (run === null) throw new Error("目标运行数据格式无效，请刷新后重试。");
      return { goal: goalView(result.goal), run };
    },
    async close(goalId, outcome, confirmed, requestId) {
      const result = await call("goal.close", {
        goal_id: goalId, outcome, confirmed_criteria: confirmed, request_id: requestId,
      });
      return goalView(result.goal);
    },
  };
}
