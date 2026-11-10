/**
 * 事件时间线的纯函数：合并、去重、成本累计。
 *
 * 界面不把事件当成“另一个事实源”：事件日志由守护进程持有，UI 只是把拉到的
 * 增量按 `seq` 合并进当前视图。因此这里只做确定性的集合运算，不做业务推断。
 */

import type { EventView } from "../api/types";
import { asRecord, readNumber, readString } from "./values";

/** 界面最多保留的时间线条数。超过时丢最早的事件，并在界面上明说。 */
export const TIMELINE_LIMIT = 2000;

/** 合并两批事件：按 seq 去重、升序、只保留最近 `limit` 条。 */
export function mergeEvents(
  existing: EventView[],
  incoming: EventView[],
  limit: number = TIMELINE_LIMIT,
): EventView[] {
  const bySeq = new Map<number, EventView>();
  for (const event of [...existing, ...incoming]) {
    if (typeof event?.seq !== "number" || !Number.isFinite(event.seq)) continue;
    if (!bySeq.has(event.seq)) bySeq.set(event.seq, event);
  }
  const sorted = [...bySeq.values()].sort((a, b) => a.seq - b.seq);
  if (sorted.length <= limit) return sorted;
  return sorted.slice(sorted.length - limit);
}

export function maxSeqOf(events: EventView[]): number {
  let max = 0;
  for (const event of events) {
    if (typeof event?.seq === "number" && Number.isFinite(event.seq) && event.seq > max) {
      max = event.seq;
    }
  }
  return max;
}

// ---------------------------------------------------------------- 成本

export type CostReading =
  | { kind: "micros"; micros: number }
  | { kind: "unknown" }
  | null;

/**
 * 从一条事件的 payload 里读成本，单位统一成「微美元（整数）」。
 *
 * 两种形状都支持，因为它们历史上都真实存在：
 * 1. M5 Host 的 EventView 契约：`payload.cost` 是一个可直接累加的数字（美元）。
 * 2. 仓库 F13 的事件 payload：`payload.cost = { cost: <微美元整数>, currency: "USD" }`
 *    （见 `src/tianshu/pricing.py` 的 `CostRecord.to_payload()`）。
 *
 * 数字到底是美元还是微美元没有元数据可以百分百区分，所以规则写死：
 * - 对象形状的 `{cost: n}` 一律按 F13 的微美元解释；
 * - 裸数字默认按美元解释（Host 契约）；
 * - 裸数字 + 顶层带 `currency` / `units` / `unit_price_ver` 时按 F13 微美元解释
 *   （这是 Host 把 F13 记录摊平的形状）。
 * 读不到、为 null 或币种不是 USD：记为“未知”，绝不当作 0 累加。
 */
export function readEventCost(payload: unknown): CostReading {
  const record = asRecord(payload);
  const hasOwn = (key: string) => Object.prototype.hasOwnProperty.call(record, key);

  const explicitMicros = readNumber(record, "cost_micros", "costMicros");
  if (explicitMicros !== null) return { kind: "micros", micros: Math.round(explicitMicros) };

  const explicitUsd = readNumber(record, "cost_usd", "costUsd");
  if (explicitUsd !== null) return { kind: "micros", micros: Math.round(explicitUsd * 1_000_000) };

  if (!hasOwn("cost")) return null;
  const cost = record.cost;
  if (cost === null || cost === undefined) return { kind: "unknown" };

  if (typeof cost === "number" && Number.isFinite(cost)) {
    const topCurrency = readString(record, "currency");
    const flattened = topCurrency !== null || hasOwn("units") || hasOwn("unit_price_ver");
    if (topCurrency !== null && topCurrency.toUpperCase() !== "USD") return { kind: "unknown" };
    return flattened
      ? { kind: "micros", micros: Math.round(cost) }
      : { kind: "micros", micros: Math.round(cost * 1_000_000) };
  }

  if (typeof cost === "object") {
    const recordCost = asRecord(cost);
    const currency = readString(recordCost, "currency");
    if (currency !== null && currency.toUpperCase() !== "USD") return { kind: "unknown" };
    const micros = readNumber(recordCost, "micros", "cost_micros");
    if (micros !== null) return { kind: "micros", micros: Math.round(micros) };
    const usd = readNumber(recordCost, "usd", "cost_usd");
    if (usd !== null) return { kind: "micros", micros: Math.round(usd * 1_000_000) };
    const nested = readNumber(recordCost, "cost");
    if (nested !== null) return { kind: "micros", micros: Math.round(nested) };
    return { kind: "unknown" };
  }

  return { kind: "unknown" };
}

export interface CostSummary {
  /** 已知成本总额（微美元）。 */
  totalMicros: number;
  /** 有价格、可计入总额的调用次数。 */
  knownCalls: number;
  /** 查不到价格 / 币种不认识的调用次数；不计入总额。 */
  unknownCalls: number;
  /** `model.result` 事件总数。 */
  calls: number;
}

/** 累计时间线里所有 `model.result` 的成本。未知成本单独计数，不混进 0。 */
export function summarizeCost(events: EventView[]): CostSummary {
  const summary: CostSummary = { totalMicros: 0, knownCalls: 0, unknownCalls: 0, calls: 0 };
  for (const event of events) {
    if (event?.type !== "model.result") continue;
    summary.calls += 1;
    const reading = readEventCost(event.payload);
    if (reading !== null && reading.kind === "micros") {
      summary.totalMicros += reading.micros;
      summary.knownCalls += 1;
    } else {
      summary.unknownCalls += 1;
    }
  }
  return summary;
}