// @vitest-environment jsdom

import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import type { EventView, RunView } from "../../api/types";
import { BudgetPanel } from "../BudgetPanel";
import { RunTimeline } from "../RunTimeline";
import { SubtaskPanel } from "../SubtaskPanel";

function event(
  type: string,
  payload: Record<string, unknown>,
  seq: number,
): EventView {
  return {
    seq,
    type,
    run_id: "run_parent",
    task_id: "task_parent",
    ts: seq * 1000,
    actor_kind: "core",
    payload,
  };
}

function run(runId: string, state: string, taskId: string = "task_parent"): RunView {
  return {
    run_id: runId,
    /* 子 Run 的 task_id 是子 Task id（tasks.py 的 _insert_run_locked 用子任务
       建_run）——夹具如实造形（复审 P1：旧夹具伪造父 task_id 掩盖了 App 层
       传父任务过滤 runs 导致子运行回填不可达的缺陷）。 */
    task_id: taskId,
    state,
    outcome: null,
    effect_state: "none_applied",
    control_requested: null,
    goal_revision: 1,
    lease_epoch: 1,
    started_at: 1,
    ended_at: null,
    n_applied: 0,
    n_unknown: 0,
    undoable: false,
  };
}

const SPAWNED = {
  subtask_id: "st_read_1",
  child_task_id: "task_child_1",
  child_run_id: "run_child_1",
  job_id: "job_child_1",
  goal_digest: "abcdef1234567890",
  limits: { max_turns: 2, max_tokens: 3_000, deadline_ms: 300_000 },
};

describe("预算面板渲染", () => {
  it("限额、用量、费用与未知口径一起出现（J5 / J6）", () => {
    const html = renderToStaticMarkup(
      <BudgetPanel
        events={[
          event("run.start", { limits: { max_turns: 8, max_total_tokens: 12_000 } }, 1),
          event("model.result", { cost: 0.005, usage: { input_tokens: 540, output_tokens: 122 } }, 2),
          event("model.result", { cost: null, usage: null }, 3),
          event("model.error", { error: "预算预占被拒" }, 4),
        ]}
      />,
    );
    expect(html).toContain("预算与用量");
    expect(html).toContain("轮次上限");
    expect(html).toContain("12,000");
    expect(html).toContain("用量未知不当作 0");
    expect(html).toContain("已知费用 $0.01"); // 0.005 美元 = 5000 微美元 → $0.01 以下取整见下
    expect(html).toContain("1 次调用没有用量记录");
  });

  it("不足半美分的已知费用如实显示并保留精确值入口", () => {
    const html = renderToStaticMarkup(
      <BudgetPanel
        events={[
          event("model.result", { cost: 0.001, usage: { input_tokens: 1, output_tokens: 1 } }, 1),
        ]}
      />,
    );
    expect(html).toContain("$0.00"); // 四舍五入到美分后的主展示
    expect(html).toContain("精确"); // formatUsdExact 出现，不把 sub-cent 藏成"没花钱"
  });

  it("C4 拒绝编号渲染成人话，原始编号只在 title/详情（J7）", () => {
    const html = renderToStaticMarkup(
      <BudgetPanel
        events={[
          event("policy.denied", { rule_id: "dispatch.budget_refused", reason: "额度耗尽" }, 1),
          event("dispatch.rejected", { rule_id: "dispatch.subtask_limit", reason: "累计 4 个" }, 2),
        ]}
      />,
    );
    expect(html).toContain("预算额度不足，请求没有发出");
    expect(html).toContain("委派限额不满足");
    expect(html).toContain("额度耗尽");
    expect(html).toContain("累计 4 个");
    // 主界面不出现机器枚举原文（D1 决策）；编号映射的键不在渲染结果里。
    expect(html).not.toContain("dispatch.budget_refused");
    expect(html).not.toContain("dispatch.subtask_limit");
  });

  it("没有预算事件的 Run 整个面板不渲染（J8）", () => {
    const html = renderToStaticMarkup(
      <BudgetPanel events={[event("receipt", { receipt_id: "rcpt_x" }, 1)]} />,
    );
    expect(html).toBe("");
  });

  it("显式 usage {0,0} 是「有记录」：显示已知 0 而非无记录（复审 P3）", () => {
    const html = renderToStaticMarkup(
      <BudgetPanel
        events={[
          event("model.result", { cost: 0, usage: { input_tokens: 0, output_tokens: 0 } }, 1),
        ]}
      />,
    );
    // 精确钉住 Token 格的渲染（「用量未知」是常驻副标题子串，不能整页 not.toContain）。
    expect(html).toContain(
      '<span class="summary-label">Token</span><strong class="num">0</strong>',
    );
    expect(html).not.toContain("无记录");
  });
});

