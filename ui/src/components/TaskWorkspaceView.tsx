import { useEffect, useState } from "react";
import {
  EFFECTS_RUN_POLICY,
  PURE_ONLY_RUN_POLICY,
} from "../api/types";
import type {
  ApprovalView,
  EventView,
  RunExecutionPolicyV1,
  RunView,
  StreamingPreview,
  TaskView,
} from "../api/types";
import type { StreamStatus } from "../api/stream";
import type { ModelConfigView, SettingsApi, SettingsView } from "../api/settings";
import type { TimelineEntry } from "../lib/humanize";
import type { CostSummary } from "../lib/timeline";
import {
  formatClock,
  formatDuration,
  formatRelative,
  formatUsd,
  formatUsdExact,
  isSubCent,
} from "../lib/format";
import {
  controlLabel,
  effectStateLabel,
  effectStateTone,
  isOpenApproval,
  isTerminalRunState,
  outcomeLabel,
  outcomeTone,
  runStateLabel,
  runStateTone,
  taskStatusLabel,
  workspaceModeLabel,
} from "../lib/humanize";
import { ApprovalCard } from "./ApprovalPanel";
import { BudgetPanel } from "./BudgetPanel";
import {
  IconAlert,
  IconChart,
  IconEdit,
  IconFolder,
  IconInfo,
  IconPause,
  IconPlay,
  IconRefresh,
  IconSend,
  IconStop,
  IconUndo,
  IconX,
} from "./Icons";
import { ModelStatusControl } from "./ModelStatusControl";
import { RunTimeline } from "./RunTimeline";
import { EmptyState, Spinner, StatusPill } from "./StatusPill";
import { SubtaskPanel } from "./SubtaskPanel";
import { WelcomePet } from "./WelcomePet";
import type { WelcomePetStatus } from "./WelcomePet";

export interface TaskWorkspaceViewProps {
  task: TaskView;
  taskRuns: RunView[];
  allRuns: RunView[];
  approvals: ApprovalView[];
  selectedRun: RunView | null;
  selectedRunId: string | null;
  entries: TimelineEntry[];
  events: EventView[];
  hasMore: boolean;
  cost: CostSummary;
  streamStatus: StreamStatus;
  streamingPreview?: StreamingPreview | null;
  connectionLost: boolean;
  eventSeqHead: number;
  busy: string | null;
  now: number;
  executionPolicy: RunExecutionPolicyV1;
  enablePet: boolean;
  reduceMotion: boolean;
  modelConfig?: ModelConfigView | null;
  settingsApi?: SettingsApi | null;
  onModelUpdated?: (settings: SettingsView) => void;
  onSelectRun: (runId: string) => void;
  onExecutionPolicyChange: (taskId: string, policy: RunExecutionPolicyV1) => void;
  onStartRun: (taskId: string, policy: RunExecutionPolicyV1) => void;
  onRevise: (taskId: string, goal: string) => Promise<void>;
  onFollowUpSubmit: (task: TaskView, text: string, policy: RunExecutionPolicyV1) => Promise<void>;
  onPause: (run: RunView) => void;
  onResume: (run: RunView) => void;
  onRequestCancel: (run: RunView) => void;
  onUndo: (run: RunView) => void;
  onDecideApproval: (
    approval: ApprovalView,
    decision: "approve" | "reject",
    reason?: string,
  ) => void;
  onJumpToTask: (taskId: string) => void;
  onOpenModelSettings: () => void;
  onRefresh: () => void;
}

function deriveCompanionStatus(
  run: RunView | null,
  hasOpenApproval: boolean,
  connectionAlert: boolean,
): WelcomePetStatus {
  if (connectionAlert || hasOpenApproval) return "error";
  if (run === null) return "idle";
  switch (run.state) {
    case "running":
      return "executing";
    case "queued":
    case "created":
      return "thinking";
    case "waiting_approval":
      return "error";
    case "paused":
      return "sleeping";
    case "succeeded":
      return "done";
    case "failed":
      return "error";
    default:
      return "idle";
  }
}

