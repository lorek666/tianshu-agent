import { useEffect, useRef, useState } from "react";
import type { GoalConfig, GoalsApi, GoalView } from "../api/goals";
import { criteriaFromText } from "../api/goals";
import type { RunExecutionPolicyV1, RunView, TaskView } from "../api/types";
import { GoalFields } from "./GoalFields";

interface Props {
  task: TaskView;
  api: GoalsApi | null;
  eventSeqHead: number;
  executionPolicy: RunExecutionPolicyV1;
  initialConfig?: GoalConfig;
  openRequest?: { nonce: number; text: string } | null;
  onRunStarted: (run: RunView) => void | Promise<void>;
  onChanged: () => void;
  onSelectRun: (runId: string) => void;
}
const labels: Record<GoalView["status"], string> = {
  ready: "等待推进", running: "本轮进行中", awaiting_review: "等待验收或继续",
  exhausted: "轮次已用尽，等待验收", blocked: "已阻断，请先核查本轮结果",
  stale: "任务已改变，请新建目标", completed: "已人工验收", cancelled: "已结束",
};

/** 由 task_id + revision 作为组件 key，防止迟到请求跨任务污染视图。 */
export function GoalPanel({
  task, api, eventSeqHead, executionPolicy, openRequest = null, initialConfig,
  onRunStarted, onChanged, onSelectRun,
}: Props) {
  const [goal, setGoal] = useState<GoalView | null>(null);
  const [editing, setEditing] = useState(initialConfig !== undefined);
  const [criteria, setCriteria] = useState(initialConfig?.criteria.join("\n") ?? "");
  const [maxRounds, setMaxRounds] = useState(initialConfig?.max_rounds ?? 3);
  const [confirmed, setConfirmed] = useState<number[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [pending, setPending] = useState(false);
  const alive = useRef(true);
  const lifecycle = useRef(0);
  const inFlight = useRef(false);
  const serial = useRef(0);
  const requests = useRef(new Map<string, string>());
  const previousRound = useRef<string | null>(null);

  useEffect(() => {
    alive.current = true;
    inFlight.current = false;
    setPending(false);
    return () => {
      alive.current = false;
      lifecycle.current += 1;
      serial.current += 1;
    };
  }, [api]);

  useEffect(() => {
    if (openRequest !== null) {
      setEditing(true);
      if (openRequest.text.trim()) setCriteria(openRequest.text);
    }
  }, [openRequest]);

  useEffect(() => {
    if (api === null || inFlight.current) return;
    const current = ++serial.current;
    setLoading(true);
    void api.get(task.task_id).then(
      (next) => {
        if (alive.current && current === serial.current) {
          setGoal(next);
          setError(null);
          if (previousRound.current !== next?.latest_run_id) setConfirmed([]);
          previousRound.current = next?.latest_run_id ?? null;
        }
      },
      (caught: unknown) => {
        if (alive.current && current === serial.current) {
          setError(caught instanceof Error ? caught.message : "读取目标失败，请刷新后重试。");
        }
      },
    ).finally(() => {
      if (alive.current && current === serial.current) setLoading(false);
    });
  }, [api, task.task_id, task.goal_revision, eventSeqHead]);

  function requestId(key: string) {
    if (!requests.current.has(key)) requests.current.set(key, crypto.randomUUID());
    return requests.current.get(key)!;
  }

  async function operate(action: "create" | "advance" | "completed" | "cancelled") {
    if (api === null || inFlight.current) return;
    const generation = lifecycle.current;
    const current = () => alive.current && generation === lifecycle.current;
    inFlight.current = true;
    serial.current += 1;
    setPending(true);
    setError(null);
    try {
      let next: GoalView;
      if (action === "create") {
        const config = { criteria: criteriaFromText(criteria), max_rounds: maxRounds };
        const key = JSON.stringify(["create", task.goal_revision, config, executionPolicy]);
        next = await api.create(task, config, executionPolicy, requestId(key));
      } else {
        if (goal === null) throw new Error("请先建立目标");
        const key = JSON.stringify([
          action, goal.goal_id, goal.rounds_started, action === "completed" ? confirmed : [],
        ]);
        if (action === "advance") {
          const result = await api.advance(goal.goal_id, requestId(key));
          next = result.goal;
          if (current()) await onRunStarted(result.run);
        } else {
          next = await api.close(goal.goal_id, action,
            action === "completed" ? confirmed : [], requestId(key));
        }
      }
      if (!current()) return;
      setGoal(next);
      setEditing(false);
      setConfirmed([]);
      previousRound.current = next.latest_run_id;
      setLoading(false);
      onChanged();
      // 推进时可能已收到末轮完成事件；不依赖之后一定再来一个新的 SSE seq。
      const refreshed = await api.get(task.task_id);
      if (current()) {
        setGoal(refreshed);
        previousRound.current = refreshed?.latest_run_id ?? null;
      }
    } catch (caught) {
      if (current()) setError(caught instanceof Error ? caught.message : "目标操作失败，请重试。");
    } finally {
      if (current()) {
        inFlight.current = false;
        setPending(false);
        setLoading(false);
      }
    }
  }

  if (api === null) return null;
  const idle = task.active_run_id === null;
  const newAllowed = goal === null || ["completed", "cancelled", "stale"].includes(goal.status);
  const canReview = goal !== null && ["awaiting_review", "exhausted"].includes(goal.status);
  return <section className="goal-panel" aria-label="目标模式" data-testid="goal-panel">
    <div className="goal-panel-heading">
      <strong>目标模式</strong>
      <span className="subtle">每轮手动继续</span>
      {goal !== null ? <span>{labels[goal.status]} · {goal.rounds_started} / {goal.max_rounds} 轮</span> : null}
      {newAllowed && !editing ? <button type="button" className="btn btn-sm"
        disabled={pending || loading || !idle} onClick={() => setEditing(true)}>
        {goal === null ? "建立目标" : "新建目标"}
      </button> : null}
    </div>
    {error !== null ? <p role="alert" className="text-danger">{error}</p> : null}
    {editing && newAllowed ? <>
      <GoalFields criteria={criteria} maxRounds={maxRounds} disabled={pending}
        onCriteriaChange={setCriteria} onMaxRoundsChange={setMaxRounds} />
      <button type="button" className="btn btn-primary btn-sm" data-testid="goal-create"
        disabled={pending || !idle} onClick={() => void operate("create")}>保存目标</button>
    </> : goal !== null ? <>
      <p className="subtle">
        {goal.execution_policy.mode === "pure_only" ? "固定为仅读取与对话" : "工具操作沿用逐次审批"}；
        使用当前默认模型，未为目标固定模型档案；运行成功后仍需人工验收。
      </p>
      <div className="goal-checklist">
        {goal.criteria.map((criterion, index) => <label key={index}>
          <input type="checkbox" data-testid={`goal-check-${index}`}
            disabled={!canReview || pending}
            checked={goal.status === "completed"
              ? goal.confirmed_criteria.includes(index) : confirmed.includes(index)}
            onChange={(event) => setConfirmed((previous) => event.target.checked
              ? [...previous, index].sort((a, b) => a - b) : previous.filter((i) => i !== index))} />
          {criterion}
        </label>)}
      </div>
      <div className="goal-panel-actions">
        <button type="button" className="btn btn-primary btn-sm" data-testid="goal-advance"
          disabled={pending || !idle || !["ready", "awaiting_review"].includes(goal.status)}
          onClick={() => void operate("advance")}>推进一轮</button>
        <button type="button" className="btn btn-sm" data-testid="goal-complete"
          disabled={pending || !idle || !canReview || confirmed.length !== goal.criteria.length}
          onClick={() => void operate("completed")}>确认目标完成</button>
        {goal.latest_run_id !== null ? <button type="button" className="btn btn-sm"
          onClick={() => onSelectRun(goal.latest_run_id!)}>查看本轮</button> : null}
        {!["completed", "cancelled"].includes(goal.status) ? <button type="button"
          className="btn btn-sm" disabled={pending || !idle}
          onClick={() => void operate("cancelled")}>结束目标</button> : null}
      </div>
    </> : loading ? <p className="subtle">正在读取目标…</p> : null}
  </section>;
}
