// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import type { Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { createAgentApi } from "../api/agent";
import type { HostClient, RpcParams } from "../api/host";
import { HostRpcError } from "../api/errors";
import { DemoHostClient } from "../demo/host";
import { AgentGoalPanel } from "../components/AgentGoalPanel";
import { ScopedConsentPanel } from "../components/ScopedConsentPanel";
import {
  activeConsent, consentPreview, contentRef, digest, humanRequest, loopGoal,
  loopPlan, loopTask, otherDigest, policy,
} from "./agent-loop-fixtures";

Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
let root: Root;
let container: HTMLDivElement;
beforeEach(() => {
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});
afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
});
function host(handler: (method: string, params: RpcParams) => Promise<unknown>): HostClient {
  return {
    mode: "host", rpc: vi.fn(handler) as HostClient["rpc"],
    getState: vi.fn(), openEvents: vi.fn(),
  };
}
async function input(selector: string, value: string) {
  await act(async () => {
    const element = container.querySelector(selector)!;
    const prototype = element instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype
      : element instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(prototype, "value")!.set!.call(element, value);
    element.dispatchEvent(new Event(element instanceof HTMLSelectElement ? "change" : "input",
      { bubbles: true }));
  });
}
async function click(selector: string) {
  await act(async () => (container.querySelector(selector)! as HTMLButtonElement).click());
}
function panel(client: HostClient, head = 1, task = loopTask, changed = vi.fn()) {
  return <AgentGoalPanel api={createAgentApi(client)} task={task} eventSeqHead={head}
    executionPolicy={policy} onChanged={changed} onSelectRun={vi.fn()} />;
}

it("V2 创建只保存；有限自动模式显式绑定累计请求、期限与轮次", async () => {
  let saved = false;
  const client = host(async (method, params) => {
    if (method === "goal.v2.get") return { goal: saved ? loopGoal : null };
    if (method === "goal.v2.create") {
      expect(params).toMatchObject({
        task_id: "task_1", goal_revision: 1, criteria: ["报告包含来源"], max_rounds: 3,
        mode: "bounded_auto", completion_policy: "human", replan_policy: "confirm",
        budget_limits: { requests: 32 }, max_no_progress_rounds: 2, execution_policy: policy,
      });
      expect(Number(params.deadline_ms)).toBeGreaterThan(Date.now());
      saved = true;
      return { goal: loopGoal };
    }
    throw new Error(`未预期 RPC ${method}`);
  });
  await act(async () => root.render(panel(client)));
  await click("[data-testid='agent-open']");
  await input("[data-testid='agent-criteria']", "报告包含来源");
  await input("[data-testid='agent-mode']", "bounded_auto");
  await click("[data-testid='agent-create']");
  expect(saved).toBe(true);
  expect(vi.mocked(client.rpc).mock.calls.some(([method]) => method === "run.start")).toBe(false);
});

it("实际计划正文和检查可审阅；确认发送当前修订与精确摘要", async () => {
  let confirmed = false;
  const client = host(async (method, params) => {
    if (method === "goal.v2.get") return { goal: { ...loopGoal, plan_id: "plan_1" } };
    if (method === "plan.graph.get") return {
      plan: { ...loopPlan, confirmed }, verification_requests: [],
    };
    if (method === "plan.graph.confirm") {
      expect(params).toEqual({
        plan_id: "plan_1", expected_revision: 1, document_digest: otherDigest,
        request_id: expect.any(String),
      });
      confirmed = true;
      return { plan: { ...loopPlan, confirmed }, verification_requests: [] };
    }
    throw new Error(method);
  });
  await act(async () => root.render(panel(client)));
  expect(container.textContent).toContain("核对原始资料后生成报告");
  expect(container.textContent).toContain("来源是否可核对");
  const button = () => container.querySelector<HTMLButtonElement>("[data-testid='plan-confirm']")!;
  expect(button().disabled).toBe(true);
  await click("[data-testid='plan-reviewed']");
  await click("[data-testid='plan-confirm']");
  expect(confirmed).toBe(true);
});

