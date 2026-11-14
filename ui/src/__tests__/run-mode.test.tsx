// @vitest-environment jsdom

import { act } from "react";
import { createRoot } from "react-dom/client";
import type { Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type {
  ApprovalView,
  EventsResult,
  RunEnvelope,
  RunView,
  StateView,
  TaskEnvelope,
  TaskView,
} from "../api/types";
import type { EventStreamCallbacks, HostClient, RpcParams } from "../api/host";
import { HostRpcError } from "../api/errors";
import { DemoHostClient } from "../demo/host";
import { DemoWorld } from "../demo/world";
import { buildBudgetView } from "../lib/budgetView";
import { buildSubtaskViews } from "../lib/subtasks";

const mocked = vi.hoisted(() => ({
  host: null as unknown,
}));

vi.mock("../api/host", () => ({
  createRealHostClient: () => mocked.host,
}));

vi.mock("../api/token", () => ({
  isDemoRequested: () => false,
  readHostToken: () => "test-token",
}));

import { App } from "../App";

const EFFECTS = { format_version: 1, mode: "effects" } as const;
const PURE_ONLY = { format_version: 1, mode: "pure_only" } as const;

function task(overrides: Partial<TaskView> = {}): TaskView {
  return {
    task_id: "task_1",
    control: false,
    goal: "整理资料",
    goal_revision: 1,
    status: "created",
    active_run_id: null,
    workspaces: [{ root_path: "/tmp/workspace", mode: "writable" }],
    created_at: 1,
    updated_at: 1,
    ...overrides,
  };
}

function run(taskId: string, index: number): RunView {
  return {
    run_id: `run_${index}`,
    task_id: taskId,
    state: "queued",
    outcome: null,
    effect_state: "none_applied",
    control_requested: null,
    goal_revision: 1,
    lease_epoch: 1,
    started_at: index,
    ended_at: null,
    n_applied: 0,
    n_unknown: 0,
    undoable: false,
  };
}

function state(tasks: TaskView[] = [], runs: RunView[] = []): StateView {
  return {
    tasks,
    runs,
    approvals: [] as ApprovalView[],
    event_seq_head: 1,
    daemon: {
      version: "test",
      started_at: 1,
      data_root: "/tmp/data",
    },
  };
}

class FakeHost implements HostClient {
  readonly mode = "host" as const;
  readonly state: StateView;
  readonly rpc = vi.fn(async (method: string, params: RpcParams = {}) => {
    if (method === "task.create") {
      const created = task({
        task_id: "task_created",
        goal: String(params.goal),
        workspaces: params.workspaces as TaskView["workspaces"],
      });
      this.state.tasks.unshift(created);
      const envelope: TaskEnvelope = { task: structuredClone(created) };
      return envelope;
    }
    if (method === "run.start") {
      if (this.failuresRemaining > 0) {
        this.failuresRemaining -= 1;
        throw new HostRpcError("internal", "dispatch.disk_space_low: 可用空间低于 2 GiB");
      }
      const started = run(String(params.task_id), this.state.runs.length + 1);
      const selected = this.state.tasks.find((item) => item.task_id === started.task_id);
      if (selected !== undefined) {
        selected.active_run_id = started.run_id;
        selected.status = "queued";
      }
      this.state.runs.unshift(started);
      const envelope: RunEnvelope = { run: structuredClone(started) };
      return envelope;
    }
    if (method === "events.run") {
      return { events: [], seq_head: 1, has_more: false };
    }
    if (method === "run.status") {
      const selected = this.state.runs.find((item) => item.run_id === params.run_id);
      if (selected !== undefined) return { run: structuredClone(selected) };
    }
    return {};
  }) as unknown as HostClient["rpc"];

  failuresRemaining = 0;

  constructor(initial: StateView) {
    this.state = structuredClone(initial);
  }

  async getState(): Promise<StateView> {
    return structuredClone(this.state);
  }

  async openEvents(
    _sinceSeq: number,
    callbacks: EventStreamCallbacks,
    signal: AbortSignal,
  ): Promise<void> {
    callbacks.onOpen?.();
    return new Promise<void>((resolve) => {
      if (signal.aborted) {
        resolve();
        return;
      }
      signal.addEventListener("abort", () => resolve(), { once: true });
    });
  }
}

let root: Root | null = null;
let container: HTMLDivElement | null = null;

beforeEach(() => {
  (
    globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }
  ).IS_REACT_ACT_ENVIRONMENT = true;
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});

