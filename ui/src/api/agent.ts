import type { HostClient } from "./host";
import type { GoalConfig } from "./goals";
import type { RunExecutionPolicyV1, TaskView } from "./types";
import { asRecord } from "../lib/values";

export const AGENT_STATUSES = [
  "ready", "active", "awaiting_input", "awaiting_review", "paused", "blocked",
  "completed", "cancelled", "stale",
] as const;
export type AgentStatus = typeof AGENT_STATUSES[number];
const BLOCKERS = [
  "verification_failed", "execution_failed", "policy_denied", "approval_denied",
  "budget_exhausted", "deadline_exceeded", "round_limit", "no_progress",
  "isolation_unavailable", "effect_unknown", "usage_unknown", "protocol_error", "recovery_required",
];
const STEP_STATES = [
  "pending", "ready", "running", "verifying", "satisfied", "failed", "blocked", "invalidated", "skipped",
];
export type RecoveryResolution = "retry" | "replan" | "reconciled" | "new_observation" | "change_decision";
export interface AgentGoalDraft extends GoalConfig {
  mode?: "manual" | "bounded_auto";
  max_requests?: number;
  deadline_minutes?: number;
  max_no_progress_rounds?: number;
}
export interface GoalBlocker {
  category: string;
  source_event_seq: number;
  safe_retry: boolean;
  available_actions: string[];
  denied_content_digest: string | null;
}
export interface RoundAssessment {
  round: number;
  target_criterion_ids: string[];
  satisfied_criterion_ids: string[];
  completed_step_ids: string[];
  remaining_step_ids: string[];
  evidence_refs: string[];
  blocker: GoalBlocker | null;
}
export interface AgentGoalView {
  format_version: 2;
  goal_id: string;
  task_id: string;
  config: {
    goal_revision: number;
    mission_digest: string;
    mode: "manual" | "bounded_auto";
    completion_policy: "human" | "verified";
    replan_policy: "confirm" | "bounded";
    max_rounds: number;
    deadline_ms: number;
    budget_limits: Record<string, number>;
    max_no_progress_rounds: number;
    execution_policy: RunExecutionPolicyV1;
  };
  status: AgentStatus;
  phase: "plan" | "execute" | "verify" | "ask" | "replan" | "finish";
  round: number;
  state_revision: number;
  orchestration_epoch: number;
  phase_run_id: string | null;
  execution_run_id: string | null;
  verification_run_id: string | null;
  plan_id: string | null;
  plan_digest: string | null;
  step_id: string | null;
  no_progress_streak: number;
  satisfied_criterion_ids: string[];
  completed_step_ids: string[];
  blocker: GoalBlocker | null;
  assessments: RoundAssessment[];
  pause_pending: boolean;
}
export interface PlanStep {
  step_id: string;
  title: string;
  instruction: string;
  depends_on: string[];
  criterion_ids: string[];
  expected_artifacts: { artifact_id: string; kind: string; locator: string }[];
  verification_ids: string[];
  state: string;
  evidence_refs: string[];
  reason_code: string;
}
export interface PlanCheck {
  check_id: string;
  criterion_ids: string[];
  kind: "file_exists" | "file_hash" | "command_exit" | "source_reference" | "human";
  required: true;
  inputs: Record<string, unknown>;
}
export interface PlanView {
  format_version: 1;
  plan_id: string;
  task_id: string;
  revision: number;
  state_revision: number;
  document_digest: string;
  confirmed: boolean;
  document: Record<string, unknown>;
  manifest: { checks: PlanCheck[] };
  mission: {
    goal_revision: number;
    criteria: { criterion_id: string; description: string; mode: "machine" | "human" }[];
  };
  steps: PlanStep[];
}
export interface VerificationRequest {
  format_version: 1;
  plan_id: string;
  revision: number;
  step_id: string;
  check_id: string;
  check_digest: string;
  attempt_id: string;
  input_fingerprint: string;
  evidence_refs: string[];
  request_id: string;
}
export interface PlanGraph {
  plan: PlanView;
  verification_requests: VerificationRequest[];
}
export interface ConsentPreview {
  task_id: string;
  run_id: string;
  goal_revision: number;
  workspace_binding_digest: string;
  mission_digest: string;
  plan_digest: string;
  action: "overwrite" | "exec";
  tool_binding: Record<string, unknown>;
  matcher: Record<string, unknown>;
}
export interface ScopedConsent extends ConsentPreview {
  consent_id: string;
  max_uses: number;
  expires_at: number;
  state: "active" | "consumed" | "revoked" | "expired";
  remaining_uses: number;
  used_bytes: number;
}
export type ConsentTarget =
  | { run_id: string; kind: "directory_overwrite"; directory: string; recursive: boolean }
  | { run_id: string; kind: "exact_command"; request_id: string };

