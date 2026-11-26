// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import type { Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { parseComposerCommand } from "../lib/commands";
import { WelcomeComposer } from "../components/WelcomeComposer";
import { GoalPanel } from "../components/GoalPanel";
import type { GoalsApi, GoalView } from "../api/goals";
import type { RunView, TaskView } from "../api/types";

Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
let root: Root;
let container: HTMLDivElement;
const task: TaskView = {
  task_id: "task_1", control: false, goal: "整理资料", goal_revision: 1,
  status: "open", active_run_id: null, workspaces: [], created_at: 1, updated_at: 1,
};
const policy = { format_version: 1, mode: "pure_only" } as const;
const ready = {
  format_version: 1, goal_id: "goal_1", task_id: "task_1", goal_revision: 1,
  criteria: ["中文报告", "包含来源"], max_rounds: 2, execution_policy: policy,
  rounds_started: 0, latest_run_id: null, status: "ready", confirmed_criteria: [],
} as const;

beforeEach(() => {
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});
afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
});

async function type(selector: string, value: string) {
  await act(async () => {
    const element = container.querySelector(selector)!;
    const prototype = element instanceof HTMLTextAreaElement
      ? HTMLTextAreaElement.prototype : element instanceof HTMLSelectElement
        ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(prototype, "value")!.set!.call(element, value);
    element.dispatchEvent(new Event(element instanceof HTMLSelectElement ? "change" : "input", {
      bubbles: true,
    }));
  });
}
async function click(selector: string) {
  await act(async () => (container.querySelector(selector)! as HTMLButtonElement).click());
}

it("/goal 优先于同名技能，显式 /skill goal 仍可调用技能", () => {
  expect(parseComposerCommand("/goal")).toEqual({ kind: "goal", text: "" });
  expect(parseComposerCommand("/goal 整理资料")).toEqual({ kind: "goal", text: "整理资料" });
  expect(parseComposerCommand("/skill goal 资料")).toEqual({
    kind: "skill", skill: "goal", text: "资料",
  });
});

it("新建目标先填写条件，不把 /goal 当普通运行发送", async () => {
  const submit = vi.fn(async () => undefined);
  await act(async () => root.render(<WelcomeComposer
    activeProject={null} projects={[]} defaultPolicy={policy}
    enablePet={false} enableParallax={false} reduceMotion previewPose="idle"
    connectionLost={false} streamStatus="live" streamDetail={null} openApprovalsCount={0}
    busy={false} onJumpToPendingApproval={vi.fn()} onRefresh={vi.fn()}
    onOpenModelSettings={vi.fn()} onSubmit={submit}
  />));
  await type("[data-testid='composer-textarea']", "/goal 整理资料");
  await click("[data-testid='composer-send-btn']");
  expect(submit).not.toHaveBeenCalled();
  expect(container.textContent).toContain("每轮手动继续");
  await type("[data-testid='goal-criteria']", "中文报告\n包含来源");
  await click("[data-testid='composer-send-btn']");
  expect(submit).toHaveBeenCalledWith(expect.objectContaining({
    goal: "整理资料", goal_config: { criteria: ["中文报告", "包含来源"], max_rounds: 3 },
    execution_policy: policy,
  }));
});

it("目标配置中显式输入 /plan 正文后只创建计划，不携带 Goal 配置", async () => {
  const submit = vi.fn(async () => undefined);
  await act(async () => root.render(<WelcomeComposer
    activeProject={null} projects={[]} defaultPolicy={policy}
    enablePet={false} enableParallax={false} reduceMotion previewPose="idle"
    connectionLost={false} streamStatus="live" streamDetail={null} openApprovalsCount={0}
    busy={false} onJumpToPendingApproval={vi.fn()} onRefresh={vi.fn()}
    onOpenModelSettings={vi.fn()} onSubmit={submit}
  />));
  await type("[data-testid='composer-textarea']", "/goal");
  await click("[data-testid='composer-send-btn']");
  await type("[data-testid='goal-criteria']", "先前目标条件");
  await type("[data-testid='composer-textarea']", "/plan 调查项目");
  await click("[data-testid='composer-send-btn']");
  expect(submit).toHaveBeenCalledWith({
    goal: "调查项目", mode: "plan", workspaces: [], execution_policy: policy,
  });
});

it.each(["attachment", "profile"])("Goal 不会静默丢弃尚未支持的 %s", async (kind) => {
  const submit = vi.fn(async () => undefined);
  await act(async () => root.render(<WelcomeComposer
    activeProject={null} projects={[]} defaultPolicy={policy}
    enablePet={false} enableParallax={false} reduceMotion previewPose="idle"
    connectionLost={false} streamStatus="live" streamDetail={null} openApprovalsCount={0}
    busy={false} onJumpToPendingApproval={vi.fn()} onRefresh={vi.fn()}
    profiles={{ local: { mode: "scripted", model_name: "", base_url: "", credential_backend: "env" } }}
    onOpenModelSettings={vi.fn()} onSubmit={submit}
  />));
  await type("[data-testid='composer-textarea']", "/goal 整理资料");
  await click("[data-testid='composer-send-btn']");
  await type("[data-testid='goal-criteria']", "中文报告");
  if (kind === "profile") {
    await type(".composer-model-route select", "local");
  } else {
    await act(async () => {
      const input = container.querySelector("input[type=file]")!;
      Object.defineProperty(input, "files", { value: [new File(["资料"], "source.txt")] });
      input.dispatchEvent(new Event("change", { bubbles: true }));
    });
  }
  await click("[data-testid='composer-send-btn']");
  expect(submit).not.toHaveBeenCalled();
  expect(container.textContent).toContain("目标模式首版暂不支持附件和单次模型档案");
  expect((container.querySelector("[data-testid='goal-criteria']") as HTMLTextAreaElement).value)
    .toBe("中文报告");
});

