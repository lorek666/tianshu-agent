// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import type { Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { WelcomeComposer } from "../components/WelcomeComposer";
import { TaskWorkspaceView } from "../components/TaskWorkspaceView";
import { summarizeCost } from "../lib/timeline";
import type { PreparedSkill, SkillCatalog, SkillsApi } from "../api/skills";
import type { HostClient } from "../api/host";
import { DemoHostClient } from "../demo/host";
import type { RunView, TaskView } from "../api/types";

Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
const skill = {
  id: "review@0123456789ab", name: "review", description: "检查修改",
  path: "/workspace/.agents/skills/review/SKILL.md", sha256: "a".repeat(64),
};
const task: TaskView = {
  task_id: "task_1", control: false, goal: "旧目标", goal_revision: 1,
  status: "created", active_run_id: null, workspaces: [{ root_path: "/workspace", mode: "readonly" }],
  created_at: 1, updated_at: 1,
};
const source: RunView = {
  run_id: "run_1", task_id: task.task_id, state: "succeeded", outcome: "noop",
  effect_state: "none_applied", control_requested: null, goal_revision: 1, lease_epoch: 1,
  started_at: 1, ended_at: 2, n_applied: 0, n_unknown: 0, undoable: false,
};
let container: HTMLDivElement;
let root: Root;
let api: SkillsApi;
let submit: ReturnType<typeof vi.fn>;

function welcome() {
  return <WelcomeComposer
    activeProject={{ name: "项目", root_path: "/workspace", mode: "readonly" }}
    projects={[]} defaultPolicy={{ format_version: 1, mode: "pure_only" }}
    enablePet={false} enableParallax={false} reduceMotion previewPose="idle"
    connectionLost={false} streamStatus="live" streamDetail={null} openApprovalsCount={0}
    busy={false} onJumpToPendingApproval={vi.fn()} onRefresh={vi.fn()}
    onOpenModelSettings={vi.fn()} onSubmit={submit} skillsApi={api}
  />;
}

function followup(run: RunView = source) {
  return <TaskWorkspaceView
    task={task} taskRuns={[run]} allRuns={[run]} approvals={[]}
    selectedRun={run} selectedRunId={run.run_id}
    entries={[]} events={[]} hasMore={false} cost={summarizeCost([])} streamStatus="live"
    connectionLost={false} eventSeqHead={0} busy={null} now={1}
    executionPolicy={{ format_version: 1, mode: "pure_only" }} defaultMode="plan"
    enablePet={false} reduceMotion onSelectRun={vi.fn()} onExecutionPolicyChange={vi.fn()}
    onStartRun={vi.fn()} onRevise={vi.fn()} onFollowUpSubmit={submit} onPause={vi.fn()}
    onResume={vi.fn()} onRequestCancel={vi.fn()} onUndo={vi.fn()} onDecideApproval={vi.fn()}
    onJumpToTask={vi.fn()} onOpenModelSettings={vi.fn()} onRefresh={vi.fn()} skillsApi={api}
  />;
}

function input(): HTMLInputElement | HTMLTextAreaElement {
  return container.querySelector("[data-testid='composer-textarea'],[data-testid='followup-input']")!;
}