export interface AgentApi {
  getGoal(taskId: string): Promise<AgentGoalView | null>;
  createGoal(task: TaskView, draft: AgentGoalDraft, policy: RunExecutionPolicyV1, requestId: string): Promise<AgentGoalView>;
  controlGoal(goal: AgentGoalView, action: "advance" | "pause" | "resume" | "close" | "recover",
    requestId: string, extra?: Record<string, unknown>): Promise<AgentGoalView>;
  getPlan(planId: string): Promise<PlanGraph>;
  confirmPlan(plan: PlanView, requestId: string): Promise<PlanGraph>;
  verifyStep(plan: PlanView, stepId: string, requestId: string): Promise<void>;
  requestHuman(plan: PlanView, stepId: string, checkId: string, requestId: string): Promise<void>;
  decideHuman(plan: PlanView, request: VerificationRequest, missionDigest: string,
    decision: "accept" | "reject", reason: string, requestId: string): Promise<void>;
  material(taskId: string, ref: string): Promise<unknown>;
  prepareConsent(target: ConsentTarget): Promise<ConsentPreview>;
  issueConsent(preview: ConsentPreview, uses: number, expiresAt: number, requestId: string): Promise<ScopedConsent>;
  listConsents(runId: string): Promise<ScopedConsent[]>;
  revokeConsent(consentId: string, requestId: string): Promise<ScopedConsent>;
}

