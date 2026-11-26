import { useCallback, useEffect, useRef, useState } from "react";
import type {
  AgentApi, AgentGoalDraft, AgentGoalView, AgentStatus, PlanGraph, RecoveryResolution,
} from "../api/agent";
import type { RunExecutionPolicyV1, TaskView } from "../api/types";
import { criteriaFromText } from "../api/goals";
import { humanizeHostError } from "../api/errors";
import { formatDateTime } from "../lib/format";
import { PlanGraphReview } from "./PlanGraphReview";

interface Props {
  api: AgentApi | null;
  task: TaskView;
  eventSeqHead: number;
  executionPolicy: RunExecutionPolicyV1;
  hasUncertainEffects?: boolean;
  initialConfig?: AgentGoalDraft;
  openRequest?: { nonce: number; text: string } | null;
  onChanged: () => void;
  onSelectRun: (runId: string) => void;
}
const statusLabels: Record<AgentStatus, string> = {
  ready: "等待推进", active: "正在推进", awaiting_input: "等待补充信息",
  awaiting_review: "等待你审阅", paused: "已暂停", blocked: "已阻断",
  completed: "已验收完成", cancelled: "已取消", stale: "任务已修订，旧目标已失效",
};
const phaseLabels: Record<AgentGoalView["phase"], string> = {
  plan: "制定计划", execute: "执行步骤", verify: "独立检查",
  ask: "补充信息", replan: "修订计划", finish: "完成验收",
};
const blockerLabels: Record<string, string> = {
  verification_failed: "检查未通过", execution_failed: "执行失败", policy_denied: "执行策略拒绝",
  approval_denied: "操作已被你否决", budget_exhausted: "累计预算已用尽",
  deadline_exceeded: "目标期限已到", round_limit: "轮次上限已到", no_progress: "连续无进展",
  isolation_unavailable: "所需执行隔离不可用", effect_unknown: "存在未知副作用，需要原工具核对",
  usage_unknown: "费用或用量未知，需要核对", protocol_error: "阶段结果格式无效",
  recovery_required: "运行中断，需要显式恢复",
};
const recoveryLabels: Record<RecoveryResolution, string> = {
  retry: "重试已确认未产生副作用的步骤", replan: "重新制定计划",
  reconciled: "已有原工具核对证据", new_observation: "开始新的进展观察段",
  change_decision: "改变先前的精确否决",
};

