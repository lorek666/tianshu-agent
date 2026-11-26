import { useEffect, useRef, useState } from "react";
import type { AgentApi, PlanGraph, VerificationRequest } from "../api/agent";
import { humanizeHostError } from "../api/errors";
import { asRecord } from "../lib/values";

interface Props {
  api: AgentApi;
  graph: PlanGraph;
  missionDigest: string;
  disabled: boolean;
  operate: (key: string, operation: (requestId: string) => Promise<unknown>) => Promise<void>;
}
const stepLabels: Record<string, string> = {
  pending: "等待依赖", ready: "可以执行", running: "执行中", verifying: "待独立验收",
  satisfied: "验证通过", failed: "失败", blocked: "阻断", invalidated: "证据已失效", skipped: "跳过",
};

function materialRefs(value: unknown): string[] {
  if (Array.isArray(value)) return value.flatMap(materialRefs);
  if (value === null || typeof value !== "object") return [];
  return Object.entries(value).flatMap(([key, item]) => {
    if (["content_ref", "content_refs", "evidence_refs"].includes(key)) {
      const candidates = Array.isArray(item) ? item : [item];
      return candidates.filter((ref): ref is string => typeof ref === "string"
        && /^content:\/\/sha256:[a-f0-9]{64}$/.test(ref));
    }
    return materialRefs(item);
  });
}
function materialText(value: unknown): string {
  const record = asRecord(value);
  if (record.encoding === "base64" && typeof record.data === "string") {
    try {
      const raw = Uint8Array.from(atob(record.data), (char) => char.charCodeAt(0));
      return new TextDecoder("utf-8", { fatal: true }).decode(raw);
    } catch {
      return "此冻结产物为二进制或不可显示的 UTF-8 内容。请按材料引用核对原始产物。";
    }
  }
  return JSON.stringify(value, null, 2);
}

/** 只展示内容存储中的冻结快照；不读取可变工作区路径，也不解释为 HTML。 */
function HumanCheckReview({
  api, graph, request, missionDigest, disabled, operate,
}: Props & { request: VerificationRequest }) {
  const [materials, setMaterials] = useState<{ ref: string; text: string }[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reviewed, setReviewed] = useState(false);
  const [reason, setReason] = useState("");
  const alive = useRef(true);
  const sequence = useRef(0);
  useEffect(() => {
    alive.current = true;
    return () => { alive.current = false; sequence.current += 1; };
  }, [api]);
  async function load() {
    const current = ++sequence.current;
    setLoading(true);
    setError(null);
    setMaterials(null);
    setReviewed(false);
    try {
      const seen = new Set<string>();
      const loaded: { ref: string; text: string }[] = [];
      const queue = [...request.evidence_refs];
      while (queue.length) {
        if (!alive.current || current !== sequence.current) return;
        const ref = queue.shift()!;
        if (seen.has(ref)) continue;
        if (seen.size >= 64) throw new Error("证据引用超过界面审阅上限，请先缩小验收范围。");
        seen.add(ref);
        const value = await api.material(graph.plan.task_id, ref);
        loaded.push({ ref, text: materialText(value) });
        queue.push(...materialRefs(value).filter((candidate) => !seen.has(candidate)));
      }
      if (alive.current && current === sequence.current) setMaterials(loaded);
    } catch (caught) {
      if (alive.current && current === sequence.current) setError(humanizeHostError(caught));
    } finally {
      if (alive.current && current === sequence.current) setLoading(false);
    }
  }
  const check = graph.plan.manifest.checks.find((item) => item.check_id === request.check_id);
  const ready = !disabled && !loading && reviewed && materials !== null && reason.trim() !== "";
  return <section className="agent-human-review" aria-label="人工验收">
    <strong>{String(check?.inputs.question ?? request.check_id)}</strong>
    <p className="subtle">待验批次 {request.attempt_id}；输入指纹 <code>{request.input_fingerprint}</code></p>
    <button type="button" className="btn btn-sm" data-testid="human-load"
      disabled={loading || disabled} onClick={() => void load()}>
      {loading ? "正在加载冻结证据…" : "加载冻结证据与产物"}
    </button>
    {error ? <p role="alert" className="text-danger">{error}</p> : null}
    {materials?.map((material) => <details key={material.ref} open>
      <summary className="agent-digest">{material.ref}</summary>
      <pre className="workflow-result-text">{material.text}</pre>
    </details>)}
    <label>验收说明
      <textarea rows={2} data-testid="human-reason" value={reason} disabled={disabled}
        onChange={(event) => setReason(event.target.value)} />
    </label>
    <label className="agent-review-check">
      <input type="checkbox" data-testid="human-reviewed" checked={reviewed}
        disabled={materials === null || disabled} onChange={(event) => setReviewed(event.target.checked)} />
      我已核对这些冻结证据与产物
    </label>
    <div className="goal-panel-actions">
      {(["accept", "reject"] as const).map((decision) => <button key={decision} type="button"
        className="btn btn-sm" data-testid={`human-${decision}`} disabled={!ready}
        onClick={() => void operate(
          JSON.stringify(["human", request.attempt_id, graph.plan.state_revision, decision, reason.trim()]),
          (id) => api.decideHuman(graph.plan, request, missionDigest, decision, reason.trim(), id),
        )}>{decision === "accept" ? "验收通过" : "验收不通过"}</button>)}
    </div>
  </section>;
}

