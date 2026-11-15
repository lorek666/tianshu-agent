import { describe, expect, it } from "vitest";
import type { EventView } from "../../api/types";
import { buildBudgetView } from "../budgetView";

function event(
  type: string,
  payload: Record<string, unknown>,
  seq: number,
): EventView {
  return {
    seq,
    type,
    run_id: "run_1",
    task_id: "task_1",
    ts: seq * 1000,
    actor_kind: "core",
    payload,
  };
}

describe("预算视图：限额（J6）", () => {
  it("run.start 的 limits 展示为轮次 / token 上限", () => {
    const view = buildBudgetView([
      event("run.start", { limits: { max_turns: 8, max_total_tokens: 12_000 } }, 1),
    ]);
    expect(view).not.toBeNull();
    expect(view?.limits).toEqual({ maxTurns: 8, maxTotalTokens: 12_000 });
  });

  it("缺 limits 键（旧事件）显示未知，不造默认值", () => {
    // 有模型请求但没有限额记录：面板渲染，限额显示"未知"。
    const view = buildBudgetView([
      event("run.start", {}, 1),
      event("model.intent", { attempt: 0 }, 2),
    ]);
    expect(view).not.toBeNull();
    expect(view?.limits).toBeNull();
  });

  it("limits 是畸形形状时按没有限额记录处理，不崩", () => {
    const view = buildBudgetView([
      event("run.start", { limits: "nonsense" }, 1),
      event("run.start", { limits: { max_turns: "8" } }, 2),
      event("model.intent", { attempt: 0 }, 3),
    ]);
    // 两次都读不出合法数字：limits 保持 null，但模型请求计数让面板仍渲染。
    expect(view?.limits).toBeNull();
    expect(view?.intents).toBe(1);
  });
});

describe("预算视图：用量口径（J3 / J5）", () => {
  it("token 只累计显式整数 usage；缺失 usage 的调用进未知计数，不当 0", () => {
    const view = buildBudgetView([
      event("model.result", { usage: { input_tokens: 100, output_tokens: 40 } }, 1),
      event("model.result", { usage: { input_tokens: null, output_tokens: null } }, 2),
      event("model.result", { usage: {} }, 3),
      event("model.result", {}, 4),
      event("model.result", { usage: { input_tokens: 30, output_tokens: 10 } }, 5),
    ]);
    expect(view?.inputTokens).toBe(130);
    expect(view?.outputTokens).toBe(50);
    expect(view?.unknownUsageAttempts).toBe(3);
    expect(view?.results).toBe(5);
  });

  it("usage 里混入字符串数字不算显式用量（容错从紧）", () => {
    const view = buildBudgetView([
      event("model.result", { usage: { input_tokens: "100", output_tokens: 5 } }, 1),
    ]);
    expect(view?.inputTokens).toBe(0);
    expect(view?.outputTokens).toBe(5);
    // input 是字符串 → 不算完整显式用量；output 是合法数字所以已计入。
    // 未知计数的判据是"两个都读不到"：这里 output 读到了，所以不算未知。
    expect(view?.unknownUsageAttempts).toBe(0);
  });

  it("model.intent / model.error 分别计数", () => {
    const view = buildBudgetView([
      event("model.intent", { attempt: 0 }, 1),
      event("model.intent", { attempt: 1 }, 2),
      event("model.error", { error: "budget: 预占被拒" }, 3),
    ]);
    expect(view?.intents).toBe(2);
    expect(view?.errors).toBe(1);
    expect(view?.results).toBe(0);
  });
});

describe("预算视图：费用口径（J4）", () => {
  it("已知费用与未知费用分列，与 summarizeCost 一致", () => {
    const events = [
      event("model.result", { cost: 0.005, usage: { input_tokens: 1, output_tokens: 1 } }, 1),
      event("model.result", { cost: null, usage: { input_tokens: 1, output_tokens: 1 } }, 2),
      event("model.result", { cost: 0.007, usage: { input_tokens: 1, output_tokens: 1 } }, 3),
    ];
    const view = buildBudgetView(events);
    expect(view?.cost.calls).toBe(3);
    expect(view?.cost.knownCalls).toBe(2);
    expect(view?.cost.unknownCalls).toBe(1);
    expect(view?.cost.totalMicros).toBe(12_000);
  });
});

describe("预算视图：C4 拒绝记录（J7）", () => {
  it("budget_refused / subtask_limit 两条编号都进拒绝记录并带 reason", () => {
    const view = buildBudgetView([
      event("run.start", { limits: { max_turns: 4, max_total_tokens: 8000 } }, 1),
      event("policy.denied", { rule_id: "dispatch.budget_refused", reason: "额度耗尽" }, 2),
      event("dispatch.rejected", { rule_id: "dispatch.subtask_limit", reason: "累计 4 个" }, 3),
      // 非预算编号不进
      event("policy.denied", { rule_id: "c4.delete", reason: "普通拒绝" }, 4),
    ]);
    expect(view?.refusals.map((item) => item.ruleId)).toEqual([
      "dispatch.budget_refused",
      "dispatch.subtask_limit",
    ]);
    expect(view?.refusals[0]?.reason).toBe("额度耗尽");
    expect(view?.refusals[1]?.reason).toBe("累计 4 个");
  });

  it("拒绝记录按 seq 升序（事件乱序输入也稳定）", () => {
    const view = buildBudgetView([
      event("policy.denied", { rule_id: "dispatch.subtask_limit" }, 9),
      event("policy.denied", { rule_id: "dispatch.budget_refused" }, 3),
    ]);
    expect(view?.refusals.map((item) => item.seq)).toEqual([3, 9]);
  });
});

describe("预算视图：空态与容错（J8 / J9）", () => {
  it("没有任何预算相关事件返回 null（面板不渲染）", () => {
    expect(
      buildBudgetView([
        event("tool.intent", { tool: "write_file" }, 1),
        event("receipt", { receipt_id: "rcpt_x" }, 2),
      ]),
    ).toBeNull();
    expect(buildBudgetView([])).toBeNull();
  });

  it("只有 run.start 限额（没有调用）也渲染：限额本身是预算事实", () => {
    const view = buildBudgetView([
      event("run.start", { limits: { max_turns: 3, max_total_tokens: 1000 } }, 1),
    ]);
    expect(view).not.toBeNull();
    expect(view?.limits?.maxTurns).toBe(3);
  });

  it("事件数组本身畸形不抛错", () => {
    expect(() => buildBudgetView(undefined as unknown as EventView[])).not.toThrow();
    expect(
      buildBudgetView([null as unknown as EventView, event("model.intent", {}, 1)]),
    ).not.toBeNull();
  });
});