function invalid(): never {
  throw new Error("任务控制数据格式无效，请更新天枢并刷新后重试。");
}
function strings(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string");
}
function integer(value: unknown): value is number {
  return Number.isSafeInteger(value) && Number(value) >= 0;
}
function nullableString(value: unknown) {
  return value === null || typeof value === "string";
}
function blockerValid(raw: unknown) {
  if (raw === null) return true;
  const value = asRecord(raw);
  return BLOCKERS.includes(String(value.category)) && integer(value.source_event_seq)
    && typeof value.safe_retry === "boolean" && strings(value.available_actions)
    && nullableString(value.denied_content_digest);
}
function goalView(raw: unknown): AgentGoalView {
  const v = asRecord(raw);
  const c = asRecord(v.config);
  if (v.format_version !== 2 || typeof v.goal_id !== "string" || typeof v.task_id !== "string"
      || !AGENT_STATUSES.includes(v.status as AgentStatus)
      || !["plan", "execute", "verify", "ask", "replan", "finish"].includes(String(v.phase))
      || !integer(v.state_revision) || !integer(v.orchestration_epoch) || !integer(v.round)
      || !integer(c.goal_revision) || typeof c.mission_digest !== "string"
      || !["manual", "bounded_auto"].includes(String(c.mode))
      || !["human", "verified"].includes(String(c.completion_policy))
      || !["confirm", "bounded"].includes(String(c.replan_policy))
      || !integer(c.max_rounds) || !integer(c.deadline_ms)
      || !integer(c.max_no_progress_rounds) || !integer(v.no_progress_streak)
      || !["pure_only", "effects"].includes(String(asRecord(c.execution_policy).mode))
      || !Object.values(asRecord(c.budget_limits)).every(integer)
      || !blockerValid(v.blocker)
      || !["phase_run_id", "execution_run_id", "verification_run_id", "plan_id", "plan_digest", "step_id"]
        .every((field) => nullableString(v[field]))
      || !strings(v.satisfied_criterion_ids) || !strings(v.completed_step_ids)
      || !Array.isArray(v.assessments) || typeof v.pause_pending !== "boolean") invalid();
  for (const rawAssessment of v.assessments) {
    const assessment = asRecord(rawAssessment);
    if (!integer(assessment.round) || !blockerValid(assessment.blocker)
        || !["target_criterion_ids", "satisfied_criterion_ids", "completed_step_ids",
          "remaining_step_ids", "evidence_refs"].every((key) => strings(assessment[key]))) invalid();
  }
  return v as unknown as AgentGoalView;
}
function graphView(raw: unknown): PlanGraph {
  const v = asRecord(raw);
  const p = asRecord(v.plan);
  const m = asRecord(p.manifest);
  const mission = asRecord(p.mission);
  if (p.format_version !== 1 || typeof p.plan_id !== "string" || typeof p.task_id !== "string"
      || !integer(p.revision) || !integer(p.state_revision) || typeof p.document_digest !== "string"
      || typeof p.confirmed !== "boolean" || !Array.isArray(p.steps) || !Array.isArray(m.checks)
      || !integer(mission.goal_revision) || !Array.isArray(mission.criteria)
      || !Array.isArray(v.verification_requests)) invalid();
  for (const criterion of mission.criteria) {
    const item = asRecord(criterion);
    if (typeof item.criterion_id !== "string" || typeof item.description !== "string"
        || !["machine", "human"].includes(String(item.mode))) invalid();
  }
  for (const rawStep of p.steps) {
    const step = asRecord(rawStep);
    if (typeof step.step_id !== "string" || typeof step.title !== "string"
        || typeof step.instruction !== "string" || !STEP_STATES.includes(String(step.state))
        || !["depends_on", "criterion_ids", "verification_ids", "evidence_refs"]
          .every((key) => strings(step[key]))
        || !Array.isArray(step.expected_artifacts) || typeof step.reason_code !== "string") invalid();
  }
  for (const check of m.checks) {
    const c = asRecord(check);
    if (!["file_exists", "file_hash", "command_exit", "source_reference", "human"]
      .includes(String(c.kind)) || typeof c.check_id !== "string" || !strings(c.criterion_ids)) invalid();
  }
  for (const rawRequest of v.verification_requests) {
    const request = asRecord(rawRequest);
    if (request.format_version !== 1 || request.plan_id !== p.plan_id || request.revision !== p.revision
        || !["step_id", "check_id", "check_digest", "attempt_id", "input_fingerprint", "request_id"]
          .every((key) => typeof request[key] === "string")
        || !strings(request.evidence_refs)) invalid();
  }
  return v as unknown as PlanGraph;
}
function previewView(raw: unknown): ConsentPreview {
  const v = asRecord(raw);
  const matcher = asRecord(v.matcher);
  if (typeof v.run_id !== "string" || typeof v.task_id !== "string"
      || !integer(v.goal_revision) || typeof v.workspace_binding_digest !== "string"
      || typeof v.mission_digest !== "string" || typeof v.plan_digest !== "string"
      || !["overwrite", "exec"].includes(String(v.action))
      || !["directory_overwrite", "exact_command"].includes(String(matcher.kind))
      || typeof asRecord(v.tool_binding).tool_id !== "string") invalid();
  return v as unknown as ConsentPreview;
}
function consentView(raw: unknown): ScopedConsent {
  const v = asRecord(raw);
  previewView(v);
  if (typeof v.consent_id !== "string"
      || !["active", "consumed", "revoked", "expired"].includes(String(v.state))
      || !integer(v.remaining_uses) || !integer(v.used_bytes) || !integer(v.expires_at)) invalid();
  return v as unknown as ScopedConsent;
}