export function PlanGraphReview({ api, graph, missionDigest, disabled, operate }: Props) {
  const { plan, verification_requests: requests } = graph;
  const [reviewed, setReviewed] = useState(false);
  return <section className="agent-plan-review" aria-label="任务计划与检查">
    <h3>计划与检查</h3>
    <p>修订 {plan.revision} · {plan.confirmed ? "已确认" : "等待你确认"}</p>
    <p className="agent-digest">计划摘要 <code>{plan.document_digest}</code></p>
    <div className="agent-criteria">
      {plan.mission.criteria.map((criterion) => <p key={criterion.criterion_id}>
        <strong>{criterion.description}</strong>
        <span className="subtle">（{criterion.mode === "human" ? "人工验收" : "机械检查"}）</span>
      </p>)}
    </div>
    {plan.steps.map((step) => <section key={step.step_id} className="agent-plan-step">
      <strong>{step.title}</strong><span className="subtle"> · {stepLabels[step.state]}</span>
      <p>{step.instruction}</p>
      <p className="subtle">依赖：{step.depends_on.join("、") || "无"}；条件：{step.criterion_ids.join("、")}</p>
      {step.expected_artifacts.map((artifact) => <p key={artifact.artifact_id}>
        预期产物：{artifact.artifact_id} · {artifact.kind} · {artifact.locator}
      </p>)}
      {step.verification_ids.map((id) => {
        const check = plan.manifest.checks.find((item) => item.check_id === id);
        if (!check) return <p role="alert" key={id}>缺少检查 {id}，请刷新计划。</p>;
        const pending = requests.find((item) => item.step_id === step.step_id && item.check_id === id);
        return <div key={id} className="agent-check">
          <strong>{check.kind === "human" ? "人工检查" : "机械检查"}：{id}</strong>
          <pre className="workflow-result-text">{JSON.stringify(check.inputs, null, 2)}</pre>
          {check.kind === "human" && !pending ? <button type="button" className="btn btn-sm"
            disabled={disabled || !plan.confirmed || step.state !== "verifying"}
            onClick={() => void operate(`human-request:${step.step_id}:${id}:${plan.state_revision}`,
              (requestId) => api.requestHuman(plan, step.step_id, id, requestId))}>
            准备人工验收材料
          </button> : null}
        </div>;
      })}
      {step.state === "verifying" ? <button type="button" className="btn btn-sm"
        disabled={disabled || !plan.confirmed}
        onClick={() => void operate(`verify:${step.step_id}:${plan.state_revision}`,
          (id) => api.verifyStep(plan, step.step_id, id))}>运行独立检查</button> : null}
      {step.evidence_refs.length ? <p className="agent-digest">步骤证据：{step.evidence_refs.join("、")}</p> : null}
      {step.reason_code ? <p className="text-danger">状态原因：{step.reason_code}</p> : null}
    </section>)}
    <details>
      <summary>查看完整计划文档与检查清单</summary>
      <pre className="workflow-result-text">{JSON.stringify({
        document: plan.document, manifest: plan.manifest,
      }, null, 2)}</pre>
    </details>
    {!plan.confirmed ? <>
      <label className="agent-review-check">
        <input type="checkbox" data-testid="plan-reviewed" checked={reviewed} disabled={disabled}
          onChange={(event) => setReviewed(event.target.checked)} />
        我已审阅当前修订的完整计划与检查，确认以上精确摘要
      </label>
      <button type="button" className="btn btn-primary btn-sm" data-testid="plan-confirm"
        disabled={disabled || !reviewed}
        onClick={() => void operate(`confirm:${plan.plan_id}:${plan.revision}:${plan.document_digest}`,
          (id) => api.confirmPlan(plan, id))}>确认这份计划</button>
    </> : null}
    {requests.filter((request) => plan.manifest.checks.some(
      (check) => check.check_id === request.check_id && check.kind === "human",
    )).map((request) => <HumanCheckReview key={`${request.attempt_id}:${plan.state_revision}`}
      api={api} graph={graph} request={request} missionDigest={missionDigest}
      disabled={disabled} operate={operate} />)}
  </section>;
}
