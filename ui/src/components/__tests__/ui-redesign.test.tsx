// @vitest-environment jsdom

import { act } from "react";
import { createRoot } from "react-dom/client";
import type { Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type {
  ApprovalDecisionResult,
  ApprovalView,
  EventsResult,
  EventView,
  RunEnvelope,
  RunView,
  StateView,
  TaskEnvelope,
  TaskView,
} from "../../api/types";
import type { EventStreamCallbacks, HostClient, RpcParams } from "../../api/host";

const mocked = vi.hoisted(() => ({
  host: null as unknown,
}));

vi.mock("../../api/host", () => ({
  createRealHostClient: () => mocked.host,
}));

vi.mock("../../api/token", () => ({
  isDemoRequested: () => false,
  readHostToken: () => "test-token",
}));

import { App } from "../../App";

function makeTask(overrides: Partial<TaskView> = {}): TaskView {
  return {
    task_id: "task_alpha",
    control: false,
    goal: "重构登录状态机",
    goal_revision: 1,
    status: "running",
    active_run_id: "run_alpha",
    workspaces: [{ root_path: "/Volumes/Work/proj-alpha", mode: "writable" }],
    created_at: 1_700_000_000_000,
    updated_at: 1_700_000_060_000,
    ...overrides,
  };
}

function makeRun(overrides: Partial<RunView> = {}): RunView {
  return {
    run_id: "run_alpha",
    task_id: "task_alpha",
    state: "waiting_approval",
    outcome: null,
    effect_state: "none_applied",
    control_requested: null,
    goal_revision: 1,
    lease_epoch: 1,
    started_at: 1_700_000_010_000,
    ended_at: null,
    n_applied: 0,
    n_unknown: 0,
    undoable: false,
    ...overrides,
  };
}

function makeApproval(overrides: Partial<ApprovalView> = {}): ApprovalView {
  return {
    approval_id: "ap_1",
    run_id: "run_alpha",
    task_id: "task_alpha",
    action: "c4.overwrite",
    resources: ["/Volumes/Work/proj-alpha/src/auth.ts"],
    reason: "覆盖已有认证实现",
    rule_id: "c4.overwrite",
    isolation: null,
    command: null,
    state: "pending",
    created_at: Date.now() - 5_000,
    expires_at: Date.now() + 600_000,
    ...overrides,
  };
}

function makeState(
  tasks: TaskView[] = [],
  runs: RunView[] = [],
  approvals: ApprovalView[] = [],
): StateView {
  return {
    tasks,
    runs,
    approvals,
    event_seq_head: 42,
    daemon: {
      version: "0.1.0",
      started_at: Date.now() - 120_000,
      data_root: "/tmp/tianshu-data",
    },
  };
}

class RedesignFakeHost implements HostClient {
  readonly mode = "host" as const;
  readonly state: StateView;
  streamCallbacks: EventStreamCallbacks | null = null;
  modelConfig = {
    mode: "scripted",
    model_name: "",
    base_url: "",
    credential_backend: "env",
    summary: "演示脚本模型（离线）",
    allow_image_attachments: false,
    compact_threshold: undefined as number | undefined,
  };

  readonly rpc = vi.fn(async (method: string, params: RpcParams = {}) => {
    if (method === "settings.get") {
      return {
        model: { ...this.modelConfig },
        credentials: [],
      };
    }
    if (method === "model.configure") {
      const nextMode = String(params.mode ?? "scripted");
      const nextName = String(params.model_name ?? "");
      this.modelConfig = {
        mode: nextMode,
        model_name: nextName,
        base_url: String(params.base_url ?? ""),
        credential_backend: String(params.credential_backend ?? "env"),
        summary: nextMode === "scripted" ? "演示脚本模型（离线）" : `${nextMode} · ${nextName}`,
        allow_image_attachments: params.allow_image_attachments === true,
        compact_threshold: typeof params.compact_threshold === "number"
          ? params.compact_threshold : undefined,
      };
      return {
        model: { ...this.modelConfig },
        credentials: [],
      };
    }
    if (method === "task.create") {
      const id = `task_new_${this.state.tasks.length + 1}`;
      const created = makeTask({
        task_id: id,
        goal: String(params.goal),
        status: "created",
        active_run_id: null,
        workspaces: (params.workspaces as TaskView["workspaces"]) ?? [],
      });
      this.state.tasks.unshift(created);
      return { task: structuredClone(created) } satisfies TaskEnvelope;
    }
    if (["run.start", "run.follow_up", "run.answer_clarification"].includes(method)) {
      const runId = `run_new_${this.state.runs.length + 1}`;
      const started = makeRun({
        run_id: runId,
        task_id: String(params.task_id),
        state: "running",
      });
      const target = this.state.tasks.find((item) => item.task_id === started.task_id);
      if (target !== undefined) {
        target.active_run_id = started.run_id;
        target.status = "running";
      }
      this.state.runs.unshift(started);
      return { run: structuredClone(started) } satisfies RunEnvelope;
    }
    if (method === "run.status") {
      const found = this.state.runs.find((item) => item.run_id === params.run_id);
      return { run: structuredClone(found ?? makeRun()) };
    }
    if (method === "events.run") {
      return { events: this.events, seq_head: this.state.event_seq_head, has_more: false } satisfies EventsResult;
    }
    if (method === "run.compact" || method === "plan.execute") {
      const started = makeRun({
        run_id: "workflow_new", task_id: "task_alpha", state: "created",
      });
      this.state.runs.unshift(started);
      this.state.tasks[0]!.active_run_id = started.run_id;
      return { run: started };
    }
    if (method === "approvals.decide") {
      const target = this.state.approvals.find(
        (item) => item.approval_id === params.approval_id,
      );
      if (target !== undefined) {
        target.state = params.decision === "approve" ? "approved" : "rejected";
      }
      return {
        approval: structuredClone(target ?? makeApproval({ state: "approved" })),
        resumed: params.decision === "approve",
      } satisfies ApprovalDecisionResult;
    }
    if (method === "memory.list") {
      return { items: [], scopes: ["user"] };
    }
    return {};
  }) as unknown as HostClient["rpc"];

  events: EventView[] = [];

  constructor(initial: StateView = makeState()) {
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
    this.streamCallbacks = callbacks;
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
  throw new Error("等待新版界面条件超时");
}

async function click(target: HTMLElement): Promise<void> {
  await act(async () => {
    target.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  });
}

async function inputValue(
  target: HTMLInputElement | HTMLTextAreaElement,
  value: string,
): Promise<void> {
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

function rpcCalls(host: RedesignFakeHost, method: string): RpcParams[] {
  const mockedRpc = host.rpc as unknown as {
    mock: { calls: Array<[string, RpcParams]> };
  };
  return mockedRpc.mock.calls
    .filter(([called]) => called === method)
    .map(([, params]) => params);
}

describe("极简双区新界面、工作区上下文、诚实权限菜单与设置中心", () => {
  it("欢迎页精简呈现：含动态伴侣、欢迎标题、模型选择器位于发送钮左侧，且移除冗余提示词", async () => {
    const host = new RedesignFakeHost(makeState());
    mocked.host = host;
    await act(async () => root?.render(<App />));
    await waitFor(() => container?.querySelector(".welcome-view") !== null);

    expect(container?.querySelector(".ts-sidebar")).not.toBeNull();
    expect(container?.querySelector(".ts-canvas")).not.toBeNull();

    const pet = container?.querySelector('[data-testid="welcome-pet"]');
    const heading = container?.querySelector(".welcome-title");
    const composer = container?.querySelector(".composer-shell");
    expect(pet).not.toBeNull();
    expect(heading?.textContent?.trim()).toBe("欢迎使用天枢");
    expect(composer).not.toBeNull();

    // 严格核对用户要求移除的文案与命名禁令
    expect(container?.textContent).not.toContain("你好，欢迎回来");
    expect(container?.textContent).not.toContain("回车发送");
    expect(container?.innerHTML).not.toMatch(/nova/i);

    // 模型切换器必须紧挨在发送按钮正左侧（DOM 顺序位于发送按钮之前）
    const modelBtn = composer?.querySelector<HTMLButtonElement>('[data-testid="composer-model-btn"]');
    const sendBtn = composer?.querySelector<HTMLButtonElement>('[data-testid="composer-send-btn"]');
    expect(modelBtn).not.toBeNull();
    expect(sendBtn).not.toBeNull();
    const pos = modelBtn!.compareDocumentPosition(sendBtn!);
    expect(pos & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

    // 侧边栏支持收起与展开
    const collapseBtn = container?.querySelector<HTMLButtonElement>(
      'button[aria-label="收起侧栏"]',
    );
    expect(collapseBtn).not.toBeNull();
    await click(collapseBtn!);
    expect(container?.querySelector(".ts-sidebar.is-collapsed")).not.toBeNull();
  });

  it("/plan 新任务命令进入真实计划运行且不会把命令写进目标", async () => {
    const host = new RedesignFakeHost(makeState());
    mocked.host = host;
    await act(async () => root?.render(<App />));
    await waitFor(() => container?.querySelector(".composer-textarea") !== null);
    const input = container!.querySelector<HTMLTextAreaElement>(".composer-textarea")!;
    await inputValue(input, "/plan 先分析登录模块再给方案");
    await click(container!.querySelector<HTMLButtonElement>('[data-testid="composer-send-btn"]')!);
    await waitFor(() => host.state.runs.length === 1);
    expect(host.state.tasks[0]?.goal).toBe("先分析登录模块再给方案");
    const calls = (host.rpc as unknown as ReturnType<typeof vi.fn>).mock.calls;
    expect(calls.find(([method]) => method === "run.start")?.[1]).toMatchObject({ mode: "plan" });
  });

  it("/compact 触发压缩 RPC，且不会改写任务目标", async () => {
    const task = makeTask({ active_run_id: null });
    const run = makeRun({ state: "succeeded", outcome: "noop" });
    const host = new RedesignFakeHost(makeState([task], [run]));
    mocked.host = host;
    await act(async () => root?.render(<App />));
    await waitFor(() => container?.querySelector('[data-testid="followup-input"]') !== null);
    await inputValue(container!.querySelector<HTMLInputElement>('[data-testid="followup-input"]')!, "/compact");
    await click(container!.querySelector<HTMLButtonElement>('[data-testid="followup-send-btn"]')!);
    await settle();
    const calls = (host.rpc as unknown as ReturnType<typeof vi.fn>).mock.calls;
    expect(calls.some(([method]) => method === "run.compact")).toBe(true);
    expect(calls.some(([method]) => method === "task.revise")).toBe(false);
    expect(host.state.tasks[0]?.goal).toBe("重构登录状态机");
  });

  it("显示持久化计划正文并在确认后调用绑定的执行入口", async () => {
    const task = makeTask({ active_run_id: null });
    const plan: RunView = {
      ...makeRun({ state: "succeeded", outcome: "noop" }),
      workflow: { mode: "plan", source_run_id: null },
    };
    const host = new RedesignFakeHost(makeState([task], [plan]));
    host.events = [{
      seq: 42, type: "model.result", run_id: plan.run_id, task_id: task.task_id,
      ts: Date.now(), actor_kind: "core",
      payload: { content: "先检查认证边界，再修改登录状态机。", finish_reason: "end_turn" },
    }];
    mocked.host = host;
    await act(async () => root?.render(<App />));
    await waitFor(() => container?.textContent?.includes("先检查认证边界，再修改登录状态机。") === true);
    const button = container!.querySelector<HTMLButtonElement>('[data-testid="execute-plan-btn"]');
    expect(button).not.toBeNull();
    await click(button!);
    await settle();
    const calls = (host.rpc as unknown as ReturnType<typeof vi.fn>).mock.calls;
    expect(calls.find(([method]) => method === "plan.execute")?.[1]).toMatchObject({
      plan_run_id: "run_alpha", goal_revision: 1,
    });
  });

  it("计划等待澄清时不显示确认按钮，回答仍请求只读计划", async () => {
    const task = makeTask({ active_run_id: null });
    const plan: RunView = {
      ...makeRun({ state: "succeeded", outcome: "noop" }),
      workflow: { mode: "plan", source_run_id: null },
      clarification: { question: "报告给哪类读者？" },
    };
    const host = new RedesignFakeHost(makeState([task], [plan]));
    host.events = [{
      seq: 42, type: "model.result", run_id: plan.run_id, task_id: task.task_id,
      ts: Date.now(), actor_kind: "core",
      payload: { content: "TIANSHU_QUESTION: 报告给哪类读者？", finish_reason: "end_turn" },
    }];
    mocked.host = host;
    await act(async () => root?.render(<App />));
    await waitFor(() => container?.querySelector('[data-testid="clarification-question"]') != null);
    expect(container!.querySelector('[data-testid="execute-plan-btn"]')).toBeNull();
    await inputValue(container!.querySelector('[data-testid="followup-input"]')!, "新同事");
    await click(container!.querySelector('[data-testid="followup-send-btn"]')!);
    await waitFor(() => rpcCalls(host, "run.answer_clarification").length === 1);
    expect(rpcCalls(host, "run.answer_clarification")[0]).toMatchObject({
      source_run_id: plan.run_id, message: "新同事", mode: "plan", request_id: expect.any(String),
    });
    expect(rpcCalls(host, "plan.execute")).toEqual([]);
    expect(rpcCalls(host, "task.revise")).toEqual([]);
  });

  it("非项目新建任务默认留空工作区并允许直接提交 workspaces: []；点击项目工作区自动带入项目路径", async () => {
    const existing = makeTask();
    const host = new RedesignFakeHost(makeState([existing], [makeRun()]));
    mocked.host = host;
    await act(async () => root?.render(<App />));
    await waitFor(() => container?.querySelector(".stream-scroll") !== null);

    // 1. 点击「新建任务」进入非项目自由新建：工作区默认留空，可直接发送空 workspaces
    const newFreeBtn = container?.querySelector<HTMLButtonElement>(
      '[data-testid="new-free-task-btn"]',
    );
    expect(newFreeBtn).not.toBeNull();
    await click(newFreeBtn!);
    await waitFor(() => container?.querySelector(".welcome-view") !== null);

    const wsChip = container?.querySelector('[data-testid="composer-ws-btn"]');
    expect(wsChip?.textContent).toContain("选择工作区（可留空）");

    const textarea = container?.querySelector<HTMLTextAreaElement>(
      '[data-testid="composer-textarea"]',
    );
    expect(textarea).not.toBeNull();
    await inputValue(textarea!, "帮我梳理一份架构评审提纲");

    const sendBtn = container?.querySelector<HTMLButtonElement>(
      '[data-testid="composer-send-btn"]',
    );
    await click(sendBtn!);
    await waitFor(() => rpcCalls(host, "task.create").length === 1);
    expect(rpcCalls(host, "task.create")[0]).toEqual({
      goal: "帮我梳理一份架构评审提纲",
      workspaces: [],
    });

    // 2. 点击左侧「项目工作区」中的 proj-alpha：自动填入 /Volumes/Work/proj-alpha
    const projBtn = container?.querySelector<HTMLButtonElement>(
      '[data-project-path="/Volumes/Work/proj-alpha"]',
    );
    expect(projBtn).not.toBeNull();
    await click(projBtn!);
    await waitFor(() => container?.querySelector(".welcome-view") !== null);

    const prefilledChip = container?.querySelector('[data-testid="composer-ws-btn"]');
    expect(prefilledChip?.textContent).toContain("/Volumes/Work/proj-alpha");

    const textarea2 = container?.querySelector<HTMLTextAreaElement>(
      '[data-testid="composer-textarea"]',
    );
    await inputValue(textarea2!, "补充登录模块边界单测");
    await click(
      container!.querySelector<HTMLButtonElement>('[data-testid="composer-send-btn"]')!,
    );
    await waitFor(() => rpcCalls(host, "task.create").length === 2);
    expect(rpcCalls(host, "task.create")[1]).toEqual({
      goal: "补充登录模块边界单测",
      workspaces: [{ root_path: "/Volumes/Work/proj-alpha", mode: "writable" }],
    });
  });

  it("桌面只将用户填写的工具范围用于本次新任务", async () => {
    const host = new RedesignFakeHost(makeState([], []));
    mocked.host = host;
    await act(async () => root?.render(<App />));
    await waitFor(() => container?.querySelector(".welcome-view") !== null);
    const field = (id: string) =>
      container!.querySelector<HTMLInputElement>(`[data-testid="${id}"]`)!;
    await inputValue(container!.querySelector<HTMLTextAreaElement>(
      '[data-testid="composer-textarea"]',
    )!, "调查组件文档");
    expect(field("task-tools-toggle")).not.toBeNull();
    await click(field("task-tools-toggle"));
    expect(field("task-outbound-sources")).not.toBeNull();
    await inputValue(field("task-outbound-sources"), "network://https/example.com");
    await inputValue(field("task-mcp-servers"), "local-docs");
    await inputValue(field("task-subtask-limit"), "2");
    await click(field("composer-send-btn"));
    await waitFor(() => rpcCalls(host, "task.create").length === 1);
    expect(rpcCalls(host, "task.create")[0]).toEqual({
      goal: "调查组件文档", workspaces: [],
      outbound_allow: ["network://https/example.com"],
      mcp_servers: ["local-docs"], max_subtasks: 2,
    });
  });

  it("将用户选中的附件按原字节交给首次运行，不写入任务目标", async () => {
    const host = new RedesignFakeHost();
    mocked.host = host;
    await act(async () => root?.render(<App />));
    await waitFor(() => container?.querySelector('[data-testid="composer-textarea"]') !== null);
    const picker = container?.querySelector<HTMLInputElement>('input[type="file"]');
    expect(picker).toBeInstanceOf(HTMLInputElement);
    const contents = "验收材料\r\n";
    const file = new File([contents], "notes.txt", { type: "text/plain" });
    Object.defineProperty(picker, "files", { configurable: true, value: [file] });
    await act(async () => picker?.dispatchEvent(new Event("change", { bubbles: true })));
    expect(container?.textContent).toContain("notes.txt");
    await inputValue(container!.querySelector('[data-testid="composer-textarea"]')!, "阅读附件");
    await click(container!.querySelector('[data-testid="composer-send-btn"]')!);
    await waitFor(() => vi.mocked(host.rpc).mock.calls.some(([method]) => method === "run.start"));
    const calls = vi.mocked(host.rpc).mock.calls;
    expect(calls.find(([method]) => method === "task.create")?.[1]).toEqual({
      goal: "阅读附件", workspaces: [],
    });
    expect(calls.find(([method]) => method === "run.start")?.[1]?.attachments).toEqual([{
      name: "notes.txt", media_type: "text/plain",
      data_base64: "6aqM5pS25p2Q5paZDQo=",
    }]);
  });

  it("只在 Responses 模型显式支持图片后发送 PNG 附件", async () => {
    const host = new RedesignFakeHost();
    host.modelConfig = { ...host.modelConfig, mode: "openai-responses", model_name: "vision-test" };
    mocked.host = host;
    await act(async () => root?.render(<App />));
    await waitFor(() => container?.querySelector('input[type="file"]') !== null);
    const picker = container!.querySelector<HTMLInputElement>('input[type="file"]')!;
    expect(picker.accept).not.toContain("image/png");

    const base64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a4xoAAAAASUVORK5CYII=";
    const bytes = new Uint8Array([...atob(base64)].map((char) => char.charCodeAt(0)));
    const image = new File([bytes], "image.png", { type: "image/png" });
    Object.defineProperty(picker, "files", { configurable: true, value: [image] });
    await act(async () => picker.dispatchEvent(new Event("change", { bubbles: true })));
    expect(container?.textContent).toContain("所选模型没有开启图片附件");

    await click(container!.querySelector<HTMLButtonElement>('[data-testid="open-settings-btn"]')!);
    const label = Array.from(container!.querySelectorAll("label")).find(
      (item) => item.textContent?.includes("此模型支持 PNG 和 JPEG"),
    );
    expect(label).toBeDefined();
    await act(async () => label!.querySelector("input")!.click());
    const save = Array.from(container!.querySelectorAll("button")).find(
      (button) => button.textContent?.trim() === "保存模型设置",
    );
    await click(save!);
    expect(rpcCalls(host, "model.configure").at(-1)?.allow_image_attachments).toBe(true);
    await click(container!.querySelector<HTMLButtonElement>('[data-testid="close-settings-btn"]')!);
    expect(picker.accept).toContain("image/png");
    await act(async () => picker.dispatchEvent(new Event("change", { bubbles: true })));
    await inputValue(container!.querySelector('[data-testid="composer-textarea"]')!, "描述图片");
    await click(container!.querySelector('[data-testid="composer-send-btn"]')!);
    await waitFor(() => rpcCalls(host, "run.start").length > 0);
    expect(rpcCalls(host, "run.start").at(-1)?.attachments).toEqual([{
      name: "image.png", media_type: "image/png", data_base64: base64,
    }]);
  });

  it("保存 Responses 自动压缩阈值并用空值关闭", async () => {
    const host = new RedesignFakeHost();
    host.modelConfig = { ...host.modelConfig, mode: "openai-responses", model_name: "fixture" };
    mocked.host = host;
    await act(async () => root?.render(<App />));
    await click(container!.querySelector<HTMLButtonElement>('[data-testid="open-settings-btn"]')!);
    const field = Array.from(container!.querySelectorAll("label")).find(
      (label) => label.textContent?.includes("自动压缩阈值"),
    )?.querySelector("input");
    expect(field).toBeDefined();
    const save = Array.from(container!.querySelectorAll("button")).find(
      (button) => button.textContent?.trim() === "保存模型设置",
    );
    await inputValue(field!, "8192");
    await click(save!);
    expect(rpcCalls(host, "model.configure").at(-1)?.compact_threshold).toBe(8192);
    await inputValue(field!, "");
    await click(save!);
    expect(rpcCalls(host, "model.configure").at(-1)).not.toHaveProperty("compact_threshold");
  });

  it("追问引用所选的已完成运行，不修改任务目标", async () => {
    const task = makeTask({ active_run_id: null });
    const run = makeRun({ state: "succeeded", goal_revision: task.goal_revision });
    const host = new RedesignFakeHost(makeState([task], [run]));
    mocked.host = host;
    await act(async () => root?.render(<App />));
    await waitFor(() => container?.querySelector('[data-testid="followup-input"]') !== null);
    const picker = container!.querySelector<HTMLInputElement>('input[type="file"]');
    expect(picker).toBeInstanceOf(HTMLInputElement);
    const attachment = new File(["new info"], "details.txt", { type: "text/plain" });
    Object.defineProperty(picker, "files", { configurable: true, value: [attachment] });
    await act(async () => picker!.dispatchEvent(new Event("change", { bubbles: true })));
    const input = container!.querySelector<HTMLInputElement>('[data-testid="followup-input"]')!;
    await inputValue(input, "请扩写第二点");
    await click(container!.querySelector<HTMLButtonElement>('[data-testid="followup-send-btn"]')!);
    await waitFor(() => rpcCalls(host, "run.follow_up").length === 1);
    expect(rpcCalls(host, "run.follow_up")[0]).toMatchObject({
      task_id: task.task_id, source_run_id: run.run_id, message: "请扩写第二点",
      attachments: [{ name: "details.txt", media_type: "text/plain", data_base64: "bmV3IGluZm8=" }],
    });
    expect(rpcCalls(host, "task.revise")).toEqual([]);
  });

  it("权限菜单提供每次询问、自动审批、完全访问与仅读取与对话，未接入后端的档位诚实置灰标注", async () => {
    const host = new RedesignFakeHost(makeState());
    mocked.host = host;
    await act(async () => root?.render(<App />));
    await waitFor(() => container?.querySelector('[data-testid="composer-perm-btn"]') !== null);

    const permBtn = container!.querySelector<HTMLButtonElement>(
      '[data-testid="composer-perm-btn"]',
    )!;
    expect(permBtn.textContent).toContain("每次询问");
    await click(permBtn);

    const menu = container?.querySelector('[data-testid="perm-popover"]');
    expect(menu).not.toBeNull();
    expect(menu?.textContent).toContain("每次询问");
    expect(menu?.textContent).toContain("自动审批");
    expect(menu?.textContent).toContain("完全访问");
    expect(menu?.textContent).toContain("仅读取与对话");
    expect(menu?.textContent).toContain("待后端授权契约接入");

    const autoOpt = menu?.querySelector<HTMLButtonElement>('[data-perm-option="auto"]');
    const fullOpt = menu?.querySelector<HTMLButtonElement>('[data-perm-option="full"]');
    const pureOpt = menu?.querySelector<HTMLButtonElement>('[data-perm-option="pure_only"]');
    expect(autoOpt?.disabled).toBe(true);
    expect(fullOpt?.disabled).toBe(true);
    expect(pureOpt?.disabled).toBe(false);

    await click(pureOpt!);
    expect(
      container?.querySelector('[data-testid="composer-perm-btn"]')?.textContent,
    ).toContain("仅读取与对话");
  });

  it("会话流内联展示待审批卡片，且右侧运行洞察抽屉默认收起、按需展开", async () => {
    const host = new RedesignFakeHost(
      makeState([makeTask()], [makeRun()], [makeApproval()]),
    );
    mocked.host = host;
    await act(async () => root?.render(<App />));
    await waitFor(() => container?.querySelector(".stream-scroll") !== null);

    // 默认不展示右侧洞察抽屉
    expect(container?.querySelector('[data-testid="inspector-drawer"]')).toBeNull();

    // 审批卡片内联在对话流中，可直接点击「允许」
    const inlineApproval = container?.querySelector(".stream-scroll .approval-card");
    expect(inlineApproval).not.toBeNull();
    expect(inlineApproval?.textContent).toContain("覆盖已有认证实现");

    // 会话底栏同样在发送按钮正左侧提供模型状态按钮，且不含「回车发送」
    const chatModelBtn = container?.querySelector('[data-testid="followup-model-btn"]');
    const chatSendBtn = container?.querySelector('[data-testid="followup-send-btn"]');
    expect(chatModelBtn).not.toBeNull();
    expect(chatSendBtn).not.toBeNull();
    expect(
      chatModelBtn!.compareDocumentPosition(chatSendBtn!) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(container?.textContent).not.toContain("回车发送");

    // 点击「运行洞察」展开右侧抽屉
    const inspectorToggle = container?.querySelector<HTMLButtonElement>(
      '[data-testid="toggle-inspector-btn"]',
    );
    expect(inspectorToggle).not.toBeNull();
    await click(inspectorToggle!);
    expect(container?.querySelector('[data-testid="inspector-drawer"]')).not.toBeNull();
    expect(container?.querySelector('[data-testid="inspector-drawer"]')?.textContent).toContain(
      "累计成本",
    );
  });

  it("设置中心包含完整 8 大分栏，且浅深主题、动态伴侣显示与眼神跟随开关实时生效", async () => {
    const host = new RedesignFakeHost(makeState());
    mocked.host = host;
    await act(async () => root?.render(<App />));
    await waitFor(() => container?.querySelector('[data-testid="open-settings-btn"]') !== null);

    expect(container?.querySelector(".app-shell")?.getAttribute("data-theme")).toBe("light");

    await click(container!.querySelector<HTMLButtonElement>('[data-testid="open-settings-btn"]')!);
    const dialog = container?.querySelector('[role="dialog"][aria-label="系统设置"]');
    expect(dialog).not.toBeNull();

    for (const tabLabel of [
      "模型与密钥",
      "安全与执行授权",
      "定时任务",
      "预算账本",
      "MCP 服务",
      "记忆与上下文",
      "诊断与数据服务",
      "外观与伴侣动效",
    ]) {
      expect(dialog?.textContent).toContain(tabLabel);
    }

    const appearanceTab = dialog?.querySelector<HTMLButtonElement>(
      '[data-settings-tab="appearance"]',
    );
    expect(appearanceTab).not.toBeNull();
    await click(appearanceTab!);

    // 切换深色模式实时同步到 app-shell
    const darkBtn = dialog?.querySelector<HTMLButtonElement>('[data-theme-choice="dark"]');
    expect(darkBtn).not.toBeNull();
    await click(darkBtn!);
    expect(container?.querySelector(".app-shell")?.getAttribute("data-theme")).toBe("dark");

    // 关闭眼神跟随视差
    const parallaxSwitch = dialog?.querySelector<HTMLButtonElement>(
      '[data-testid="switch-pet-parallax"]',
    );
    expect(parallaxSwitch).not.toBeNull();
    await click(parallaxSwitch!);

    const closeBtn = dialog?.querySelector<HTMLButtonElement>(
      '[data-testid="close-settings-btn"]',
    );
    await click(closeBtn!);
    expect(
      container
        ?.querySelector('[data-testid="welcome-pet"]')
        ?.getAttribute("data-pet-parallax"),
    ).toBe("false");

    // 再次打开设置，关闭欢迎页动态伴侣
    await click(container!.querySelector<HTMLButtonElement>('[data-testid="open-settings-btn"]')!);
    await click(
      container!.querySelector<HTMLButtonElement>('[data-settings-tab="appearance"]')!,
    );
    await click(
      container!.querySelector<HTMLButtonElement>('[data-testid="switch-enable-pet"]')!,
    );
    await click(
      container!.querySelector<HTMLButtonElement>('[data-testid="close-settings-btn"]')!,
    );
    expect(container?.querySelector('.welcome-view [data-testid="welcome-pet"]')).toBeNull();
  });

  it("发送按钮左侧模型选择器调用真实 model.configure 热切换，会话流支持 W4 实时流式预览", async () => {
    const host = new RedesignFakeHost(makeState([makeTask()], [makeRun({ state: "running" })]));
    mocked.host = host;
    await act(async () => root?.render(<App />));
    await waitFor(() => container?.querySelector('[data-testid="followup-model-btn"]') !== null);

    const modelBtn = container!.querySelector<HTMLButtonElement>(
      '[data-testid="followup-model-btn"]',
    )!;
    expect(modelBtn.textContent).toContain("演示脚本模型");
    await click(modelBtn);

    const openaiBtn = container?.querySelector<HTMLButtonElement>('[data-model-mode="openai"]');
    expect(openaiBtn).not.toBeNull();
    await click(openaiBtn!);

    const modelInput = container?.querySelector<HTMLInputElement>(
      '[data-testid="followup-model-btn-model-input"]',
    );
    expect(modelInput).not.toBeNull();
    await inputValue(modelInput!, "gpt-4o");

    await click(
      container!.querySelector<HTMLButtonElement>('[data-testid="quick-model-apply-btn"]')!,
    );
    await waitFor(() => rpcCalls(host, "model.configure").length === 1);
    expect(rpcCalls(host, "model.configure")[0]).toEqual({
      mode: "openai",
      model_name: "gpt-4o",
      base_url: "",
      credential_backend: "env",
    });
    expect(
      container?.querySelector('[data-testid="followup-model-btn"]')?.textContent,
    ).toContain("gpt-4o");

    // W4 实时流式增量进入会话时间线，收到 run.end 后自动收口
    expect(host.streamCallbacks).not.toBeNull();
    await act(async () => {
      host.streamCallbacks?.onModelStream?.({
        kind: "model_stream",
        run_id: "run_alpha",
        task_id: "task_alpha",
        turn_index: 1,
        delta: {
          schema_version: "1.0",
          kind: "text",
          text: "正在流式分析认证模块…",
          tool_name: "",
        },
        done: false,
      });
    });
    expect(container?.querySelector(".streaming-item")?.textContent).toContain(
      "正在流式分析认证模块…",
    );

    await act(async () => {
      host.streamCallbacks?.onModelStream?.({
        kind: "model_stream",
        run_id: "run_alpha",
        task_id: "task_alpha",
        turn_index: 1,
        delta: null,
        done: true,
      });
    });
    expect(container?.querySelector(".streaming-item")).toBeNull();
  });
});
