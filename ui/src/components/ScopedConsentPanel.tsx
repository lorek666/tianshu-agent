import { useCallback, useEffect, useRef, useState } from "react";
import type { AgentApi, ConsentPreview, ConsentTarget, ScopedConsent } from "../api/agent";
import type { TaskView } from "../api/types";
import { humanizeHostError, isHostRpcError } from "../api/errors";
import { formatDateTime } from "../lib/format";

interface Props {
  api: AgentApi | null;
  task: TaskView;
  runId: string | null;
  eventSeqHead: number;
}
const labels: Record<ScopedConsent["state"], string> = {
  active: "活动中", consumed: "次数已用尽", revoked: "已撤销", expired: "已失效",
};
export function ScopedConsentPanel(props: Props) {
  if (props.api === null || props.runId === null) return null;
  return <ConsentContent key={`${props.task.task_id}:${props.task.goal_revision}:${props.runId}`}
    {...props} api={props.api} runId={props.runId} />;
}
function ConsentContent({ api, task, runId, eventSeqHead }: Props & { api: AgentApi; runId: string }) {
  const [kind, setKind] = useState<"directory_overwrite" | "exact_command">("directory_overwrite");
  const [target, setTarget] = useState("");
  const [recursive, setRecursive] = useState(false);
  const [uses, setUses] = useState(1);
  const [minutes, setMinutes] = useState(10);
  const [preview, setPreview] = useState<{ binding: ConsentPreview; expiresAt: number } | null>(null);
  const [reviewed, setReviewed] = useState(false);
  const [consents, setConsents] = useState<ScopedConsent[]>([]);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const alive = useRef(true);
  const lifecycle = useRef(0);
  const serial = useRef(0);
  const inFlight = useRef(false);
  const requests = useRef(new Map<string, string>());
  useEffect(() => {
    alive.current = true;
    inFlight.current = false;
    setPending(false);
    requests.current.clear();
    return () => { alive.current = false; lifecycle.current += 1; serial.current += 1; };
  }, [api]);
  const refresh = useCallback(async () => {
    const current = ++serial.current;
    try {
      const next = await api.listConsents(runId);
      if (alive.current && current === serial.current) {
        setConsents(next.filter((consent) => consent.task_id === task.task_id
          && consent.run_id === runId && consent.goal_revision === task.goal_revision));
      }
    } catch (caught) {
      if (alive.current && current === serial.current) throw caught;
    }
  }, [api, runId, task.task_id, task.goal_revision]);
  useEffect(() => {
    if (inFlight.current) return;
    // 新事实可能改变 Mission/Plan 绑定，旧预览须重新核对。
    setPreview(null); setReviewed(false);
    void refresh().catch((caught: unknown) => {
      if (alive.current) setError(humanizeHostError(caught));
    });
  }, [refresh, eventSeqHead]);
  function invalidate() { setPreview(null); setReviewed(false); }
  function requestId(key: string) {
    if (!requests.current.has(key)) requests.current.set(key, crypto.randomUUID());
    return requests.current.get(key)!;
  }
  async function operate(action: "preview" | "issue" | "revoke", consent?: ScopedConsent) {
    if (inFlight.current) return;
    const generation = lifecycle.current;
    const current = () => alive.current && generation === lifecycle.current;
    inFlight.current = true;
    serial.current += 1;
    setPending(true); setError(null);
    try {
      if (action === "preview") {
        if (!target.trim()) throw new Error(kind === "directory_overwrite" ? "请填写精确目录。" : "请填写已有命令请求 ID。");
        if (![uses, minutes].every((value) => Number.isSafeInteger(value) && value > 0)
            || uses > 10000 || minutes > 1440) throw new Error("请填写范围内的有限次数和有效分钟。");
        const input: ConsentTarget = kind === "directory_overwrite"
          ? { run_id: runId, kind, directory: target.trim(), recursive }
          : { run_id: runId, kind, request_id: target.trim() };
        const binding = await api.prepareConsent(input);
        if (!current()) return;
        if (binding.task_id !== task.task_id || binding.run_id !== runId
            || binding.goal_revision !== task.goal_revision) throw new Error("授权预览属于其他任务修订，请刷新。");
        setPreview({ binding, expiresAt: Date.now() + minutes * 60000 });
        setReviewed(false);
      } else if (action === "issue" && preview && reviewed) {
        await api.issueConsent(preview.binding, uses, preview.expiresAt,
          requestId(JSON.stringify(["issue", preview, uses])));
        if (!current()) return;
        invalidate();
      } else if (action === "revoke" && consent) {
        await api.revokeConsent(consent.consent_id, requestId(`revoke:${consent.consent_id}`));
        if (!current()) return;
      }
      if (action !== "preview" && current()) await refresh();
    } catch (caught) {
      if (current()) {
        setError(humanizeHostError(caught));
        if (action === "preview" || (isHostRpcError(caught)
            && ["conflict", "bad_params", "invalid"].includes(caught.code))) invalidate();
        try { await refresh(); } catch { /* 原操作错误保留，刷新入口允许重试。 */ }
      }
    } finally {
      if (current()) { inFlight.current = false; setPending(false); }
    }
  }
  return <section className="goal-panel agent-consent-panel" aria-label="有限范围授权">
    <div className="goal-panel-heading">
      <strong>有限范围授权</strong>
      <span className="subtle">自动审批仅限你明确签发的范围、次数和期限</span>
      <button type="button" className="link-btn" disabled={pending} onClick={() => {
        invalidate();
        void refresh().catch((caught: unknown) => {
          if (alive.current) setError(humanizeHostError(caught));
        });
      }}>刷新授权</button>
    </div>
    <p className="subtle">目录授权仅允许覆盖既有文件内容；精确命令绑定可执行文件、参数、目录、环境、超时与隔离配置。
      删除、目录外访问和其他操作仍须单独审批。完全访问未开放；进程重启后未使用的授权失效。</p>
    <p className="subtle">所选运行：{runId}</p>
    {error ? <p role="alert" className="text-danger">{error}</p> : null}
    <div className="goal-fields">
      <label>授权类型<select value={kind} disabled={pending} onChange={(event) => {
        setKind(event.target.value as typeof kind); setTarget(""); invalidate();
      }}>
        <option value="directory_overwrite">指定目录的有限覆盖</option>
        <option value="exact_command">已提出的精确命令</option>
      </select></label>
      <label>{kind === "directory_overwrite" ? "精确目录" : "已有命令请求 ID"}
        <input data-testid="consent-target" value={target} disabled={pending}
          placeholder={kind === "directory_overwrite" ? task.workspaces[0]?.root_path : "request_…"}
          onChange={(event) => { setTarget(event.target.value); invalidate(); }} />
      </label>
      {kind === "directory_overwrite" ? <label className="agent-review-check">
        <input type="checkbox" checked={recursive} disabled={pending} onChange={(event) => {
          setRecursive(event.target.checked); invalidate();
        }} />包含子目录
      </label> : null}
      <div className="agent-bound-fields">
        <label>最多使用次数<input type="number" min={1} max={10000} data-testid="consent-uses"
          value={uses} disabled={pending} onChange={(event) => {
            setUses(Number(event.target.value)); invalidate();
          }} /></label>
        <label>有效分钟<input type="number" min={1} max={1440} value={minutes} disabled={pending}
          onChange={(event) => { setMinutes(Number(event.target.value)); invalidate(); }} /></label>
      </div>
      <button type="button" className="btn btn-sm" data-testid="consent-preview" disabled={pending}
        onClick={() => void operate("preview")}>预览精确授权范围</button>
    </div>
    {preview ? <div className="agent-consent-preview">
      <p>以下预览尚未授权。最多 {uses} 次，截止 {formatDateTime(preview.expiresAt)}。</p>
      <pre className="workflow-result-text">{JSON.stringify(preview.binding, null, 2)}</pre>
      <label className="agent-review-check"><input type="checkbox" data-testid="consent-reviewed"
        checked={reviewed} disabled={pending} onChange={(event) => setReviewed(event.target.checked)} />
        我已核对精确绑定、文件字节上限或命令参数，并同意此有限范围授权
      </label>
      <button type="button" className="btn btn-primary btn-sm" data-testid="consent-issue"
        disabled={pending || !reviewed} onClick={() => void operate("issue")}>签发此有限授权</button>
    </div> : null}
    <div className="agent-consent-list">
      <h3>本轮授权</h3>
      {consents.length === 0 ? <p className="subtle">本轮暂无有限授权。</p> : consents.map((consent) => {
        const state = consent.state === "active" && consent.expires_at <= Date.now() ? "expired" : consent.state;
        return <article key={consent.consent_id}>
        <strong>{consent.matcher.kind === "directory_overwrite" ? "目录覆盖" : "精确命令"} · {labels[state]}</strong>
        <p className="subtle">剩余 {consent.remaining_uses} / {consent.max_uses} 次；
          已用 {consent.used_bytes} 字节；截止 {formatDateTime(consent.expires_at)}</p>
        <details><summary>查看原始绑定与范围</summary>
          <pre className="workflow-result-text">{JSON.stringify(consent, null, 2)}</pre>
        </details>
        {state === "active" ? <button type="button" className="btn btn-sm"
          data-testid="consent-revoke" disabled={pending} onClick={() => void operate("revoke", consent)}>
          撤销授权</button> : null}
      </article>; })}
    </div>
  </section>;
}