/** 保留 HostRpcError 供界面区分 CAS 冲突；所有绑定都来自真实控制面。 */
export function createAgentApi(host: HostClient): AgentApi {
  const creations = new Map<string, Record<string, unknown>>();
  async function call(method: string, params: Record<string, unknown>) {
    return asRecord(await host.rpc(method, params));
  }
  return {
    async getGoal(taskId) {
      const result = await call("goal.v2.get", { task_id: taskId });
      return result.goal === null ? null : goalView(result.goal);
    },
    async createGoal(task, draft, policy, requestId) {
      if (!creations.has(requestId)) creations.set(requestId, {
        task_id: task.task_id, goal_revision: task.goal_revision, criteria: draft.criteria,
        execution_policy: policy, mode: draft.mode ?? "manual",
        completion_policy: "human", replan_policy: "confirm", max_rounds: draft.max_rounds,
        deadline_ms: Date.now() + (draft.deadline_minutes ?? 30) * 60_000,
        budget_limits: { requests: draft.max_requests ?? 32 }, budget_bound: "estimate",
        max_no_progress_rounds: draft.max_no_progress_rounds ?? 2, isolation_requirements: [],
        request_id: requestId,
      });
      return goalView((await call("goal.v2.create", creations.get(requestId)!)).goal);
    },
    async controlGoal(goal, action, requestId, extra = {}) {
      return goalView((await call(`goal.v2.${action}`, {
        goal_id: goal.goal_id, expected_state_revision: goal.state_revision,
        ...(action === "advance" ? { orchestration_epoch: goal.orchestration_epoch } : {}),
        ...extra, request_id: requestId,
      })).goal);
    },
    async getPlan(planId) {
      return graphView(await call("plan.graph.get", { plan_id: planId }));
    },
    async confirmPlan(plan, requestId) {
      return graphView(await call("plan.graph.confirm", {
        plan_id: plan.plan_id, expected_revision: plan.revision,
        document_digest: plan.document_digest, request_id: requestId,
      }));
    },
    async verifyStep(plan, stepId, requestId) {
      await call("plan.graph.verify", {
        plan_id: plan.plan_id, step_id: stepId, expected_state_revision: plan.state_revision,
        request_id: requestId,
      });
    },
    async requestHuman(plan, stepId, checkId, requestId) {
      await call("verification.request", {
        plan_id: plan.plan_id, step_id: stepId, check_id: checkId,
        expected_state_revision: plan.state_revision, request_id: requestId,
      });
    },
    async decideHuman(plan, request, missionDigest, decision, reason, requestId) {
      const { request_id: _oldRequestId, ...binding } = request;
      await call("verification.decide", {
        ...binding, format_version: 1, task_id: plan.task_id, mission_digest: missionDigest,
        plan_digest: plan.document_digest, expected_state_revision: plan.state_revision,
        decision, reason, request_id: requestId,
      });
    },
    async material(taskId, ref) {
      return (await call("agent.material", { task_id: taskId, content_ref: ref })).material;
    },
    async prepareConsent(target) {
      return previewView((await call("consent.prepare", target)).preview);
    },
    async issueConsent(preview, uses, expiresAt, requestId) {
      const { task_id: _taskId, ...binding } = preview;
      return consentView((await call("consent.issue", {
        ...binding, max_uses: uses, expires_at: expiresAt, request_id: requestId,
      })).consent);
    },
    async listConsents(runId) {
      const result = await call("consent.list", { run_id: runId });
      if (!Array.isArray(result.consents)) invalid();
      return result.consents.map(consentView);
    },
    async revokeConsent(consentId, requestId) {
      return consentView((await call("consent.revoke", {
        consent_id: consentId, request_id: requestId, reason: "用户在界面撤销有限授权",
      })).consent);
    },
  };
}