/** 内层按 Task 修订重新挂载；旧任务的读取、写入与回调均失效。 */
export function AgentGoalPanel(props: Props) {
  if (props.api === null) return null;
  return <AgentGoalContent key={`${props.task.task_id}:${props.task.goal_revision}`} {...props} api={props.api} />;
}
function AgentGoalContent({
  api, task, eventSeqHead, executionPolicy, initialConfig, openRequest = null, onChanged, onSelectRun,
  hasUncertainEffects = false,
}: Props & { api: AgentApi }) {
  const [goal, setGoal] = useState<AgentGoalView | null>(null);
  const [graph, setGraph] = useState<PlanGraph | null>(null);
  const [editing, setEditing] = useState(initialConfig !== undefined);
  const [criteria, setCriteria] = useState(initialConfig?.criteria.join("\n") ?? "");
  const [mode, setMode] = useState<"manual" | "bounded_auto">(initialConfig?.mode ?? "manual");
  const [maxRounds, setMaxRounds] = useState(initialConfig?.max_rounds ?? 3);
  const [maxRequests, setMaxRequests] = useState(initialConfig?.max_requests ?? 32);
  const [minutes, setMinutes] = useState(initialConfig?.deadline_minutes ?? 30);
  const [noProgress, setNoProgress] = useState(initialConfig?.max_no_progress_rounds ?? 2);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [synced, setSynced] = useState(false);
  const [resolution, setResolution] = useState<RecoveryResolution>("retry");
  const [evidence, setEvidence] = useState("");
  const alive = useRef(true);
  const generation = useRef(0);
  const serial = useRef(0);
  const inFlight = useRef(false);
  const requests = useRef(new Map<string, string>());
  useEffect(() => {
    alive.current = true;
    inFlight.current = false;
    setPending(false);
    requests.current.clear();
    return () => { alive.current = false; generation.current += 1; serial.current += 1; };
  }, [api]);
  const refresh = useCallback(async () => {
    const version = ++serial.current;
    try {
      const next = await api.getGoal(task.task_id);
      if (!alive.current || version !== serial.current) return;
      if (next && (next.task_id !== task.task_id || next.config.goal_revision !== task.goal_revision)) {
        setGoal(null); setGraph(null); setSynced(true);
        return;
      }
      const plan = next?.plan_id ? await api.getPlan(next.plan_id) : null;
      if (!alive.current || version !== serial.current) return;
      if (plan && (plan.plan.task_id !== task.task_id || plan.plan.mission.goal_revision !== task.goal_revision)) {
        throw new Error("计划属于其他任务修订，请刷新后重试。");
      }
      setGoal(next);
      setGraph(plan);
      setSynced(true);
    } catch (caught) {
      if (alive.current && version === serial.current) throw caught;
    }
  }, [api, task.task_id, task.goal_revision]);
  useEffect(() => {
    if (inFlight.current) return;
    setSynced(false);
    void refresh().catch((caught: unknown) => {
      if (alive.current) setError(humanizeHostError(caught));
    });
  }, [refresh, eventSeqHead]);
  useEffect(() => {
    if (openRequest) {
      setEditing(true);
      if (openRequest.text.trim()) setCriteria(openRequest.text);
    }
  }, [openRequest]);

  function requestId(key: string) {
    if (!requests.current.has(key)) requests.current.set(key, crypto.randomUUID());
    return requests.current.get(key)!;
  }
  async function operate(key: string, operation: (id: string) => Promise<unknown>) {
    if (inFlight.current || !synced) return;
    const active = generation.current;
    const current = () => alive.current && active === generation.current;
    inFlight.current = true;
    serial.current += 1;
    setPending(true); setError(null);
    let succeeded = false;
    try {
      await operation(requestId(key));
      if (!current()) return;
      succeeded = true;
      setEditing(false);
    } catch (caught) {
      if (current()) setError(humanizeHostError(caught));
    } finally {
      if (current()) {
        setSynced(false);
        try { await refresh(); }
        catch (caught) {
          if (current()) setError((previous) =>
            `${previous ? `${previous}；` : ""}刷新失败：${humanizeHostError(caught)}`);
        }
        if (current()) {
          inFlight.current = false; setPending(false);
          if (succeeded) onChanged();
        }
      }
    }
  }
  const terminal = goal !== null && ["completed", "cancelled", "stale"].includes(goal.status);
  const disabled = pending || !synced;
  const newAllowed = goal === null || terminal;
  const canComplete = goal?.status === "awaiting_review" && goal.phase === "finish"
    && !hasUncertainEffects
    && graph?.plan.steps.every((step) => step.state === "satisfied");
  const recoveryOptions: RecoveryResolution[] = goal?.blocker?.category === "approval_denied"
    ? ["change_decision"] : goal?.blocker?.category === "no_progress" ? ["new_observation"]
      : ["effect_unknown", "usage_unknown"].includes(goal?.blocker?.category ?? "")
        ? ["reconciled"] : goal?.blocker?.safe_retry ? ["retry", "replan"] : ["replan", "reconciled"];
  const recovery = recoveryOptions.includes(resolution) ? resolution : recoveryOptions[0]!;
  const canRecover = goal?.blocker?.available_actions.includes("recover") === true;
  const evidenceRefs = evidence.split("\n").map((line) => line.trim()).filter(Boolean);
  const evidenceRequired = ["effect_unknown", "usage_unknown"].includes(goal?.blocker?.category ?? "");
  const assessment = goal?.assessments.at(-1);
  const currentStep = graph?.plan.steps.find((step) => step.step_id === goal?.step_id);

  return <section className="goal-panel agent-goal-panel" aria-label="连续目标" data-testid="agent-goal-panel">
    <div className="goal-panel-heading">
      <strong>目标推进</strong>
      {goal ? <span>{statusLabels[goal.status]} · {goal.round} / {goal.config.max_rounds} 轮</span> : null}
      {newAllowed && !editing ? <button type="button" className="btn btn-sm" data-testid="agent-open"
        disabled={disabled || task.active_run_id !== null} onClick={() => setEditing(true)}>
        {goal ? "新建连续目标" : "建立连续目标"}
      </button> : null}
      <button type="button" className="link-btn" disabled={pending} onClick={() => {
        setSynced(false); setError(null);
        void refresh().catch((caught: unknown) => {
          if (alive.current) setError(humanizeHostError(caught));
        });
      }}>刷新目标</button>
    </div>
    {error ? <p role="alert" className="text-danger">{error}</p> : null}
    {!synced && !pending ? <p className="subtle">正在核对目标与计划…</p> : null}
    {editing && newAllowed ? <div className="goal-fields">
      <p className="subtle">创建只保存配置。手动模式由你继续；有限自动模式在这些上限内推进，仍需确认计划和人工检查。</p>
      <label>验收条件（每行一条）<textarea data-testid="agent-criteria" rows={3} value={criteria}
        disabled={disabled} onChange={(event) => setCriteria(event.target.value)} /></label>
      <label>推进方式<select data-testid="agent-mode" value={mode} disabled={disabled}
        onChange={(event) => setMode(event.target.value as "manual" | "bounded_auto")}>
        <option value="manual">每轮手动继续</option><option value="bounded_auto">有限自动推进</option>
      </select></label>
      <div className="agent-bound-fields">
        <label>最多轮次<input type="number" min={1} max={100} value={maxRounds} disabled={disabled}
          onChange={(event) => setMaxRounds(Number(event.target.value))} /></label>
        <label>累计模型请求上限<input type="number" min={1} max={10000} value={maxRequests} disabled={disabled}
          onChange={(event) => setMaxRequests(Number(event.target.value))} /></label>
        <label>有效分钟<input type="number" min={1} max={1440} value={minutes} disabled={disabled}
          onChange={(event) => setMinutes(Number(event.target.value))} /></label>
        <label>连续无进展轮次<input type="number" min={1} max={100} value={noProgress} disabled={disabled}
          onChange={(event) => setNoProgress(Number(event.target.value))} /></label>
      </div>
      <button type="button" className="btn btn-primary btn-sm" data-testid="agent-create" disabled={disabled}
        onClick={() => {
          try {
            const draft: AgentGoalDraft = { criteria: criteriaFromText(criteria), max_rounds: maxRounds,
              mode, max_requests: maxRequests, deadline_minutes: minutes, max_no_progress_rounds: noProgress };
            if (![[maxRounds, 100], [maxRequests, 10000], [minutes, 1440], [noProgress, 100]]
              .every(([value, maximum]) => Number.isSafeInteger(value) && value! > 0 && value! <= maximum!)) {
              throw new Error("轮次、请求、期限与无进展上限必须是范围内的正整数。");
            }
            void operate(JSON.stringify(["create", draft, executionPolicy]),
              (id) => api.createGoal(task, draft, executionPolicy, id));
          } catch (caught) { setError(humanizeHostError(caught)); }
        }}>保存连续目标</button>
    </div> : null}
    {goal ? <>
      <p>{goal.config.mode === "bounded_auto" ? "有限自动推进" : "每轮手动继续"}；
        当前阶段：{phaseLabels[goal.phase]}；
        {goal.config.execution_policy.mode === "pure_only" ? "仅读取与对话" : "具体操作仍按规则审批"}</p>
      <p className="subtle">累计模型请求上限 {goal.config.budget_limits.requests ?? "未设"}；
        截止 {formatDateTime(goal.config.deadline_ms)}；
        连续无进展 {goal.no_progress_streak} / {goal.config.max_no_progress_rounds} 轮。
        暂停和恢复不会重置这些上限。</p>
      <p><strong>本轮目标：</strong>{currentStep?.title ?? (goal.phase === "plan" ? "制定可验收的任务计划" : phaseLabels[goal.phase])}</p>
      <p><strong>已完成并有证据：</strong>{assessment?.completed_step_ids.join("、") || "尚无本轮验证通过的步骤"}</p>
      <p><strong>剩余事项：</strong>{graph?.plan.steps.filter((step) => step.state !== "satisfied")
        .map((step) => step.title).join("、") || (graph ? "等待最终验收" : "等待计划")}</p>
      {assessment?.evidence_refs.length ? <p className="agent-digest">本轮证据：{assessment.evidence_refs.join("、")}</p> : null}
      {goal.assessments.length ? <details>
        <summary>查看各轮目标、证据与剩余事项</summary>
        {goal.assessments.map((round, index) => <section key={`${round.round}:${index}`} className="agent-plan-step">
          <strong>第 {round.round} 轮</strong>
          <p>本轮目标：{round.target_criterion_ids.map((id) =>
            graph?.plan.mission.criteria.find((criterion) => criterion.criterion_id === id)?.description ?? id,
          ).join("、") || "无"}</p>
          <p>已验证步骤：{round.completed_step_ids.join("、") || "无"}</p>
          <p>剩余步骤：{round.remaining_step_ids.join("、") || "无"}</p>
          <p className="agent-digest">证据引用：{round.evidence_refs.join("、") || "无"}</p>
          {round.blocker ? <p>阻断：{blockerLabels[round.blocker.category]}</p> : null}
        </section>)}
      </details> : null}
      {goal.pause_pending ? <p role="status">正在暂停，等待当前操作到达安全点；此时还不能恢复。</p> : null}
      {hasUncertainEffects ? <p role="status">当前任务存在未知或部分副作用。请先通过原工具核对，再继续推进或人工验收。</p> : null}
      {goal.status === "awaiting_input" ? <p role="status">请打开当前阶段运行，在下方追问输入框补充信息。</p> : null}
      {goal.blocker ? <div className="agent-blocker" role="status">
        <p><strong>阻断原因：</strong>{blockerLabels[goal.blocker.category] ?? goal.blocker.category}</p>
        <p className="subtle">原始事件 {goal.blocker.source_event_seq}。恢复不会授予工具权限，也不能用人工确认洗白未知副作用。</p>
        {canRecover ? <>
          <label>恢复决定<select value={recovery} disabled={disabled}
            onChange={(event) => setResolution(event.target.value as RecoveryResolution)}>
            {recoveryOptions.map((value) => <option key={value} value={value}>{recoveryLabels[value]}</option>)}
          </select></label>
          <label>核对证据引用（每行一条）
            <textarea data-testid="agent-recovery-evidence" rows={2} value={evidence} disabled={disabled}
              onChange={(event) => setEvidence(event.target.value)} />
          </label>
          {goal.blocker.denied_content_digest ? <p className="agent-digest">
            原否决内容摘要：{goal.blocker.denied_content_digest}</p> : null}
          <button type="button" className="btn btn-sm" data-testid="agent-recover"
            disabled={disabled || hasUncertainEffects || (evidenceRequired && evidenceRefs.length === 0)}
            onClick={() => {
              const extra = {
                expected_block_event_seq: goal.state_revision, resolution: recovery,
                evidence_refs: evidenceRefs,
                ...(goal.blocker?.denied_content_digest
                  ? { denied_content_digest: goal.blocker.denied_content_digest } : {}),
              };
              void operate(JSON.stringify(["recover", goal.state_revision, extra]),
                (id) => api.controlGoal(goal, "recover", id, extra));
            }}>按此决定显式恢复</button>
        </> : <p>此上限不能通过恢复重置。请结束目标并检查原始记录。</p>}
      </div> : null}
      <div className="goal-panel-actions">
        <button type="button" className="btn btn-primary btn-sm" data-testid="agent-advance"
          disabled={disabled || !["ready", "active", "awaiting_review"].includes(goal.status)
            || hasUncertainEffects
            || goal.phase_run_id !== null || (graph !== null && !graph.plan.confirmed)}
          onClick={() => void operate(`advance:${goal.state_revision}`,
            (id) => api.controlGoal(goal, "advance", id))}>
          {goal.config.mode === "bounded_auto" && goal.status === "ready" ? "开始有限自动推进" : "继续推进"}
        </button>
        <button type="button" className="btn btn-sm" data-testid="agent-pause"
          disabled={disabled || terminal || ["paused", "blocked"].includes(goal.status)}
          onClick={() => void operate(`pause:${goal.state_revision}`, (id) => api.controlGoal(goal, "pause", id))}>
          暂停目标</button>
        <button type="button" className="btn btn-sm" data-testid="agent-resume"
          disabled={disabled || goal.status !== "paused" || goal.pause_pending}
          onClick={() => void operate(`resume:${goal.state_revision}`, (id) => api.controlGoal(goal, "resume", id))}>
          恢复普通暂停</button>
        <button type="button" className="btn btn-sm" data-testid="agent-complete"
          disabled={disabled || !canComplete}
          onClick={() => void operate(`complete:${goal.state_revision}`,
            (id) => api.controlGoal(goal, "close", id, { outcome: "completed" }))}>确认目标完成</button>
        {goal.phase_run_id ? <button type="button" className="btn btn-sm"
          onClick={() => onSelectRun(goal.phase_run_id!)}>查看当前阶段</button> : null}
        <button type="button" className="btn btn-sm" data-testid="agent-cancel"
          disabled={disabled || terminal}
          onClick={() => void operate(`cancel:${goal.state_revision}`,
            (id) => api.controlGoal(goal, "close", id, { outcome: "cancelled" }))}>取消目标</button>
      </div>
      {graph ? <PlanGraphReview key={`${graph.plan.plan_id}:${graph.plan.revision}:${graph.plan.state_revision}:${graph.plan.document_digest}`}
        api={api} graph={graph} missionDigest={goal.config.mission_digest}
        disabled={disabled || terminal || goal.status === "blocked" || hasUncertainEffects} operate={operate} /> : null}
    </> : null}
  </section>;
}