it.each(["accept", "reject"])("人工 %s 必须先加载冻结证据，决定绑定当前 attempt 与输入指纹", async (decision) => {
  let decided = false;
  const client = host(async (method, params) => {
    if (method === "goal.v2.get") return { goal: { ...loopGoal, plan_id: "plan_1" } };
    if (method === "plan.graph.get") return {
      plan: { ...loopPlan, confirmed: true }, verification_requests: decided ? [] : [humanRequest],
    };
    if (method === "agent.material") return { material: {
      kind: "human_decision", content_refs: [`content://sha256:${otherDigest}`],
    } };
    if (method === "verification.decide") {
      expect(params).toMatchObject({
        format_version: 1, task_id: "task_1", mission_digest: digest, plan_digest: otherDigest,
        plan_id: "plan_1", revision: 1, step_id: "step_1", check_id: "human_1",
        check_digest: digest, attempt_id: "attempt_1", expected_state_revision: 20,
        input_fingerprint: otherDigest, evidence_refs: [contentRef], decision, reason: "已逐项核对",
      });
      expect(params).not.toHaveProperty("request_old");
      decided = true;
      return { result: { result: decision === "accept" ? "passed" : "failed" } };
    }
    throw new Error(method);
  });
  await act(async () => root.render(panel(client)));
  expect(container.querySelector<HTMLButtonElement>(`[data-testid='human-${decision}']`)!.disabled).toBe(true);
  await click("[data-testid='human-load']");
  expect(container.textContent).toContain("human_decision");
  await input("[data-testid='human-reason']", "已逐项核对");
  await click("[data-testid='human-reviewed']");
  await click(`[data-testid='human-${decision}']`);
  expect(decided).toBe(true);
});

it("冲突后刷新当前计划并撤销原摘要确认", async () => {
  let conflicted = false;
  const client = host(async (method) => {
    if (method === "goal.v2.get") return { goal: { ...loopGoal, plan_id: "plan_1" } };
    if (method === "plan.graph.get") return {
      plan: { ...loopPlan, revision: conflicted ? 2 : 1, document_digest: conflicted ? digest : otherDigest },
      verification_requests: [],
    };
    if (method === "plan.graph.confirm") {
      conflicted = true;
      throw new HostRpcError("conflict", "revision changed");
    }
    throw new Error(method);
  });
  await act(async () => root.render(panel(client)));
  await click("[data-testid='plan-reviewed']");
  await click("[data-testid='plan-confirm']");
  expect(container.textContent).toContain("冲突");
  expect(container.querySelector<HTMLButtonElement>("[data-testid='plan-confirm']")!.disabled).toBe(true);
  expect(container.textContent).toContain("修订 2");
});

it("暂停等待安全点，恢复和取消均携带 CAS，普通恢复不冒充阻断恢复", async () => {
  let goal = { ...loopGoal, status: "active", phase_run_id: "run_1" };
  const calls: string[] = [];
  const client = host(async (method, params) => {
    if (method === "goal.v2.get") return { goal };
    expect(params.expected_state_revision).toBe(goal.state_revision);
    calls.push(method);
    if (method === "goal.v2.pause") goal = {
      ...goal, status: "paused", pause_pending: true, state_revision: 11,
    };
    if (method === "goal.v2.resume") goal = { ...goal, status: "active", state_revision: 13 };
    if (method === "goal.v2.close") goal = { ...goal, status: "cancelled", state_revision: 14 };
    return { goal };
  });
  const api = createAgentApi(client);
  const render = (head = 1) => <AgentGoalPanel api={api} task={loopTask} eventSeqHead={head}
    executionPolicy={policy} onChanged={vi.fn()} onSelectRun={vi.fn()} />;
  await act(async () => root.render(render()));
  await click("[data-testid='agent-pause']");
  expect(container.textContent).toContain("安全点");
  expect(container.querySelector<HTMLButtonElement>("[data-testid='agent-resume']")!.disabled).toBe(true);
  goal = { ...goal, pause_pending: false, state_revision: 12 };
  await act(async () => root.render(render(2)));
  await click("[data-testid='agent-resume']");
  await click("[data-testid='agent-cancel']");
  expect(calls).toEqual(["goal.v2.pause", "goal.v2.resume", "goal.v2.close"]);
});