afterEach(async () => {
  if (root !== null) {
    await act(async () => root?.unmount());
  }
  container?.remove();
  root = null;
  container = null;
  mocked.host = null;
  vi.clearAllMocks();
});

async function settle(): Promise<void> {
  await act(async () => {
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
  });
}

async function waitFor(condition: () => boolean): Promise<void> {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    if (condition()) return;
    await settle();
  }
  throw new Error("等待 UI 条件超时");
}

function buttonByText(text: string): HTMLButtonElement | null {
  if (container === null) return null;
  return (
    [...container.querySelectorAll("button")].find(
      (button) => button.textContent?.trim() === text,
    ) ?? null
  );
}

async function click(target: HTMLElement): Promise<void> {
  await act(async () => {
    target.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  });
}

async function inputValue(target: HTMLInputElement | HTMLTextAreaElement, value: string): Promise<void> {
  const prototype =
    target instanceof HTMLTextAreaElement
      ? HTMLTextAreaElement.prototype
      : HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(prototype, "value")?.set;
  if (setter === undefined) throw new Error("找不到原生 value setter");
  await act(async () => {
    setter.call(target, value);
    target.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

function rpcCalls(host: FakeHost, method: string): RpcParams[] {
  const mockedRpc = host.rpc as unknown as {
    mock: { calls: Array<[string, RpcParams]> };
  };
  return mockedRpc.mock.calls
    .filter(([called]) => called === method)
    .map(([, params]) => params);
}

async function mount(initial: StateView, failuresRemaining = 0): Promise<FakeHost> {
  const host = new FakeHost(initial);
  host.failuresRemaining = failuresRemaining;
  mocked.host = host;
  await act(async () => root?.render(<App />));
  await waitFor(() => buttonByText("新建任务") !== null);
  return host;
}

describe("Run 执行模式的真实 UI 入口", () => {
  it("新建任务只把纯读策略交给 run.start，启动失败后的详情重试仍保持 pure_only", async () => {
    const host = await mount(state(), 1);
    const newTask = buttonByText("新建任务");
    expect(newTask).not.toBeNull();
    await click(newTask as HTMLButtonElement);
    await waitFor(() => container?.querySelector('[role="dialog"]') !== null);

    const pure = buttonByText("仅读取与对话");
    expect(pure).not.toBeNull();
    await click(pure as HTMLButtonElement);

    const goal = container?.querySelector("textarea");
    const workspace = container?.querySelector<HTMLInputElement>(
      'input[aria-label="第 1 个目录路径"]',
    );
    expect(goal).not.toBeNull();
    expect(workspace).not.toBeNull();
    await inputValue(goal as HTMLTextAreaElement, "只整理，不写文件");
    await inputValue(workspace as HTMLInputElement, "/tmp/readonly-workspace");

    const submit = buttonByText("创建并开始运行");
    expect(submit).not.toBeNull();
    await click(submit as HTMLButtonElement);
    await waitFor(() => buttonByText("开始运行") !== null);
    expect(container?.textContent).toContain("dispatch.disk_space_low");

    const taskCreates = rpcCalls(host, "task.create");
    const firstStarts = rpcCalls(host, "run.start");
    expect(taskCreates).toEqual([
      {
        goal: "只整理，不写文件",
        workspaces: [
          { root_path: "/tmp/readonly-workspace", mode: "writable" },
        ],
      },
    ]);
    expect(firstStarts).toEqual([
      { task_id: "task_created", execution_policy: PURE_ONLY },
    ]);

    const retry = buttonByText("开始运行");
    expect(retry).not.toBeNull();
    await click(retry as HTMLButtonElement);
    await waitFor(() => rpcCalls(host, "run.start").length === 2);
    expect(rpcCalls(host, "run.start")).toEqual([
      { task_id: "task_created", execution_policy: PURE_ONLY },
      { task_id: "task_created", execution_policy: PURE_ONLY },
    ]);
  });

  it.each([
    ["默认允许工具操作", null, EFFECTS],
    ["显式仅读取与对话", "仅读取与对话", PURE_ONLY],
  ] as const)("已有任务：%s 会原样传到 run.start", async (_label, selection, policy) => {
    const existing = task();
    const host = await mount(state([existing]));
    await waitFor(() => buttonByText("开始运行") !== null);
    if (selection !== null) {
      const selector = buttonByText(selection);
      expect(selector).not.toBeNull();
      await click(selector as HTMLButtonElement);
    }
    const start = buttonByText("开始运行");
    expect(start).not.toBeNull();
    await click(start as HTMLButtonElement);
    await waitFor(() => rpcCalls(host, "run.start").length === 1);
    expect(rpcCalls(host, "run.start")).toEqual([
      { task_id: "task_1", execution_policy: policy },
    ]);
  });

  it("演示模式把同一策略写进 run.started，仅证明离线接线", async () => {
    vi.useFakeTimers();
    try {
      const host = new DemoHostClient(new DemoWorld(1));
      const created = await host.rpc<TaskEnvelope>("task.create", {
        goal: "演示纯读",
        workspaces: [{ root_path: "/tmp/demo", mode: "writable" }],
      });
      const started = await host.rpc<RunEnvelope>("run.start", {
        task_id: created.task.task_id,
        execution_policy: PURE_ONLY,
      });
      const result = await host.rpc<EventsResult>("events.run", {
        run_id: started.run.run_id,
      });
      const event = result.events.find((item) => item.type === "run.started");
      expect(event?.payload.execution_policy).toEqual(PURE_ONLY);
    } finally {
      vi.clearAllTimers();
      vi.useRealTimers();
    }
  });

  it("演示世界的事件能驱动预算与子任务面板（W8 判据 J12）", async () => {
    vi.useFakeTimers();
    try {
      const host = new DemoHostClient(new DemoWorld(1));
      const created = await host.rpc<TaskEnvelope>("task.create", {
        goal: "演示预算与子任务",
        workspaces: [{ root_path: "/tmp/demo", mode: "writable" }],
      });
      const started = await host.rpc<RunEnvelope>("run.start", {
        task_id: created.task.task_id,
        execution_policy: EFFECTS,
      });
      // 演示世界的动作都挂在 window.setTimeout 上：推到收尾之后。
      await act(async () => {
        vi.advanceTimersByTime(5_000);
      });
      const result = await host.rpc<EventsResult>("events.run", {
        run_id: started.run.run_id,
      });
      const events = result.events;

      const budget = buildBudgetView(events);
      expect(budget).not.toBeNull();
      expect(budget?.limits).toEqual({ maxTurns: 8, maxTotalTokens: 12_000 });
      expect(budget?.results).toBe(2);
      expect(budget?.inputTokens).toBe(1_150);
      expect(budget?.outputTokens).toBe(218);
      expect(budget?.unknownUsageAttempts).toBe(0);
      expect(budget?.cost.knownCalls).toBe(2);
      expect(budget?.cost.totalMicros).toBe(8_900); // 0.0051 + 0.0038 美元

      const subtasks = buildSubtaskViews(events, []);
      expect(subtasks).toHaveLength(1);
      expect(subtasks[0]?.finishedState).toBe("succeeded");
      expect(subtasks[0]?.limits).toEqual({
        maxTurns: 2,
        maxTokens: 3_000,
        deadlineMs: 300_000,
      });
      expect(subtasks[0]?.usage).toEqual({ turns: 2, tokens: 1_842 });
    } finally {
      vi.clearAllTimers();
      vi.useRealTimers();
    }
  });
});
