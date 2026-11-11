import { useState } from "react";
import type { ApprovalView, TaskView } from "../api/types";
import { formatRelative, shortenPath } from "../lib/format";
import {
  approvalStateLabel,
  approvalStateTone,
  describeApproval,
  isOpenApproval,
  isolationSentence,
  taskGoalSummary,
} from "../lib/humanize";
import { IconAlert, IconCheck, IconInfo } from "./Icons";
import { EmptyState, StatusPill } from "./StatusPill";

export interface ApprovalPanelProps {
  approvals: ApprovalView[];
  tasks: TaskView[];
  busy: string | null;
  now: number;
  onDecide: (approval: ApprovalView, decision: "approve" | "reject", reason?: string) => void;
}

export function ApprovalPanel({ approvals, tasks, busy, now, onDecide }: ApprovalPanelProps) {
  const open = approvals
    .filter((approval) => isOpenApproval(approval.state))
    .sort((left, right) => (left.created_at ?? 0) - (right.created_at ?? 0));
  const tasksById = new Map(tasks.map((task) => [task.task_id, task]));

  return (
    <aside className="pane approvals-pane">
      <div className="pane-head">
        <div className="pane-heading">
          <h2>待审批</h2>
          <span className={`count${open.length > 0 ? " count-alert" : ""}`}>{open.length}</span>
        </div>
      </div>
      <div className="pane-scroll">
        {open.length === 0 ? (
          <EmptyState
            icon={<IconCheck size={22} />}
            title="没有待审批的操作"
            hint="Agent 需要你确认删除、覆盖或访问目录外文件时，会出现在这里。"
          />
        ) : (
          <div className="approval-list">
            {open.map((approval) => {
              const task = tasksById.get(approval.task_id) ?? null;
              return (
                <ApprovalCard
                  key={approval.approval_id}
                  approval={approval}
                  task={task}
                  now={now}
                  busy={busy}
                  onDecide={onDecide}
                />
              );
            })}
            <div className="approval-note">
              <IconInfo size={13} />
              <span>允许后任务会立刻继续；拒绝后这次操作不会执行。决定会写入事件日志。</span>
            </div>
          </div>
        )}
      </div>
      <div className="pane-footnote">审批是一次性的：每次执行都要你确认，不会记住“以后都允许”。</div>
    </aside>
  );
}

export function ApprovalCard({
  approval,
  task,
  now,
  busy,
  onDecide,
}: {
  approval: ApprovalView;
  task: TaskView | null;
  now: number;
  busy: string | null;
  onDecide: ApprovalPanelProps["onDecide"];
}) {
  const [showReason, setShowReason] = useState(false);
  const [rejectReason, setRejectReason] = useState("");
  const display = describeApproval(approval);
  // 审批前就要让用户看到真实权限范围（PRD §8.1）：这条命令是不是受信任执行、
  // 后端自称的隔离级别有没有实测。
  const isolation = isolationSentence(approval.isolation);
  const working = busy === `approval:${approval.approval_id}`;
  const expired = approval.expires_at !== null && approval.expires_at <= now;
  const pending = isOpenApproval(approval.state) && !expired;
  const visibleResources = display.resources.slice(0, 4);
  const hiddenCount = display.resources.length - visibleResources.length;

  return (
    <article className={`approval-card${working ? " is-busy" : ""}`}>
      <div className="approval-top">
        <span className="approval-kicker">
          <IconAlert size={14} />
          需要你确认
        </span>
        <span className="approval-time">{formatRelative(approval.created_at, now)}</span>
      </div>

      <h3 className="approval-title">{display.actionText}</h3>

      {task !== null ? (
        <p className="approval-task" title={task.goal}>
          任务：{taskGoalSummary(task, 56)}
        </p>
      ) : (
        <p className="approval-task">任务：未知任务</p>
      )}

      {display.resources.length > 0 ? (
        <ul className="resource-list">
          {visibleResources.map((resource, index) => (
            <li key={`${resource}-${index}`}>
              <span className="resource-path mono" title={resource}>
                {shortenPath(resource, 62)}
              </span>
              {approval.reason !== null && approval.action === "c4.overwrite" && index === 0 ? (
                <span className="resource-note">会被覆盖</span>
              ) : null}
            </li>
          ))}
          {hiddenCount > 0 ? <li className="resource-more">还有 {hiddenCount} 项…</li> : null}
        </ul>
      ) : null}

      {display.ruleText !== display.actionText ? (
        <div className="approval-rule">触发规则：{display.ruleText}</div>
      ) : null}

      {isolation !== null ? (
        <p className={`approval-isolation approval-isolation-${isolation.tone}`}>
          {isolation.text}
        </p>
      ) : null}

      {approval.reason !== null && approval.reason !== "" ? (
        <p className="approval-reason">原因：{approval.reason}</p>
      ) : null}

      <div className="approval-status">
        <StatusPill tone={expired ? "warn" : approvalStateTone(approval.state)}>
          {expired ? "已过期" : approvalStateLabel(approval.state)}
        </StatusPill>
        {approval.expires_at !== null && !expired ? (
          <span className="approval-expiry">
            {formatExpiry(approval.expires_at, now)}
          </span>
        ) : null}
      </div>

      {pending ? (
        <>
          <div className="approval-actions">
            <button
              type="button"
              className="btn btn-primary btn-sm"
              onClick={() => onDecide(approval, "approve")}
              disabled={working}
            >
              {working ? "处理中…" : "允许"}
            </button>
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              onClick={() => onDecide(approval, "reject", rejectReason.trim() === "" ? undefined : rejectReason.trim())}
              disabled={working}
            >
              拒绝
            </button>
            <button
              type="button"
              className="link-btn"
              onClick={() => setShowReason((previous) => !previous)}
              disabled={working}
            >
              {showReason ? "收起理由" : "填写拒绝理由"}
            </button>
          </div>
          {showReason ? (
            <textarea
              className="textarea approval-reason-input"
              rows={2}
              value={rejectReason}
              placeholder="可选：告诉 Agent 为什么拒绝，会写入事件记录。"
              onChange={(event) => setRejectReason(event.target.value)}
              disabled={working}
            />
          ) : null}
        </>
      ) : (
        <p className="approval-expired-text">
          {expired ? "这条审批已经过期，需要让 Agent 重新发起。" : "这条审批已经处理过，无需再操作。"}
        </p>
      )}
    </article>
  );
}
function formatExpiry(expiresAt: number, now: number): string {
  const minutes = Math.ceil((expiresAt - now) / 60_000);
  if (minutes <= 1) return "不到 1 分钟内有效";
  if (minutes < 60) return `${minutes} 分钟内有效`;
  return `${Math.floor(minutes / 60)} 小时内有效`;
}