it("显式阻断恢复携带阻断修订、证据及决定；不能通过人工完成洗白未知副作用", async () => {
  let recovered = false;
  const goal = { ...loopGoal, status: "blocked", blocker: {
    category: "execution_failed", source_event_seq: 8, safe_retry: true,
    available_actions: ["cancel", "recover"], denied_content_digest: null,
  } };
  const client = host(async (method, params) => {
    if (method === "goal.v2.get") return { goal };
    if (method === "goal.v2.recover") {
      expect(params).toMatchObject({
        goal_id: "goal_2", expected_state_revision: 10, expected_block_event_seq: 10,
        resolution: "retry", evidence_refs: [contentRef],
      });
      recovered = true;
      return { goal: loopGoal };
    }
    throw new Error(method);
  });
  await act(async () => root.render(panel(client)));
  expect(container.querySelector<HTMLButtonElement>("[data-testid='agent-complete']")!.disabled).toBe(true);
  await input("[data-testid='agent-recovery-evidence']", contentRef);
  await click("[data-testid='agent-recover']");
  expect(recovered).toBe(true);
});

it("Task 修订变化后不接收旧 V2 读取，卸载后不触发后续回调", async () => {
  let resolve!: (value: unknown) => void;
  const changed = vi.fn();
  const client = host(async () => new Promise((finish) => { resolve = finish; }));
  const api = createAgentApi(client);
  const render = (revision: number) => <AgentGoalPanel api={api} task={{ ...loopTask, goal_revision: revision }}
    eventSeqHead={1} executionPolicy={policy} onChanged={changed} onSelectRun={vi.fn()} />;
  await act(async () => root.render(render(1)));
  const old = resolve;
  await act(async () => root.render(render(2)));
  await act(async () => old({ goal: loopGoal }));
  expect(container.textContent).not.toContain("0 / 3");
  await act(async () => root.render(<div />));
  await act(async () => resolve({ goal: loopGoal }));
  expect(changed).not.toHaveBeenCalled();
});

it("有限授权必须预览与确认才签发；改范围撤销预览，签发后可撤销活动授权", async () => {
  let issued = false;
  let revoked = false;
  const client = host(async (method, params) => {
    if (method === "consent.list") return { consents: issued ? [{
      ...activeConsent, state: revoked ? "revoked" : "active",
    }] : [] };
    if (method === "consent.prepare") {
      expect(params).toMatchObject({ run_id: "run_1", kind: "directory_overwrite", directory: "/work" });
      return { preview: consentPreview };
    }
    if (method === "consent.issue") {
      expect(params).toMatchObject({
        run_id: "run_1", goal_revision: 1, workspace_binding_digest: digest,
        mission_digest: digest, plan_digest: otherDigest, action: "overwrite",
        tool_binding: consentPreview.tool_binding, matcher: consentPreview.matcher, max_uses: 2,
      });
      expect(params).not.toHaveProperty("task_id");
      issued = true;
      return { consent: activeConsent };
    }
    if (method === "consent.revoke") {
      expect(params.consent_id).toBe("consent_1");
      revoked = true;
      return { consent: { ...activeConsent, state: "revoked" } };
    }
    throw new Error(method);
  });
  await act(async () => root.render(<ScopedConsentPanel api={createAgentApi(client)}
    task={loopTask} runId="run_1" eventSeqHead={1} />));
  await input("[data-testid='consent-target']", "/work");
  await input("[data-testid='consent-uses']", "2");
  await click("[data-testid='consent-preview']");
  expect(container.textContent).toContain("262144");
  expect(container.querySelector<HTMLButtonElement>("[data-testid='consent-issue']")!.disabled).toBe(true);
  await click("[data-testid='consent-reviewed']");
  await input("[data-testid='consent-target']", "/work/sub");
  expect(container.querySelector("[data-testid='consent-issue']")).toBeNull();
  await input("[data-testid='consent-target']", "/work");
  await click("[data-testid='consent-preview']");
  await click("[data-testid='consent-reviewed']");
  await click("[data-testid='consent-issue']");
  expect(issued).toBe(true);
  await click("[data-testid='consent-revoke']");
  expect(revoked).toBe(true);
});

