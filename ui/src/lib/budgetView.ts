/**
 * 预算面板的选择器：从选中 Run 的事件流里推导"这次 Run 花了多少、被拒了几次"。
 *
 * 纯函数、零 RPC：输入就是 App 已经加载的 `events.run` 事件（事实源），输出是
 * 展示视图。口径纪律（PRD §5.6 第 4 条 / PRD §5.7）：
 * - 已知费用与未知费用分开，未知不当 0——费用累计直接复用 `timeline.summarizeCost`
 *   的既有解释，本文件不发明第二种 cost 语义。
 * - token 只累计 `usage` 里显式给出整数的 input/output；`usage` 缺失（断流、超时、
 *   可能计费但没有 usage）的调用单独计 `unknownUsageAttempts`，绝不当作 0。
 * - 拒绝归类只认封闭集 `rule_id`（C4 契约编号），不做自由文本猜测；
 *   `model.error` 的 error 字段是自由文本，只进失败计数，不参与"是不是预算拒绝"的判定。
 */

import type { EventView } from "../api/types";
import { summarizeCost } from "./timeline";
import type { CostSummary } from "./timeline";
import { asRecord, readNumber, readString } from "./values";

/** 预算面板展示的 C4 拒绝编号（封闭集；`rules.py` RULE_IDS 的子集）。 */
export const BUDGET_REFUSAL_RULE_IDS: readonly string[] = [
  "dispatch.budget_refused",
  "dispatch.subtask_limit",
];

export interface BudgetRefusal {
  seq: number;
  ruleId: string;
  reason: string | null;
}

export interface RunLimitView {
  maxTurns: number | null;
  maxTotalTokens: number | null;
}

export interface BudgetView {
  /** `run.start` 事件记录的 Run 限额；事件或字段缺失时为 null（界面显示"未知"）。 */
  limits: RunLimitView | null;
  /** `model.intent` 计数：发出的模型请求数（≈ 已发生的请求轮次）。 */
  intents: number;
  /** `model.result` 计数：完成的模型调用数。 */
  results: number;
  /** `model.error` 计数：失败的模型调用（含预算拒发）。 */
  errors: number;
  /** usage 显式给出的累计 input token。 */
  inputTokens: number;
  /** usage 显式给出的累计 output token。 */
  outputTokens: number;
  /** 没有可用 usage 的 `model.result` 次数（用量未知，不当 0）。 */
  unknownUsageAttempts: number;
  /** 费用（已知/未知分列，复用 summarizeCost 口径）。 */
  cost: CostSummary;
  /** 预算/委派限额的拒绝记录（C4 编号封闭集内），按 seq 升序。 */
  refusals: BudgetRefusal[];
}

function readRunLimits(event: EventView): RunLimitView | null {
  const payload = asRecord(event?.payload);
  const raw = asRecord(payload.limits);
  if (raw === null || Object.keys(raw).length === 0) return null;
  // 契约字段是 max_turns / max_total_tokens；两个都读不到时按"没有限额记录"处理，
  // 不造默认值（limits 键存在但全空 = 旧事件形状，如实显示未知）。
  const maxTurns = readNumber(raw, "max_turns", "maxTurns");
  const maxTotalTokens = readNumber(raw, "max_total_tokens", "maxTotalTokens");
  if (maxTurns === null && maxTotalTokens === null) return null;
  return { maxTurns, maxTotalTokens };
}

/** 一条 `model.result` 的 usage 是否给出了可用 token 数。 */
function hasExplicitUsage(event: EventView): boolean {
  const payload = asRecord(event?.payload);
  const usage = asRecord(payload.usage);
  if (usage === null) return false;
  return readNumber(usage, "input_tokens", "inputTokens") !== null ||
    readNumber(usage, "output_tokens", "outputTokens") !== null;
}

/**
 * 事件流 → 预算视图。事件顺序无关（内部只做计数与合并）；
 * 空 Run（没有任何模型请求 / 限额记录 / 拒绝）返回 null，面板整个不渲染。
 */
export function buildBudgetView(events: EventView[]): BudgetView | null {
  const view: BudgetView = {
    limits: null,
    intents: 0,
    results: 0,
    errors: 0,
    inputTokens: 0,
    outputTokens: 0,
    unknownUsageAttempts: 0,
    cost: { totalMicros: 0, knownCalls: 0, unknownCalls: 0, calls: 0 },
    refusals: [],
  };
  const refusalIds = new Set<string>(BUDGET_REFUSAL_RULE_IDS);

  for (const event of Array.isArray(events) ? events : []) {
    const type = typeof event?.type === "string" ? event.type : "";
    if (type === "run.start" || type === "run.started") {
      const limits = readRunLimits(event);
      if (limits !== null) view.limits = limits;
      continue;
    }
    if (type === "model.intent") {
      view.intents += 1;
      continue;
    }
    if (type === "model.result") {
      view.results += 1;
      if (hasExplicitUsage(event)) {
        const usage = asRecord(asRecord(event?.payload).usage);
        const input = readNumber(usage, "input_tokens", "inputTokens");
        const output = readNumber(usage, "output_tokens", "outputTokens");
        if (input !== null) view.inputTokens += input;
        if (output !== null) view.outputTokens += output;
      } else {
        view.unknownUsageAttempts += 1;
      }
      continue;
    }
    if (type === "model.error") {
      view.errors += 1;
      continue;
    }
    if (type === "policy.denied" || type === "dispatch.rejected") {
      const payload = asRecord(event?.payload);
      const ruleId = readString(payload, "rule_id");
      if (ruleId === null || !refusalIds.has(ruleId)) continue;
      view.refusals.push({
        seq: typeof event?.seq === "number" && Number.isFinite(event.seq) ? event.seq : 0,
        ruleId,
        reason: readString(payload, "reason"),
      });
      continue;
    }
  }

  view.refusals.sort((left, right) => left.seq - right.seq);
  view.cost = summarizeCost(Array.isArray(events) ? events : []);

  const hasBudgetSignals =
    view.limits !== null ||
    view.intents > 0 ||
    view.results > 0 ||
    view.errors > 0 ||
    view.refusals.length > 0;
  return hasBudgetSignals ? view : null;
}
