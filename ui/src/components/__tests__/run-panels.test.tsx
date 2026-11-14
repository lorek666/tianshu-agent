// @vitest-environment jsdom

import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import type { EventView, RunView } from "../../api/types";
import { BudgetPanel } from "../BudgetPanel";
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

function run(runId: string, state: string): RunView {
  return {
    run_id: runId,
    task_id: "task_parent",
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
});

describe("子任务面板渲染", () => {
  it("进行中的子任务显示进行中与限额，不显示 usage 0（J1 / J2）", () => {
    const html = renderToStaticMarkup(
      <SubtaskPanel
        events={[event("subtask.spawned", SPAWNED, 1)]}
        runs={[run("run_child_1", "running")]}
      />,
    );
    expect(html).toContain("子任务");
    expect(html).toContain("进行中");
    expect(html).toContain("子运行：运行中");
    expect(html).toContain("轮次 2");
    expect(html).toContain("300s");
    expect(html).not.toContain("已完成");
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
        runs={[run("run_child_1", "failed")]}
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