it("未知封闭状态或畸形计划不进入可操作界面", async () => {
  const client = host(async (method) => {
    if (method === "goal.v2.get") return { goal: {
      ...loopGoal, status: "blocked", blocker: {
        category: "new_success", source_event_seq: 9, safe_retry: true,
        available_actions: ["recover"], denied_content_digest: null,
      },
    } };
    throw new Error(method);
  });
  await act(async () => root.render(panel(client)));
  expect(container.textContent).toContain("数据格式无效");
  expect(container.querySelector("[data-testid='agent-recover']")).toBeNull();
});

it("证据依赖加载失败时不能接受或拒绝；计划更新会使已审阅证据失效", async () => {
  let changed = false;
  const client = host(async (method, params) => {
    if (method === "goal.v2.get") return { goal: { ...loopGoal, plan_id: "plan_1" } };
    if (method === "plan.graph.get") return {
      plan: { ...loopPlan, state_revision: changed ? 21 : 20, confirmed: true },
      verification_requests: [{ ...humanRequest, attempt_id: changed ? "attempt_2" : "attempt_1" }],
    };
    if (method === "agent.material") {
      if (params.content_ref === contentRef) return { material: { content_refs: [`content://sha256:${otherDigest}`] } };
      throw new Error("证据已不可读取");
    }
    throw new Error(method);
  });
  const api = createAgentApi(client);
  const render = (head: number) => <AgentGoalPanel api={api} task={loopTask} eventSeqHead={head}
    executionPolicy={policy} onChanged={vi.fn()} onSelectRun={vi.fn()} />;
  await act(async () => root.render(render(1)));
  await click("[data-testid='human-load']");
  expect(container.textContent).toContain("证据已不可读取");
  expect(container.querySelector<HTMLButtonElement>("[data-testid='human-accept']")!.disabled).toBe(true);
  changed = true;
  await act(async () => root.render(render(2)));
  expect(container.querySelector<HTMLInputElement>("[data-testid='human-reviewed']")!.checked).toBe(false);
  expect(container.textContent).toContain("attempt_2");
});

it("卸载或切换 Task 后的迟到写入不会刷新旧任务或触发回调", async () => {
  let finish!: (value: unknown) => void;
  const changed = vi.fn();
  let reads = 0;
  const client = host(async (method) => {
    if (method === "goal.v2.get") { reads += 1; return { goal: loopGoal }; }
    if (method === "goal.v2.advance") return new Promise((resolve) => { finish = resolve; });
    throw new Error(method);
  });
  const api = createAgentApi(client);
  await act(async () => root.render(<AgentGoalPanel api={api} task={loopTask} eventSeqHead={1}
    executionPolicy={policy} onChanged={changed} onSelectRun={vi.fn()} />));
  await click("[data-testid='agent-advance']");
  await act(async () => root.render(<div />));
  await act(async () => finish({ goal: { ...loopGoal, phase_run_id: "run_late" } }));
  expect(reads).toBe(1);
  expect(changed).not.toHaveBeenCalled();
});

it("未知副作用阻断不提供人工完成，恢复不重置预算并需显式核对证据", async () => {
  const client = host(async () => ({ goal: {
    ...loopGoal, status: "blocked", round: 2, blocker: {
      category: "effect_unknown", source_event_seq: 9, safe_retry: false,
      available_actions: ["cancel", "recover"], denied_content_digest: null,
    },
  } }));
  await act(async () => root.render(panel(client)));
  expect(container.textContent).toContain("原工具核对");
  expect(container.textContent).toContain("2 / 3");
  expect(container.querySelector<HTMLButtonElement>("[data-testid='agent-complete']")!.disabled).toBe(true);
  expect(container.querySelector<HTMLButtonElement>("[data-testid='agent-recover']")!.disabled).toBe(true);
});

it("演示模式不假造目标和授权事实，也不因只读新接口报错", async () => {
  const api = createAgentApi(new DemoHostClient());
  expect(await api.getGoal("demo")).toBeNull();
  expect(await api.listConsents("run_demo")).toEqual([]);
  await expect(api.createGoal(loopTask, { criteria: ["验收条件"], max_rounds: 3 },
    policy, "request_demo")).rejects.toThrow("演示模式");
});