function api(goal: GoalView | null = structuredClone(ready) as unknown as GoalView): GoalsApi {
  return {
    get: vi.fn(async () => goal),
    create: vi.fn(async () => structuredClone(ready) as unknown as GoalView),
    advance: vi.fn(async () => { throw new Error("not used"); }),
    close: vi.fn(async () => {
      goal = { ...goal!, status: "completed", confirmed_criteria: [0, 1] };
      return goal;
    }),
  };
}

function panel(goalsApi: GoalsApi, selectedTask = task, head = 1) {
  return <GoalPanel key={`${selectedTask.task_id}:${selectedTask.goal_revision}`}
    api={goalsApi} task={selectedTask} eventSeqHead={head} executionPolicy={policy}
    onRunStarted={vi.fn()} onChanged={vi.fn()} onSelectRun={vi.fn()}
  />;
}

it("逐项确认前不能完成目标，完成不会创建普通 Run", async () => {
  const value = {
    ...structuredClone(ready), status: "awaiting_review", rounds_started: 1,
    latest_run_id: "run_1",
  } as unknown as GoalView;
  const goalsApi = api(value);
  await act(async () => root.render(panel(goalsApi)));
  const complete = () => container.querySelector("[data-testid='goal-complete']") as HTMLButtonElement;
  expect(complete().disabled).toBe(true);
  await click("[data-testid='goal-check-0']");
  expect(complete().disabled).toBe(true);
  await click("[data-testid='goal-check-1']");
  expect(complete().disabled).toBe(false);
  await click("[data-testid='goal-complete']");
  expect(goalsApi.close).toHaveBeenCalledWith(
    "goal_1", "completed", [0, 1], expect.any(String),
  );
  expect(goalsApi.advance).not.toHaveBeenCalled();
});

it("推进错误保留目标，重试复用同一 request_id", async () => {
  const goalsApi = api();
  goalsApi.advance = vi.fn(async () => { throw new Error("连接中断"); });
  await act(async () => root.render(panel(goalsApi)));
  await click("[data-testid='goal-advance']");
  expect(container.textContent).toContain("连接中断");
  expect(container.textContent).toContain("中文报告");
  await click("[data-testid='goal-advance']");
  const calls = vi.mocked(goalsApi.advance).mock.calls;
  expect(calls).toHaveLength(2);
  expect(calls[0]).toEqual(calls[1]);
});

it("切换 Task 后忽略旧读取的迟到结果", async () => {
  let finish!: (goal: GoalView | null) => void;
  const goalsApi = api();
  goalsApi.get = vi.fn(() => new Promise<GoalView | null>((resolve) => { finish = resolve; }));
  await act(async () => root.render(panel(goalsApi)));
  const old = finish;
  await act(async () => root.render(panel(goalsApi, { ...task, task_id: "task_2" })));
  await act(async () => old(structuredClone(ready) as unknown as GoalView));
  expect(container.textContent).not.toContain("中文报告");
});

it("阻断目标不能继续或完成，已用轮次仍显示", async () => {
  const goalsApi = api({
    ...structuredClone(ready), status: "blocked", rounds_started: 1, latest_run_id: "run_1",
  } as unknown as GoalView);
  await act(async () => root.render(panel(goalsApi)));
  expect((container.querySelector("[data-testid='goal-advance']") as HTMLButtonElement).disabled)
    .toBe(true);
  expect((container.querySelector("[data-testid='goal-complete']") as HTMLButtonElement).disabled)
    .toBe(true);
  expect(container.textContent).toContain("1 / 2");
});

const queued: RunView = {
  run_id: "run_1", task_id: "task_1", state: "created", outcome: null,
  effect_state: "none_applied", control_requested: null, goal_revision: 1,
  lease_epoch: 1, started_at: 1, ended_at: null, n_applied: 0, n_unknown: 0, undoable: false,
};

it("快速完成的运行在推进后重新核对状态，不永久显示进行中", async () => {
  const goalsApi = api();
  const running = {
    ...structuredClone(ready), status: "running", rounds_started: 1, latest_run_id: "run_1",
  } as unknown as GoalView;
  goalsApi.get = vi.fn().mockResolvedValueOnce(ready)
    .mockResolvedValue({ ...running, status: "awaiting_review" });
  goalsApi.advance = vi.fn(async () => ({ goal: running, run: queued }));
  await act(async () => root.render(panel(goalsApi)));
  await click("[data-testid='goal-advance']");
  expect(container.textContent).toContain("等待验收或继续");
});

it("更换连接后忽略旧连接迟到的推进响应", async () => {
  const first = api();
  let finish!: (value: { goal: GoalView; run: RunView }) => void;
  first.advance = vi.fn(() => new Promise<{ goal: GoalView; run: RunView }>((resolve) => {
    finish = resolve;
  }));
  await act(async () => root.render(panel(first)));
  await click("[data-testid='goal-advance']");
  const next = api({
    ...structuredClone(ready), goal_id: "new_goal", criteria: ["新连接的条件"],
  } as unknown as GoalView);
  await act(async () => root.render(panel(next)));
  await act(async () => finish({
    goal: { ...structuredClone(ready), status: "running" } as unknown as GoalView,
    run: queued,
  }));
  expect(container.textContent).toContain("新连接的条件");
  expect(container.textContent).not.toContain("中文报告");
});