export function TaskWorkspaceView({
  task,
  taskRuns,
  allRuns,
  approvals,
  selectedRun,
  selectedRunId,
  entries,
  events,
  hasMore,
  cost,
  streamStatus,
  streamingPreview = null,
  connectionLost,
  eventSeqHead,
  busy,
  now,
  executionPolicy,
  enablePet,
  reduceMotion,
  modelConfig = null,
  settingsApi = null,
  onModelUpdated,
  onSelectRun,
  onExecutionPolicyChange,
  onStartRun,
  onRevise,
  onFollowUpSubmit,
  onPause,
  onResume,
  onRequestCancel,
  onUndo,
  onDecideApproval,
  onJumpToTask,
  onOpenModelSettings,
  onRefresh,
}: TaskWorkspaceViewProps) {
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const [editingGoal, setEditingGoal] = useState(false);
  const [draftGoal, setDraftGoal] = useState(task.goal);
  const [reviseError, setReviseError] = useState<string | null>(null);
  const [followUpText, setFollowUpText] = useState("");
  const [followUpSubmitting, setFollowUpSubmitting] = useState(false);

  useEffect(() => {
    setDraftGoal(task.goal);
    setEditingGoal(false);
    setReviseError(null);
    setFollowUpText("");
  }, [task.task_id, task.goal_revision, task.goal]);

  const taskApprovals = approvals
    .filter((approval) => approval.task_id === task.task_id && isOpenApproval(approval.state))
    .sort((left, right) => (left.created_at ?? 0) - (right.created_at ?? 0));

  const otherOpenApprovals = approvals.filter(
    (approval) => approval.task_id !== task.task_id && isOpenApproval(approval.state),
  );

  const activeRun = task.active_run_id
    ? taskRuns.find((run) => run.run_id === task.active_run_id) ?? null
    : null;

  const starting = busy === `start-run:${task.task_id}`;
  const savingGoal = busy === `revise:${task.task_id}`;
  const pausing = busy === "control:pause";
  const resuming = busy === "control:resume";
  const cancelling = busy === "control:cancel";

  const controlTargetRun = selectedRun ?? activeRun;
  const terminal =
    controlTargetRun !== null ? isTerminalRunState(controlTargetRun.state) : true;
  const requested = controlTargetRun?.control_requested ?? null;
  const pauseDisabled =
    controlTargetRun === null ||
    requested !== null ||
    ["paused", "succeeded", "failed", "cancelled", "fenced"].includes(controlTargetRun.state);

  const submitGoalRevision = async (): Promise<void> => {
    const trimmed = draftGoal.trim();
    if (trimmed === "") {
      setReviseError("目标不能为空。");
      return;
    }
    setReviseError(null);
    try {
      await onRevise(task.task_id, trimmed);
      setEditingGoal(false);
    } catch (caught) {
      setReviseError(caught instanceof Error ? caught.message : "修改目标失败，请重试。");
    }
  };

  const submitFollowUp = async (): Promise<void> => {
    const trimmed = followUpText.trim();
    if (trimmed === "" || followUpSubmitting) return;
    setFollowUpSubmitting(true);
    try {
      await onFollowUpSubmit(task, trimmed, executionPolicy);
      setFollowUpText("");
    } finally {
      setFollowUpSubmitting(false);
    }
  };

  const connectionAlert = connectionLost || streamStatus === "disconnected";
  const companionPose = deriveCompanionStatus(
    selectedRun ?? activeRun,
    taskApprovals.length > 0,
    connectionAlert,
  );

  const primaryStateLabel =
    taskApprovals.length > 0
      ? `${taskApprovals.length} 项待批准`
      : selectedRun !== null
        ? runStateLabel(selectedRun.state)
        : taskStatusLabel(task.status);

  const primaryStateTone =
    taskApprovals.length > 0
      ? "approval"
      : selectedRun !== null
        ? runStateTone(selectedRun.state)
        : runStateTone(task.status);

  return (
    <>
      <div className="canvas-main">
        <header className="canvas-header">
          <div className="canvas-task-heading">
            <h2 className="canvas-task-title" title={task.goal}>
              {task.goal}
            </h2>
            <StatusPill tone={primaryStateTone} dot>
              {primaryStateLabel}
            </StatusPill>
            {selectedRun?.outcome !== null && selectedRun?.outcome !== undefined ? (
              <StatusPill tone={outcomeTone(selectedRun.outcome)}>
                结果：{outcomeLabel(selectedRun.outcome)}
              </StatusPill>
            ) : null}
            {requested !== null ? (
              <StatusPill tone="warn">已请求{controlLabel(requested)}</StatusPill>
            ) : null}
          </div>

          <div className="canvas-header-actions">
            {otherOpenApprovals.length > 0 ? (
              <button
                type="button"
                className="chip-btn is-warn"
                onClick={() => onJumpToTask(otherOpenApprovals[0]!.task_id)}
              >
                <span>另有 {otherOpenApprovals.length} 项待批准</span>
              </button>
            ) : null}

            {controlTargetRun !== null && !terminal && requested !== "cancel" ? (
              controlTargetRun.state === "paused" ? (
                <button
                  type="button"
                  className="chip-btn"
                  onClick={() => onResume(controlTargetRun)}
                  disabled={resuming}
                >
                  <IconPlay size={13} />
                  <span>{resuming ? "正在继续…" : "继续"}</span>
                </button>
              ) : (
                <button
                  type="button"
                  className="chip-btn"
                  onClick={() => onPause(controlTargetRun)}
                  disabled={pauseDisabled || pausing}
                >
                  <IconPause size={13} />
                  <span>
                    {pausing
                      ? "正在暂停…"
                      : requested === "pause"
                        ? "等待暂停…"
                        : "暂停"}
                  </span>
                </button>
              )
            ) : null}

            {controlTargetRun !== null && !terminal && requested !== "cancel" ? (
              <button
                type="button"
                className="chip-btn"
                onClick={() => onRequestCancel(controlTargetRun)}
                disabled={cancelling}
              >
                <IconStop size={13} />
                <span>取消</span>
              </button>
            ) : null}

            {selectedRun?.undoable ? (
              <button
                type="button"
                className="chip-btn"
                onClick={() => onUndo(selectedRun)}
                disabled={busy === "undo-plan"}
              >
                <IconUndo size={13} />
                <span>撤销这次修改</span>
              </button>
            ) : null}

            <button
              type="button"
              className={`chip-btn${inspectorOpen ? " is-active" : ""}`}
              data-testid="toggle-inspector-btn"
              aria-expanded={inspectorOpen}
              onClick={() => setInspectorOpen((previous) => !previous)}
            >
              <IconChart size={14} />
              <span>运行洞察</span>
            </button>

            <button
              type="button"
              className="icon-btn"
              onClick={onRefresh}
              title="立即刷新"
              aria-label="刷新"
            >
              <IconRefresh size={15} />
            </button>
          </div>
        </header>

        <div className="stream-scroll">
          <div className="stream-inner">
            {/* 用户任务目标气泡卡片（含工作区回显、编辑目标与待启动运行面板） */}
            <section className="user-goal-card" aria-label="任务目标">
              {editingGoal ? (
                <form
                  className="revise-form"
                  onSubmit={(event) => {
                    event.preventDefault();
                    void submitGoalRevision();
                  }}
                >
                  <textarea
                    className="textarea"
                    rows={3}
                    value={draftGoal}
                    autoFocus
                    onChange={(event) => setDraftGoal(event.target.value)}
                    disabled={savingGoal}
                  />
                  {activeRun !== null ? (
                    <p className="warn-text">
                      保存后，当前正在进行的运行会被停止（目标已变更，旧运行不会继续按旧目标执行）。
                    </p>
                  ) : null}
                  {reviseError !== null ? <p className="form-error">{reviseError}</p> : null}
                  <div className="form-actions">
                    <button
                      type="button"
                      className="btn btn-ghost btn-sm"
                      onClick={() => setEditingGoal(false)}
                      disabled={savingGoal}
                    >
                      取消
                    </button>
                    <button
                      type="submit"
                      className="btn btn-primary btn-sm"
                      disabled={savingGoal}
                    >
                      {savingGoal ? "正在保存…" : "保存目标"}
                    </button>
                  </div>
                </form>
              ) : (
                <p className="goal-text">{task.goal}</p>
              )}

              <div className="goal-card-meta">
                <div className="goal-ws-badges">
                  {task.workspaces.length === 0 ? (
                    <span className="goal-ws-tag">
                      <IconFolder size={13} />
                      <span>未绑定工作区（无本地文件写入权限）</span>
                    </span>
                  ) : (
                    task.workspaces.map((ws, index) => (
                      <span
                        className="goal-ws-tag"
                        key={`${ws.root_path}-${index}`}
                        title={ws.root_path}
                      >
                        <IconFolder size={13} />
                        <span className="mono">{ws.root_path}</span>
                        <span className="goal-ws-mode">
                          {workspaceModeLabel(ws.mode)}
                        </span>
                      </span>
                    ))
                  )}
                </div>

                <div className="goal-card-actions">
                  <span className="subtle">
                    更新于 {formatRelative(task.updated_at ?? task.created_at, now)}
                  </span>
                  <button
                    type="button"
                    className="link-btn"
                    onClick={() => {
                      setDraftGoal(task.goal);
                      setReviseError(null);
                      setEditingGoal((prev) => !prev);
                    }}
                    disabled={savingGoal}
                  >
                    <IconEdit size={12} />
                    {editingGoal ? "取消修改" : "修改目标"}
                  </button>
                </div>
              </div>

              {activeRun === null ? (
                <div className="idle-run-bar">
                  <div
                    className="segmented run-mode"
                    role="group"
                    aria-label="运行方式"
                  >
                    <button
                      type="button"
                      className={`segmented-item${executionPolicy.mode === "effects" ? " is-active" : ""}`}
                      aria-pressed={executionPolicy.mode === "effects"}
                      onClick={() =>
                        onExecutionPolicyChange(task.task_id, EFFECTS_RUN_POLICY)
                      }
                      disabled={starting}
                    >
                      每次询问
                    </button>
                    <button
                      type="button"
                      className={`segmented-item${executionPolicy.mode === "pure_only" ? " is-active" : ""}`}
                      aria-pressed={executionPolicy.mode === "pure_only"}
                      onClick={() =>
                        onExecutionPolicyChange(task.task_id, PURE_ONLY_RUN_POLICY)
                      }
                      disabled={starting}
                    >
                      仅读取与对话
                    </button>
                    <button
                      type="button"
                      className="segmented-item"
                      disabled
                      title="待后端授权契约接入"
                    >
                      自动审批
                    </button>
                    <button
                      type="button"
                      className="segmented-item"
                      disabled
                      title="待后端授权契约接入"
                    >
                      完全访问
                    </button>
                  </div>

                  <button
                    type="button"
                    className="btn btn-primary btn-sm"
                    onClick={() => onStartRun(task.task_id, executionPolicy)}
                    disabled={starting}
                  >
                    <IconPlay size={13} />
                    {starting ? "正在启动…" : "开始运行"}
                  </button>
                </div>
              ) : null}
            </section>

            {/* 内联待审批卡片（无需切栏即可直接批准或拒绝） */}
            {taskApprovals.length > 0 ? (
              <section className="inline-approvals-stack" aria-label="待你确认的操作">
                {taskApprovals.map((approval) => (
                  <ApprovalCard
                    key={approval.approval_id}
                    approval={approval}
                    task={task}
                    now={now}
                    busy={busy}
                    onDecide={onDecideApproval}
                  />
                ))}
              </section>
            ) : null}

            {/* 效果状态强提醒（partial / unknown / applied / none_applied） */}
            {selectedRun !== null ? <EffectNotice run={selectedRun} /> : null}

            {/* 运行事件时间线 */}
            {selectedRun !== null ? (
              <section className="stream-timeline-card">
                <RunTimeline
                  entries={entries}
                  hasMore={hasMore}
                  live={streamStatus === "live"}
                  streamingPreview={streamingPreview}
                />
              </section>
            ) : selectedRunId !== null ? (
              <div className="pane-loading">
                <Spinner label="正在读取运行详情…" />
              </div>
            ) : (
              <EmptyState
                title="当前还没有运行记录"
                hint="点击上方「开始运行」，或在下方输入补充要求直接发起新一轮运行。"
              />
            )}
          </div>
        </div>

        {/* 会话底部追问输入栏（无「回车发送」提示；发送按钮左侧放置模型状态胶囊） */}
        <div className="followup-bar">
          <form
            className="followup-form"
            onSubmit={(event) => {
              event.preventDefault();
              void submitFollowUp();
            }}
          >
            <input
              className="followup-input"
              data-testid="followup-input"
              value={followUpText}
              placeholder={
                activeRun === null
                  ? "继续追问，或输入补充要求启动新一轮运行…"
                  : "输入新要求以更新目标（当前运行会安全停止）…"
              }
              onChange={(event) => setFollowUpText(event.target.value)}
              disabled={followUpSubmitting || savingGoal}
            />

            <ModelStatusControl
              testId="followup-model-btn"
              model={modelConfig}
              settingsApi={settingsApi}
              disabled={followUpSubmitting}
              onModelUpdated={onModelUpdated}
              onOpenModelSettings={onOpenModelSettings}
            />

            <button
              type="submit"
              className="send-btn followup-send"
              data-testid="followup-send-btn"
              disabled={followUpSubmitting || followUpText.trim() === ""}
              aria-label="发送补充要求"
              title="发送"
            >
              <IconSend size={15} />
            </button>
          </form>
        </div>
      </div>

      {/* 按需展开的右侧运行洞察抽屉 */}
      {inspectorOpen ? (
        <aside
          className="inspector-drawer"
          data-testid="inspector-drawer"
          aria-label="运行洞察"
        >
          <div className="inspector-header">
            <strong className="inspector-title">运行洞察</strong>
            <button
              type="button"
              className="icon-btn"
              onClick={() => setInspectorOpen(false)}
              aria-label="关闭运行洞察"
            >
              <IconX size={15} />
            </button>
          </div>

          {enablePet ? (
            <div className="inspector-card inspector-companion-card">
              <WelcomePet
                status={companionPose}
                showPedestal={false}
                parallaxEnabled={false}
                reducedMotion={reduceMotion}
                size={68}
              />
              <div className="inspector-companion-meta">
                <div className="inspector-companion-headline">
                  {taskApprovals.length > 0
                    ? "等待你的审批决定"
                    : selectedRun !== null
                      ? `当前状态：${runStateLabel(selectedRun.state)}`
                      : "任务待命中"}
                </div>
                <div className="subtle">
                  事件水位 <span className="num">#{eventSeqHead}</span> · 关掉界面后台仍继续
                </div>
              </div>
            </div>
          ) : null}

          {selectedRun !== null ? (
            <>
              <div className="inspector-card">
                <div className="section-title">运行指标</div>
                <div className="inspector-metrics-grid">
                  <div className="summary-item">
                    <span className="summary-label">开始</span>
                    <strong className="num" title={formatRelative(selectedRun.started_at, now)}>
                      {formatClock(selectedRun.started_at)}
                    </strong>
                  </div>
                  <div className="summary-item">
                    <span className="summary-label">结束</span>
                    <strong className="num">
                      {selectedRun.ended_at !== null
                        ? formatClock(selectedRun.ended_at)
                        : "进行中"}
                    </strong>
                  </div>
                  <div className="summary-item">
                    <span className="summary-label">用时</span>
                    <strong className="num">
                      {formatDuration(selectedRun.started_at, selectedRun.ended_at)}
                    </strong>
                  </div>
                  <div className="summary-item">
                    <span className="summary-label">已生效修改</span>
                    <strong className="num">{selectedRun.n_applied} 处</strong>
                  </div>
                  <div className="summary-item">
                    <span className="summary-label">无法确认</span>
                    <strong
                      className={`num${selectedRun.n_unknown > 0 ? " text-warn" : ""}`}
                    >
                      {selectedRun.n_unknown} 处
                    </strong>
                  </div>
                </div>
              </div>

              <div className="cost-card inspector-cost-card">
                <div className="cost-main">
                  <span className="cost-label">累计成本</span>
                  <span
                    className="cost-value num"
                    title={
                      cost.knownCalls > 0
                        ? `精确值 ${formatUsdExact(cost.totalMicros)}`
                        : "没有可用的价格记录"
                    }
                  >
                    {cost.calls === 0
                      ? "暂无调用"
                      : cost.knownCalls === 0
                        ? "费用未知"
                        : formatUsd(cost.totalMicros)}
                  </span>
                  {cost.knownCalls > 0 ? (
                    <span className="cost-exact num">
                      精确 {formatUsdExact(cost.totalMicros)}
                    </span>
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
                  <div className="cost-note">
                    实际成本不足 1 美分，按两位小数显示为 $0.00；悬停可看精确值。
                  </div>
                ) : null}
              </div>

              <BudgetPanel events={events} />

              <SubtaskPanel events={events} runs={allRuns} />
            </>
          ) : null}

          <div className="inspector-card">
            <div className="section-title">
              运行历史
              <span className="count">{taskRuns.length}</span>
            </div>
            {taskRuns.length === 0 ? (
              <p className="subtle">暂无运行记录。</p>
            ) : (
              <ul className="run-list">
                {taskRuns.map((run, index) => (
                  <li key={run.run_id}>
                    <button
                      type="button"
                      className={`run-item${selectedRunId === run.run_id ? " is-selected" : ""}`}
                      onClick={() => onSelectRun(run.run_id)}
                      aria-current={selectedRunId === run.run_id ? "true" : undefined}
                    >
                      <span className="run-item-main">
                        <span className="run-item-title">
                          <StatusPill tone={runStateTone(run.state)} dot>
                            {runStateLabel(run.state)}
                          </StatusPill>
                          <span className="run-index">第 {taskRuns.length - index} 次运行</span>
                        </span>
                        <span className="run-item-meta">
                          <span>{formatRelative(run.started_at, now)}</span>
                          <span>·</span>
                          <span>{formatDuration(run.started_at, run.ended_at)}</span>
                          {run.effect_state !== null ? (
                            <span className="run-effect">
                              <span
                                className={`effect-dot tone-${effectStateTone(run.effect_state)}`}
                                aria-hidden="true"
                              />
                              {effectStateLabel(run.effect_state)}
                            </span>
                          ) : null}
                          {run.undoable ? <span className="undo-mark">可撤销</span> : null}
                        </span>
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </aside>
      ) : null}
    </>
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
            {counts}，改动已记录。
            {run.undoable ? "需要时可以撤销这次运行。" : "这次运行没有可撤销的快照。"}
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