async function type(value: string) {
  await act(async () => {
    const element = input();
    const prototype = element instanceof HTMLTextAreaElement
      ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(prototype, "value")!.set!.call(element, value);
    element.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

async function send() {
  await act(async () => {
    input().closest("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  });
}

beforeEach(() => {
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  submit = vi.fn(async () => undefined);
  api = {
    list: vi.fn(async () => ({ format_version: 1 as const, skills: [skill], issues: [] })),
    prepare: vi.fn(async () => ({
      format_version: 1 as const, skill, goal: "已冻结的技能全文和用户要求",
    })),
  };
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
});

describe.each([["新建", welcome], ["追问", followup]] as const)("%s技能调用", (_, view) => {
  it("卸载后忽略迟到的准备结果", async () => {
    let finish!: (value: Awaited<ReturnType<SkillsApi["prepare"]>>) => void;
    api.prepare = vi.fn(() => new Promise<PreparedSkill>((resolve) => { finish = resolve; }));
    await act(async () => root.render(view()));
    await type("/review 检查");
    await send();
    expect(api.prepare).toHaveBeenCalledTimes(1);
    await act(async () => root.render(null));
    await act(async () => finish({ format_version: 1, skill, goal: "旧快照" }));
    expect(submit).not.toHaveBeenCalled();
  });

  it("输入法确认键 keyCode229 不准备或提交技能", async () => {
    await act(async () => root.render(view()));
    await type("/review");
    await act(async () => input().dispatchEvent(new KeyboardEvent(
      "keydown", { key: "Enter", keyCode: 229, bubbles: true, cancelable: true },
    )));
    expect(input().value).toBe("/review");
    expect(api.prepare).not.toHaveBeenCalled();
    expect(submit).not.toHaveBeenCalled();
  });

  it("准备成功后才提交完整快照，并保留运行策略", async () => {
    await act(async () => root.render(view()));
    await type("/review 检查改动");
    await send();
    expect(api.prepare).toHaveBeenCalledWith("review", "检查改动", ["/workspace"]);
    expect(submit).toHaveBeenCalledTimes(1);
    const args = submit.mock.calls[0]!;
    if (view === welcome) {
      expect(args[0]).toMatchObject({
        goal: "已冻结的技能全文和用户要求",
        execution_policy: { format_version: 1, mode: "pure_only" },
      });
    } else {
      expect(args.slice(1)).toEqual([
        source, "已冻结的技能全文和用户要求",
        { format_version: 1, mode: "pure_only" }, {}, [], "plan",
      ]);
    }
  });

  it("未知或过期技能准备失败时保留草稿，不提交目标", async () => {
    api.prepare = vi.fn(async () => { throw new Error("所选技能已变化，请重新选择"); });
    await act(async () => root.render(view()));
    await type("/skill review@old 检查");
    await send();
    expect(submit).not.toHaveBeenCalled();
    expect(input().value).toBe("/skill review@old 检查");
    expect(container.querySelector("[role='alert']")?.textContent).toContain("已变化");
  });

  it("菜单选择只插入版本标识，再次发送才准备技能", async () => {
    await act(async () => root.render(view()));
    await type("/");
    const option = container.querySelector<HTMLButtonElement>("[data-skill-id]")!;
    expect(option?.textContent).toContain("检查修改");
    await act(async () => option.click());
    expect(input().value).toBe("/skill review@0123456789ab ");
    expect(submit).not.toHaveBeenCalled();
    expect(api.prepare).not.toHaveBeenCalled();
    await send();
    expect(api.prepare).toHaveBeenCalledWith("review@0123456789ab", "", ["/workspace"]);
  });
});

it("取消请求保留明确的取消中状态、恢复说明和终态切换", async () => {
  const cancelling: RunView = {
    ...source, state: "running", outcome: null, ended_at: null,
    control_requested: "cancel",
  };
  await act(async () => root.render(followup(cancelling)));
  const heading = container.querySelector(".canvas-task-heading")!;
  expect(heading.textContent).toContain("正在取消");
  expect(heading.textContent).not.toContain("运行中");
  expect(heading.textContent).toContain("恢复提示");
  const pending = [...container.querySelectorAll<HTMLButtonElement>("button")].find(
    (button) => button.textContent?.includes("取消中…"),
  );
  expect(pending).toBeDefined();
  expect(pending!.disabled).toBe(true);

  await act(async () => root.render(followup({
    ...cancelling, state: "cancelled", outcome: "cancelled", ended_at: 2,
    control_requested: null,
  })));
  expect(heading.textContent).toContain("已取消");
  expect([...container.querySelectorAll<HTMLButtonElement>("button")].some(
    (button) => button.textContent?.includes("取消中…"),
  )).toBe(false);
});

it("任务修订后忽略旧准备结果，仍可提交新修订的技能", async () => {
  let finish!: (value: Awaited<ReturnType<SkillsApi["prepare"]>>) => void;
  api.prepare = vi.fn(() => new Promise<PreparedSkill>((resolve) => { finish = resolve; }));
  await act(async () => root.render(followup()));
  await type("/review 检查");
  await send();
  const revised = { ...task, goal_revision: 2, goal: "新目标" };
  const revisedSource = { ...source, run_id: "run_2", goal_revision: 2 };
  await act(async () => root.render(<TaskWorkspaceView {...followup().props} task={revised}
    taskRuns={[revisedSource]} allRuns={[revisedSource]}
    selectedRun={revisedSource} selectedRunId={revisedSource.run_id} />));
  await act(async () => finish({ format_version: 1, skill, goal: "旧快照" }));
  expect(submit).not.toHaveBeenCalled();
  api.prepare = vi.fn(async () => ({ format_version: 1 as const, skill, goal: "新快照" }));
  await type("/review 新检查");
  await send();
  expect(submit).toHaveBeenCalledWith(
    revised, revisedSource, "新快照", { format_version: 1, mode: "pure_only" }, {}, [], "plan",
  );
});

it("新建时切换工作区后忽略旧准备结果并恢复输入", async () => {
  let finish!: (value: Awaited<ReturnType<SkillsApi["prepare"]>>) => void;
  api.prepare = vi.fn(() => new Promise<PreparedSkill>((resolve) => { finish = resolve; }));
  await act(async () => root.render(welcome()));
  await type("/review 检查");
  await send();
  await act(async () => root.render(<WelcomeComposer {...welcome().props}
    activeProject={{ name: "新项目", root_path: "/new-workspace", mode: "readonly" }} />));
  await act(async () => finish({ format_version: 1, skill, goal: "旧快照" }));
  expect(submit).not.toHaveBeenCalled();
  expect(input().disabled).toBe(false);
  expect(input().value).toBe("/review 检查");
});

it("菜单键盘选择与输入法组合输入不会误发送，Escape 可关闭", async () => {
  await act(async () => root.render(welcome()));
  await type("/review");
  await act(async () => input().dispatchEvent(new KeyboardEvent(
    "keydown", { key: "Enter", isComposing: true, bubbles: true, cancelable: true },
  )));
  expect(input().value).toBe("/review");
  expect(submit).not.toHaveBeenCalled();
  await act(async () => input().dispatchEvent(new KeyboardEvent(
    "keydown", { key: "Tab", bubbles: true, cancelable: true },
  )));
  expect(input().value).toBe("/skill review@0123456789ab ");
  await type("/");
  await act(async () => input().dispatchEvent(new KeyboardEvent(
    "keydown", { key: "Escape", bubbles: true, cancelable: true },
  )));
  expect(container.querySelector("[role='listbox']")).toBeNull();
});

it("列表读取失败显示原因，仍可使用内置计划命令", async () => {
  api.list = vi.fn(async () => { throw new Error("目录不可读"); });
  await act(async () => root.render(welcome()));
  await type("/");
  expect(container.textContent).toContain("目录不可读");
  await type("/plan 先分析");
  await send();
  expect(submit.mock.calls[0]?.[0]).toMatchObject({ goal: "先分析", mode: "plan" });
});

it("关闭菜单后再使用 /skills 能重新打开", async () => {
  await act(async () => root.render(welcome()));
  await type("/");
  await act(async () => input().dispatchEvent(new KeyboardEvent(
    "keydown", { key: "Escape", bubbles: true, cancelable: true },
  )));
  expect(container.querySelector("[role='listbox']")).toBeNull();
  await type("/skills");
  await send();
  expect(container.querySelector("[role='listbox']")).not.toBeNull();
  expect(submit).not.toHaveBeenCalled();
});

it("列表按描述过滤，方向键选中技能，Shift+Enter 不选择", async () => {
  await act(async () => root.render(welcome()));
  await type("/检查");
  expect(container.querySelector("[data-skill-id]")?.textContent).toContain("review");
  await act(async () => input().dispatchEvent(new KeyboardEvent(
    "keydown", { key: "Enter", shiftKey: true, bubbles: true, cancelable: true },
  )));
  expect(input().value).toBe("/检查");
  await type("/");
  // 四个内置命令（Goal/计划/压缩/执行）之后才是本地技能。
  for (let i = 0; i < 4; i += 1) {
    await act(async () => input().dispatchEvent(new KeyboardEvent(
      "keydown", { key: "ArrowDown", bubbles: true, cancelable: true },
    )));
  }
  await act(async () => input().dispatchEvent(new KeyboardEvent(
    "keydown", { key: "Enter", bubbles: true, cancelable: true },
  )));
  expect(input().value).toBe("/skill review@0123456789ab ");
  expect(submit).not.toHaveBeenCalled();
});

it("工作区切换后忽略旧列表响应", async () => {
  let finishOld!: (catalog: SkillCatalog) => void;
  api.list = vi.fn((roots: string[]) => roots[0] === "/workspace"
    ? new Promise<SkillCatalog>((resolve) => { finishOld = resolve; })
    : Promise.resolve({
      format_version: 1 as const, skills: [{ ...skill, name: "new-workspace" }], issues: [],
    }));
  await act(async () => root.render(welcome()));
  await type("/");
  await act(async () => root.render(<WelcomeComposer {...welcome().props}
    activeProject={{ name: "新项目", root_path: "/new-workspace", mode: "readonly" }} />));
  expect(container.querySelector("[data-skill-id]")?.textContent).toContain("new-workspace");
  await act(async () => finishOld({ format_version: 1, skills: [skill], issues: [] }));
  expect(container.querySelector("[data-skill-id]")?.textContent).toContain("new-workspace");
});

it("客户端拒绝未知版本，真实演示客户端明确返回空技能", async () => {
  const { createSkillsApi } = await import("../api/skills");
  const rpc = vi.fn(async () => ({ format_version: 2, skills: [], issues: [] }));
  const client = createSkillsApi({ rpc } as unknown as HostClient);
  await expect(client.list(["/workspace"])).rejects.toThrow("V1");
  expect(rpc).toHaveBeenCalledWith("skill.list", { workspace_roots: ["/workspace"] });
  const demo = createSkillsApi(new DemoHostClient());
  const catalog = await demo.list([]);
  expect(catalog.skills).toEqual([]);
  expect(catalog.issues[0]?.message).toContain("演示");
  await expect(demo.prepare("review", "", [])).rejects.toThrow("演示");
});
