import { describe, expect, it } from "vitest";
import type { EventView } from "../../api/types";
import { maxSeqOf, mergeEvents, readEventCost, summarizeCost } from "../timeline";

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

describe("事件合并", () => {
  it("按 seq 去重并升序排列", () => {
    const merged = mergeEvents(
      [event("run.started", {}, 2), event("run.started", {}, 1)],
      [event("model.result", {}, 2), event("model.result", {}, 3)],
    );
    expect(merged.map((item) => item.seq)).toEqual([1, 2, 3]);
    expect(merged[1]?.type).toBe("run.started");
  });

  it("超过上限只保留最近的事件", () => {
    const events = [1, 2, 3, 4, 5].map((seq) => event("run.started", {}, seq));
    const merged = mergeEvents([], events, 2);
    expect(merged.map((item) => item.seq)).toEqual([4, 5]);
    expect(maxSeqOf(merged)).toBe(5);
    expect(maxSeqOf([])).toBe(0);
  });
});

describe("成本读取（统一成微美元）", () => {
  it("Host 契约的裸数字按美元累计", () => {
    expect(readEventCost({ cost: 0.0123 })).toEqual({ kind: "micros", micros: 12_300 });
    expect(readEventCost({ cost: 1 })).toEqual({ kind: "micros", micros: 1_000_000 });
  });

  it("仓库 F13 的对象形状按微美元解释", () => {
    expect(readEventCost({ cost: { cost: 1234, currency: "USD" } })).toEqual({
      kind: "micros",
      micros: 1234,
    });
    expect(readEventCost({ cost: { micros: 88 } })).toEqual({ kind: "micros", micros: 88 });
    expect(readEventCost({ cost: { usd: 0.5 } })).toEqual({ kind: "micros", micros: 500_000 });
  });

  it("摊平的 F13 形状（顶层带 currency/units）按微美元解释", () => {
    expect(readEventCost({ cost: 1234, currency: "USD" })).toEqual({
      kind: "micros",
      micros: 1234,
    });
    expect(readEventCost({ cost: 777, units: { input_tokens: 10 } })).toEqual({
      kind: "micros",
      micros: 777,
    });
  });

  it("null / 缺价格 / 非 USD 币种记为未知，而不是 0", () => {
    expect(readEventCost({ cost: null })).toEqual({ kind: "unknown" });
    expect(readEventCost({ cost: { cost: null, currency: null } })).toEqual({
      kind: "unknown",
    });
    expect(readEventCost({ cost: { cost: 12, currency: "EUR" } })).toEqual({
      kind: "unknown",
    });
    expect(readEventCost({ cost: 12, currency: "EUR" })).toEqual({ kind: "unknown" });
    expect(readEventCost({ text: "没有费用字段" })).toBeNull();
    expect(readEventCost(null)).toBeNull();
  });

  it("显式的 cost_micros / cost_usd 优先", () => {
    expect(readEventCost({ cost_micros: 9999 })).toEqual({ kind: "micros", micros: 9999 });
    expect(readEventCost({ cost_usd: 1.5 })).toEqual({ kind: "micros", micros: 1_500_000 });
  });
});

describe("成本累计", () => {
  it("只累计 model.result，未知成本单独计数", () => {
    const summary = summarizeCost([
      event("model.result", { cost: 0.0123 }, 1),
      event("model.result", { cost: 0.0286 }, 2),
      event("tool.result", { cost: 99 }, 3),
      event("model.result", { cost: null }, 4),
      event("model.result", {}, 5),
    ]);
    expect(summary.calls).toBe(4);
    expect(summary.knownCalls).toBe(2);
    expect(summary.unknownCalls).toBe(2);
    expect(summary.totalMicros).toBe(12_300 + 28_600);
  });

  it("空时间线的成本是零调用，而不是 0 元", () => {
    expect(summarizeCost([])).toEqual({
      totalMicros: 0,
      knownCalls: 0,
      unknownCalls: 0,
      calls: 0,
    });
  });

  it("F13 对象形状也能累计", () => {
    const summary = summarizeCost([event("model.result", { cost: { cost: 4_321 } }, 1)]);
    expect(summary.totalMicros).toBe(4_321);
    expect(summary.knownCalls).toBe(1);
    expect(summary.unknownCalls).toBe(0);
  });
});