describe("子任务面板渲染", () => {
  it("进行中的子任务显示进行中与限额，不显示 usage 0（J1 / J2）", () => {
    const html = renderToStaticMarkup(
      <SubtaskPanel
        events={[event("subtask.spawned", SPAWNED, 1)]}
        runs={[run("run_child_1", "running", "task_child_1")]}
      />,
    );
    expect(html).toContain("子任务");
    expect(html).toContain("进行中");
    expect(html).toContain("子运行：运行中");
    expect(html).toContain("轮次 2");
    expect(html).toContain("300s");
    expect(html).not.toContain("已完成");
  });

  it.each([
    ["paused", "已暂停"],
    ["waiting_approval", "等待你的批准"],
    ["reconciling", "正在核对结果"],
  ])("子运行在 %s 时不显示进行中或委派已完成", (state, label) => {
    const html = renderToStaticMarkup(
      <SubtaskPanel
        events={[event("subtask.spawned", SPAWNED, 1)]}
        runs={[run("run_child_1", state, "task_child_1")]}
      />,
    );
    expect(html).toContain(label);
    expect(html).not.toContain("进行中");
    expect(html).not.toContain("已完成");
  });

  it("找不到子运行时不猜测运行状态", () => {
    const html = renderToStaticMarkup(
      <SubtaskPanel
        events={[event("subtask.spawned", SPAWNED, 1)]}
        runs={[run("unrelated_run", "running", "unrelated_task")]}
      />,
    );
    expect(html).toContain("子运行状态暂不可用");
    expect(html).not.toContain("进行中");
  });

  it("收尾的子任务显示终态与用量；error 文本被转义（J10）", () => {
    const payload = '<img src=x onerror="alert(1)"><script>alert(2)</script>';
    const html = renderToStaticMarkup(
      <SubtaskPanel
        events={[
          event("subtask.spawned", SPAWNED, 1),
          event(
            "subtask.finished",
            { subtask_id: "st_read_1", state: "failed", error: payload, usage: { turns: 1, tokens: 42 } },
            2,
          ),
        ]}
        runs={[run("run_child_1", "failed", "task_child_1")]}
      />,
    );
    expect(html).toContain("失败");
    expect(html).toContain("轮次 1 · token 42");
    expect(html).not.toContain("<img");
    expect(html).not.toContain("<script>");
    expect(html).toContain("&lt;img");
  });

  it("没有委派事件的 Run 整个面板不渲染（J8）", () => {
    const html = renderToStaticMarkup(
      <SubtaskPanel events={[event("model.result", {}, 1)]} runs={[]} />,
    );
    expect(html).toBe("");
  });
});

describe("RunTimeline W4 流式预览", () => {
  it("展示正在输出的流式增量文本与工具名，并转义 HTML", () => {
    const html = renderToStaticMarkup(
      <RunTimeline
        entries={[]}
        hasMore={false}
        live={true}
        streamingPreview={{
          turnIndex: 2,
          text: "实时内容 <script>evil()</script>",
          toolNames: ["read_text"],
        }}
      />,
    );
    expect(html).toContain("第 2 轮");
    expect(html).toContain("准备调用工具：read_text");
    expect(html).toContain("&lt;script&gt;evil()");
    expect(html).not.toContain("<script>");
  });
});
