import type { RunView, TaskView } from "../api/types";
import type { StreamStatus } from "../api/stream";
import type { CostSummary } from "../lib/timeline";
import type { TimelineEntry } from "../lib/humanize";
import {
  controlLabel,
  effectStateLabel,
  isTerminalRunState,
  outcomeLabel,
  outcomeTone,
  runStateLabel,
  runStateTone,
} from "../lib/humanize";
import { formatClock, formatDuration, formatRelative, formatUsd, formatUsdExact, isSubCent } from "../lib/format";
import { IconAlert, IconInfo, IconPause, IconPlay, IconRefresh, IconStop, IconUndo } from "./Icons";
import { StatusPill } from "./StatusPill";
import { RunTimeline } from "./RunTimeline";

export interface RunDetailProps {
  run: RunView;
  task: TaskView;
  entries: TimelineEntry[];
  hasMore: boolean;
  cost: CostSummary;
  streamStatus: StreamStatus;
  busy: string | null;
  now: number;
  onPause: () => void;
  onResume: () => void;
  onCancel: () => void;
  onUndo: () => void;
  onRefresh: () => void;
}

export function RunDetail({
  run,
  task,
  entries,
  hasMore,
  cost,
  streamStatus,
  busy,
  now,
  onPause,
  onResume,
  onCancel,
  onUndo,
  onRefresh,
}: RunDetailProps) {
  const terminal = isTerminalRunState(run.state);
  const requested = run.control_requested;
  const pausing = busy === "control:pause";
  const resuming = busy === "control:resume";
  const cancelling = busy === "control:cancel";
  const pauseDisabled = requested !== null || ["paused", "succeeded", "failed", "cancelled", "fenced"].includes(run.state);
  const showUndo = run.undoable;

  return (
    <section className="pane run-pane">
      <div className="pane-head run-head">
        <div className="run-title">
          <h2>运行详情</h2>
          <StatusPill tone={runStateTone(run.state)} dot>
            {runStateLabel(run.state)}
          </StatusPill>
          {run.outcome !== null ? (
            <StatusPill tone={outcomeTone(run.outcome)}>结果：{outcomeLabel(run.outcome)}</StatusPill>
          ) : null}
          {requested !== null ? (
            <StatusPill tone="warn">已请求{controlLabel(requested)}</StatusPill>
          ) : null}
          <span className="run-task-ref" title={task.goal}>
            {task.goal.length > 30 ? `${task.goal.slice(0, 30)}…` : task.goal}
          </span>
        </div>
        <div className="head-actions">
          {!terminal && requested !== "cancel" ? (
            run.state === "paused" ? (
              <button type="button" className="btn btn-ghost btn-sm" onClick={onResume} disabled={resuming}>
                <IconPlay size={13} />
                {resuming ? "正在继续…" : "继续"}
              </button>
            ) : (
              <button type="button" className="btn btn-ghost btn-sm" onClick={onPause} disabled={pauseDisabled || pausing}>
                <IconPause size={13} />
                {pausing ? "正在暂停…" : requested === "pause" ? "等待暂停…" : "暂停"}
              </button>
            )
          ) : null}
          {!terminal && requested !== "cancel" ? (
            <button type="button" className="btn btn-ghost btn-sm" onClick={onCancel} disabled={cancelling}>
              <IconStop size={13} />
              取消
            </button>
          ) : null}
          {showUndo ? (
            <button type="button" className="btn btn-ghost btn-sm" onClick={onUndo} disabled={busy === "undo-plan"}>
              <IconUndo size={13} />
              撤销这次修改
            </button>
          ) : null}
          <button type="button" className="icon-btn" onClick={onRefresh} title="立即刷新" aria-label="立即刷新">
            <IconRefresh size={15} />
          </button>
        </div>
      </div>

      <div className="pane-scroll run-scroll">
        <div className="run-summary">
          <div className="summary-item">
            <span className="summary-label">开始</span>
            <strong className="num" title={formatRelative(run.started_at, now)}>
              {formatClock(run.started_at)}
            </strong>
          </div>
          <div className="summary-item">
            <span className="summary-label">结束</span>
            <strong className="num">{run.ended_at !== null ? formatClock(run.ended_at) : "进行中"}</strong>
          </div>
          <div className="summary-item">
            <span className="summary-label">用时</span>
            <strong className="num">{formatDuration(run.started_at, run.ended_at)}</strong>
          </div>
          <div className="summary-item">
            <span className="summary-label">已生效修改</span>
            <strong className="num">{run.n_applied} 处</strong>
          </div>
          <div className="summary-item">
            <span className="summary-label">无法确认</span>
            <strong className={`num${run.n_unknown > 0 ? " text-warn" : ""}`}>{run.n_unknown} 处</strong>
          </div>
        </div>

        <EffectNotice run={run} />

        <div className="cost-card">
          <div className="cost-main">
            <span className="cost-label">累计成本</span>
            <span
              className="cost-value num"
              title={cost.knownCalls > 0 ? `精确值 ${formatUsdExact(cost.totalMicros)}` : "没有可用的价格记录"}
            >
              {cost.calls === 0
                ? "暂无调用"
                : cost.knownCalls === 0
                  ? "费用未知"
                  : formatUsd(cost.totalMicros)}
            </span>
            {cost.knownCalls > 0 ? (
              <span className="cost-exact num">精确 {formatUsdExact(cost.totalMicros)}</span>
            ) : null}
          </div>
          <div className="cost-sub">
            {cost.calls === 0
              ? "这次运行还没有模型调用记录。"
              : `${cost.calls} 次模型调用${
                  cost.unknownCalls > 0
                    ? `；其中 ${cost.unknownCalls} 次没有价格，未计入总额（“未知”不等于 0）`
                    : ""
                }。`}
          </div>
          {isSubCent(cost.totalMicros) ? (
            <div className="cost-note">实际成本不足 1 美分，按两位小数显示为 $0.00；悬停可看精确值。</div>
          ) : null}
        </div>

        <RunTimeline entries={entries} hasMore={hasMore} live={streamStatus === "live"} />
      </div>
    </section>
  );
}