it("Run 的 partial 状态尚未形成 Goal blocker 时也不能人工验收或完成", async () => {
  const client = host(async (method) => {
    if (method === "goal.v2.get") return { goal: {
      ...loopGoal, status: "awaiting_review", phase: "finish", plan_id: "plan_1",
    } };
    if (method === "plan.graph.get") return {
      plan: { ...loopPlan, confirmed: true, steps: loopPlan.steps.map((step) => ({ ...step, state: "satisfied" })) },
      verification_requests: [humanRequest],
    };
    throw new Error(method);
  });
  await act(async () => root.render(<AgentGoalPanel api={createAgentApi(client)} task={loopTask}
    eventSeqHead={1} executionPolicy={policy} hasUncertainEffects onChanged={vi.fn()} onSelectRun={vi.fn()} />));
  expect(container.querySelector<HTMLButtonElement>("[data-testid='agent-complete']")!.disabled).toBe(true);
  expect(container.querySelector<HTMLButtonElement>("[data-testid='human-load']")!.disabled).toBe(true);
});

it("创建重试复用原绝对期限和请求 ID，避免重试时扩大自动预算", async () => {
  const creates: RpcParams[] = [];
  const client = host(async (method, params) => {
    if (method === "goal.v2.get") return { goal: null };
    if (method === "goal.v2.create") { creates.push(params); throw new Error("断开连接"); }
    throw new Error(method);
  });
  await act(async () => root.render(panel(client)));
  await click("[data-testid='agent-open']");
  await input("[data-testid='agent-criteria']", "报告包含来源");
  await click("[data-testid='agent-create']");
  await click("[data-testid='agent-create']");
  expect(creates).toHaveLength(2);
  expect(creates[1]).toEqual(creates[0]);
});

it("切换 Host API 后可重新推进，旧 API 的迟到错误不污染新界面", async () => {
  let reject!: (reason: unknown) => void;
  const first = host(async (method) => {
    if (method === "goal.v2.get") return { goal: loopGoal };
    return new Promise((_resolve, fail) => { reject = fail; });
  });
  let advanced = false;
  const second = host(async (method) => {
    if (method === "goal.v2.advance") advanced = true;
    return { goal: loopGoal };
  });
  const render = (api: ReturnType<typeof createAgentApi>) => <AgentGoalPanel api={api}
    task={loopTask} eventSeqHead={1} executionPolicy={policy} onChanged={vi.fn()} onSelectRun={vi.fn()} />;
  await act(async () => root.render(render(createAgentApi(first))));
  await click("[data-testid='agent-advance']");
  await act(async () => root.render(render(createAgentApi(second))));
  await act(async () => reject(new Error("旧连接错误")));
  expect(container.textContent).not.toContain("旧连接错误");
  await click("[data-testid='agent-advance']");
  expect(advanced).toBe(true);
});

it("精确命令预览使用已有 dispatcher request，绑定冲突后必须重新预览", async () => {
  let prepared = false;
  const matcher = {
    kind: "exact_command", executable_path: "/usr/bin/git", executable_digest: digest,
    argv: ["/usr/bin/git", "status"], cwd: "/work", environment_digest: otherDigest,
    timeout_ms: 30000, isolation_profile_digest: digest, pty: false,
  };
  const client = host(async (method, params) => {
    if (method === "consent.list") return { consents: [] };
    if (method === "consent.prepare") {
      expect(params).toEqual({ run_id: "run_1", kind: "exact_command", request_id: "request_command" });
      prepared = true;
      return { preview: { ...consentPreview, action: "exec", matcher } };
    }
    if (method === "consent.issue") {
      expect(params.matcher).toEqual(matcher);
      throw new HostRpcError("conflict", "Plan changed");
    }
    throw new Error(method);
  });
  await act(async () => root.render(<ScopedConsentPanel api={createAgentApi(client)}
    task={loopTask} runId="run_1" eventSeqHead={1} />));
  await input("select", "exact_command");
  await input("[data-testid='consent-target']", "request_command");
  await click("[data-testid='consent-preview']");
  expect(prepared).toBe(true);
  await click("[data-testid='consent-reviewed']");
  await click("[data-testid='consent-issue']");
  expect(container.textContent).toContain("冲突");
  expect(container.querySelector("[data-testid='consent-issue']")).toBeNull();
});
