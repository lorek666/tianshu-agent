import type { EventView, RunView } from "../api/types";
import { buildSubtaskViews } from "../lib/subtasks";
import type { SubtaskRow } from "../lib/subtasks";
import { runStateLabel, runStateTone, subtaskStateLabel } from "../lib/humanize";
import { formatInt } from "../lib/format";
import { IconInfo } from "./Icons";
import { StatusPill } from "./StatusPill";

/**
 * 子任务面板：这个 Run 派发了哪些子任务、各自的限额 / 状态 / 用量。
 *
 * 只有 `subtask.spawned` 没有 `subtask.finished` 的显示"进行中"（不冒充终态）；
 * 收尾用量缺失显示"用量未知"。没有委派事件的 Run 整个面板不渲染。
 * 目标正文不进父 Run 事件（契约只有 goal_digest），卡片不显示子任务目标原文。
 */

export function SubtaskPanel({ events, runs }: { events: EventView[]; runs: RunView[] }) {
  const rows = buildSubtaskViews(events, runs);
  if (rows.length === 0) return null;
  return <SubtaskPanelView rows={rows} />;
}

export function SubtaskPanelView({ rows }: { rows: SubtaskRow[] }) {
  return (
    <div className="cost-card subtask-card">
      <div className="pane-subhead">
        <h3>子任务</h3>
        <span className="pane-subhead-note">
          共 {rows.length} 个；子任务只读、深度 1，取消不退款
        </span>
      </div>
      <div className="subtask-list" role="list">
        {rows.map((row) => (
          <SubtaskCard key={row.subtaskId} row={row} />
        ))}
      </div>
    </div>
  );
}

function SubtaskCard({ row }: { row: SubtaskRow }) {
  const running = row.finishedState === null;
  const failed = row.finishedState === "failed" || row.finishedState === "fenced";
  const cancelled =
    row.finishedState === "cancelled" || row.finishedState === "cancelling";
  return (
    <div className="subtask-item" role="listitem">
      <div className="subtask-head">
        <span className="subtask-name">
          子任务 {row.goalDigest !== null ? row.goalDigest.slice(0, 8) : row.subtaskId.slice(0, 12)}
        </span>
        <StatusPill
          tone={running ? "info" : failed ? "danger" : cancelled ? "warn" : "success"}
          dot={running}
        >
          {running ? "进行中" : subtaskStateLabel(row.finishedState)}
        </StatusPill>
        {row.childRunState !== null && running ? (
          <StatusPill tone={runStateTone(row.childRunState)}>
            子运行：{runStateLabel(row.childRunState)}
          </StatusPill>
        ) : null}
      </div>
      <div className="subtask-meta">
        {row.limits !== null ? (
          <span>
            限额：轮次 {row.limits.maxTurns ?? "未知"} · token{" "}
            {row.limits.maxTokens !== null ? formatInt(row.limits.maxTokens) : "未知"} ·
            期限 {row.limits.deadlineMs !== null ? `${Math.round(row.limits.deadlineMs / 1000)}s` : "未知"}
          </span>
        ) : (
          <span>限额：未知</span>
        )}
        <span>
          用量：
          {row.usage !== null
            ? `轮次 ${row.usage.turns} · token ${formatInt(row.usage.tokens)}`
            : running
              ? "尚未收尾"
              : "用量未知"}
        </span>
      </div>
      {row.error !== null ? (
        <div className="subtask-error" role="status">
          <IconInfo size={13} />
          <span>{row.error}</span>
        </div>
      ) : null}
    </div>
  );
}