function EffectNotice({ run }: { run: RunView }) {
  const effect = run.effect_state;
  const terminal = isTerminalRunState(run.state);
  const counts = `已生效 ${run.n_applied} 处${run.n_unknown > 0 ? `，另有 ${run.n_unknown} 处无法确认` : ""}`;

  if (effect === "partial") {
    return (
      <div className="effect-notice danger" role="status">
        <span className="effect-icon">
          <IconAlert size={18} />
        </span>
        <div>
          <div className="effect-title">{effectStateLabel(effect)}</div>
          <div className="effect-body">
            {counts}。请先检查工作区里这些文件，再决定是否在右上角「撤销这次修改」。
          </div>
        </div>
      </div>
    );
  }
  if (effect === "unknown") {
    return (
      <div className="effect-notice warn" role="status">
        <span className="effect-icon">
          <IconInfo size={18} />
        </span>
        <div>
          <div className="effect-title">{effectStateLabel(effect)}</div>
          <div className="effect-body">
            守护进程没能确认这次运行有没有改动文件。请检查绑定目录里的内容，必要时手动恢复。
          </div>
        </div>
      </div>
    );
  }
  if (effect === "applied") {
    return (
      <div className="effect-notice success" role="status">
        <span className="effect-icon">
          <IconInfo size={17} />
        </span>
        <div>
          <div className="effect-title">{effectStateLabel(effect)}</div>
          <div className="effect-body">
            {counts}，改动已记录。{run.undoable ? "需要时可以撤销这次运行。" : "这次运行没有可撤销的快照。"}
          </div>
        </div>
      </div>
    );
  }
  if (terminal && (effect === "none_applied" || effect === "none")) {
    return (
      <div className="effect-notice neutral" role="status">
        <span className="effect-icon">
          <IconInfo size={17} />
        </span>
        <div>
          <div className="effect-title">{effectStateLabel(effect)}</div>
          <div className="effect-body">这次运行没有改动工作区里的文件。</div>
        </div>
      </div>
    );
  }
  if (run.n_unknown > 0) {
    return (
      <div className="effect-notice warn" role="status">
        <span className="effect-icon">
          <IconAlert size={18} />
        </span>
        <div>
          <div className="effect-title">有 {run.n_unknown} 处修改无法确认</div>
          <div className="effect-body">运行还在推进中；结束后会给出明确的效果结论。</div>
        </div>
      </div>
    );
  }
  return null;
}