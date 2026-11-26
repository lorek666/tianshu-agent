import type { TaskView } from "../api/types";

export const digest = "a".repeat(64);
export const otherDigest = "b".repeat(64);
export const contentRef = `content://sha256:${digest}`;
export const policy = { format_version: 1, mode: "pure_only" } as const;
export const loopTask: TaskView = {
  task_id: "task_1", control: false, goal: "整理资料", goal_revision: 1,
  status: "open", active_run_id: null, workspaces: [{ root_path: "/work", mode: "readwrite" }],
  created_at: 1, updated_at: 1,
};
export const loopGoal = {
  format_version: 2, goal_id: "goal_2", task_id: "task_1",
  config: {
    format_version: 2, goal_id: "goal_2", task_id: "task_1", goal_revision: 1,
    mission_id: "mission_1", mission_digest: digest, mode: "manual",
    completion_policy: "human", replan_policy: "confirm", max_rounds: 3,
    deadline_ms: 1800000000000, budget_limits: { requests: 32 }, budget_bound: "estimate",
    max_no_progress_rounds: 2, execution_policy: policy, isolation_requirements: [],
    predecessor_goal_id: null,
  },
  status: "ready", phase: "plan", round: 0, rounds_started: 0, phase_run_id: null,
  plan_id: null, plan_digest: null, step_id: null, execution_run_id: null,
  verification_run_id: null, no_progress_streak: 0, satisfied_criterion_ids: [],
  completed_step_ids: [], baseline_step_ids: [], last_assessed_round: 0,
  progress_fingerprint: digest, blocker: null, orchestration_epoch: 1,
  clock_wall_ms: 1, deadline_remaining_ms: 1800000, clock_monotonic_ms: 1,
  deadline_monotonic_ms: 1800001, runtime_session_id: "runtime_1",
  denied_content_digests: [], recovered_denial_digests: [], state_revision: 10,
  assessments: [], pause_pending: false,
};
export const loopPlan = {
  format_version: 1, plan_id: "plan_1", task_id: "task_1", revision: 1,
  state_revision: 20, document_digest: otherDigest, confirmed: false,
  mission: {
    format_version: 1, mission_id: "mission_1", task_id: "task_1", goal_revision: 1,
    task_kind: "general", user_goal: "整理资料",
    profile: { id: "general", version: "1", digest },
    criteria: [{ criterion_id: "criterion_1", description: "报告包含来源", mode: "human" }],
    tool_allowlist: [], prompt: { template_id: "base", version: "1", digest, content_ref: contentRef },
  },
  manifest: {
    format_version: 1, mission_id: "mission_1", mission_digest: digest,
    checks: [{
      format_version: 1, check_id: "human_1", criterion_ids: ["criterion_1"],
      required: true, kind: "human",
      inputs: { question: "来源是否可核对？", required_artifact_ids: [], required_check_ids: [] },
    }],
  },
  document: {
    format_version: 1, plan_id: "plan_1", revision: 1, mission_id: "mission_1",
    mission_digest: digest, previous_digest: null,
    verification_manifest: { content_ref: contentRef, digest },
    steps: [{
      step_id: "step_1", title: "整理来源", instruction: "核对原始资料后生成报告",
      depends_on: [], criterion_ids: ["criterion_1"], expected_artifacts: [],
      verification_ids: ["human_1"],
    }],
  },
  changed_step_ids: ["step_1"],
  steps: [{
    step_id: "step_1", title: "整理来源", instruction: "核对原始资料后生成报告",
    depends_on: [], criterion_ids: ["criterion_1"], expected_artifacts: [],
    verification_ids: ["human_1"], state: "verifying", attempt: 1,
    execution_run_id: "run_1", evidence_refs: [], reason_code: "",
  }],
};
export const humanRequest = {
  format_version: 1, plan_id: "plan_1", revision: 1, step_id: "step_1",
  check_id: "human_1", check_digest: digest, attempt_id: "attempt_1",
  input_fingerprint: otherDigest, evidence_refs: [contentRef], request_id: "request_old",
};
export const consentPreview = {
  task_id: "task_1", run_id: "run_1", goal_revision: 1, workspace_binding_digest: digest,
  mission_digest: digest, plan_digest: otherDigest, action: "overwrite",
  tool_binding: { tool_id: "modify_text", version: "1", schema_hash: digest, description_hash: digest },
  matcher: {
    kind: "directory_overwrite", directory: "/work", directory_identity: "1:2",
    recursive: false, allow_content_changes: true, max_file_bytes: 262144, max_total_bytes: 2097152,
  },
};
export const activeConsent = {
  ...consentPreview, format_version: 1, consent_id: "consent_1", max_uses: 2,
  expires_at: 1800000000000, runtime_session_id: "runtime_1",
  issued_by: { actor: "local", source: "cli" }, request_id: "issue_1",
  state: "active", remaining_uses: 2, used_bytes: 0,
};
