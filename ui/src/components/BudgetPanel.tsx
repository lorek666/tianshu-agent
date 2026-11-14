import type { EventView } from "../api/types";
import { buildBudgetView } from "../lib/budgetView";
import type { BudgetView } from "../lib/budgetView";
import { budgetRefusalLabel } from "../lib/humanize";
import { formatInt, formatUsd, formatUsdExact } from "../lib/format";
import { IconInfo } from "./Icons";
import { StatusPill } from "./StatusPill";

/**
 * 预算面板：这次 Run 的限额、用量与拒绝记录。
 *
 * 数据全部来自父 Run 事件流（`run.start` 的 limits / `model.result` 的 usage 与
 * cost / C4 拒绝事件）；账户级账本（上限设置、未决预占、下载字节维度）在事件里
 * 不存在，面板不展示也不暗示"看到的就是账本全部"。
 * 没有任何预算相关事件的 Run（例如纯执行没有模型调用）整个不渲染。
 */

export function BudgetPanel({ events }: { events: EventView[] }) {
  const view = buildBudgetView(events);
  if (view === null) return null;
  return <BudgetPanelView view={view} />;
}

export function BudgetPanelView({ view }: { view: BudgetView }) {
  const totalTokens =
    view.inputTokens + view.outputTokens > 0 || view.unknownUsageAttempts > 0
      ? { known: view.inputTokens + view.outputTokens, unknown: view.unknownUsageAttempts }
      : null;
  return (
    <div className="cost-card budget-card">
      <div className="pane-subhead">
        <h3>预算与用量</h3>
        <span className="pane-subhead-note">基于已加载的最近事件；用量未知不当作 0</span>
      </div>
      <div className="run-summary budget-summary">
        <div className="summary-item">
          <span className="summary-label">模型请求</span>
          <strong className="num">{view.intents}</strong>
        </div>
        <div className="summary-item">
          <span className="summary-label">完成调用</span>
          <strong className="num">{view.results}</strong>
        </div>
        <div className="summary-item">
          <span className="summary-label">失败调用</span>
          <strong className={`num${view.errors > 0 ? " text-warn" : ""}`}>{view.errors}</strong>
        </div>
        <div className="summary-item">
          <span className="summary-label">Token</span>
          <strong className="num">
            {totalTokens === null
              ? "无记录"
              : totalTokens.known > 0
                ? formatInt(totalTokens.known)
                : "用量未知"}
          </strong>
        </div>
        {view.limits !== null ? (
          <>
            <div className="summary-item">
              <span className="summary-label">轮次上限</span>
              <strong className="num">
                {view.limits.maxTurns !== null ? view.limits.maxTurns : "未知"}
              </strong>
            </div>
            <div className="summary-item">
              <span className="summary-label">Token 上限</span>
              <strong className="num">
                {view.limits.maxTotalTokens !== null
                  ? formatInt(view.limits.maxTotalTokens)
                  : "未知"}
              </strong>
            </div>
          </>
        ) : null}
      </div>
      {view.unknownUsageAttempts > 0 ? (
        <div className="cost-note">
          有 {view.unknownUsageAttempts} 次调用没有用量记录（断流或超时可能已计费）；已知
          token {totalTokens?.known ?? 0} 不代表全部消耗。
        </div>
      ) : null}
      {view.cost.calls > 0 ? (
        <div className="cost-sub">
          {view.cost.knownCalls > 0
            ? `已知费用 ${formatUsd(view.cost.totalMicros)}（精确 ${formatUsdExact(
                view.cost.totalMicros,
              )}）`
            : "费用未知"}
          {view.cost.unknownCalls > 0
            ? `；另有 ${view.cost.unknownCalls} 次调用没有价格记录，未计入总额`
            : ""}
          。
        </div>
      ) : null}
      {view.refusals.length > 0 ? (
        <div className="budget-refusals" role="list">
          {view.refusals.map((refusal) => (
            <div className="budget-refusal" role="listitem" key={`refusal-${refusal.seq}`}>
              <StatusPill tone="warn">
                <IconInfo size={13} />
                {budgetRefusalLabel(refusal.ruleId)}
              </StatusPill>
              {refusal.reason !== null ? (
                <span className="budget-refusal-reason">{refusal.reason}</span>
              ) : null}
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}
