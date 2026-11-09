import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type {
  ApprovalDecisionResult,
  ApprovalView,
  CreateTaskInput,
  EventView,
  EventsResult,
  RunEnvelope,
  RunView,
  StateView,
  TaskEnvelope,
  TaskView,
  UndoApplyResult,
  UndoPlanResult,
} from "./api/types";
import { RPC } from "./api/types";
import { createRealHostClient } from "./api/host";
import type { HostClient } from "./api/host";
import { createDemoHostClient } from "./demo/host";
import { isDemoRequested, readHostToken } from "./api/token";
import {
  normalizeEvents,
  normalizeRun,
  normalizeTask,
  normalizeUndoChanges,
} from "./api/normalize";
import { HostRpcError, humanizeHostError } from "./api/errors";
import { startEventStream } from "./api/stream";
import type { StreamStatus } from "./api/stream";
import { reduceEventTimeline } from "./lib/humanize";
import { isOpenApproval } from "./lib/humanize";
import { mergeEvents, maxSeqOf, summarizeCost } from "./lib/timeline";
import { shortenPath } from "./lib/format";
import { AppHeader, streamStatusText } from "./components/AppHeader";
import { ConnectionScreen } from "./components/ConnectionScreen";
import { TaskList } from "./components/TaskList";
import { TaskDetail } from "./components/TaskDetail";
import { RunDetail } from "./components/RunDetail";
import { ApprovalPanel } from "./components/ApprovalPanel";
import { NewTaskDialog } from "./components/NewTaskDialog";
import { UndoDialog } from "./components/UndoDialog";
import type { UndoPlanState } from "./components/UndoDialog";
import { Modal } from "./components/Modal";
import { EmptyState, Spinner } from "./components/StatusPill";
import { IconAlert, IconX } from "./components/Icons";

type Phase = "booting" | "no-token" | "unreachable" | "ready";

type ToastTone = "info" | "success" | "warn" | "danger";

interface ToastState {
  id: number;
  text: string;
  tone: ToastTone;
}

interface ConfirmState {
  title: string;
  body: string;
  confirmText: string;
  danger: boolean;
  onConfirm: () => void;
}

const EMPTY_DAEMON = { version: null, started_at: null, data_root: null };
const POLL_INTERVAL_MS = 3_000;
const REFRESH_DEBOUNCE_MS = 300;
const EVENT_PAGE_LIMIT = 200;
const EVENT_PAGE_LOOPS = 10;

function useNow(intervalMs: number): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(timer);
  }, [intervalMs]);
  return now;
}

function runsForTask(state: StateView, taskId: string): RunView[] {
  return state.runs
    .filter((run) => run.task_id === taskId)
    .sort((left, right) => (right.started_at ?? 0) - (left.started_at ?? 0));
}

function pickRunForTask(state: StateView, task: TaskView | null): RunView | null {
  if (task === null) return null;
  const runs = runsForTask(state, task.task_id);
  if (task.active_run_id !== null) {
    const active = runs.find((run) => run.run_id === task.active_run_id);
    if (active !== undefined) return active;
  }
  return runs[0] ?? null;
}

function pickInitialTask(state: StateView): TaskView | null {
  const withApproval = state.tasks.find((task) =>
    state.approvals.some(
      (approval) => approval.task_id === task.task_id && isOpenApproval(approval.state),
    ),
  );
  if (withApproval !== undefined) return withApproval;
  const withActiveRun = state.tasks.find((task) => task.active_run_id !== null);
  return withActiveRun ?? state.tasks[0] ?? null;
}

export function App() {
  const demoMode = useMemo(() => isDemoRequested(), []);
  const now = useNow(10_000);

  const [phase, setPhase] = useState<Phase>("booting");
  const [bootError, setBootError] = useState<string | null>(null);
  const [retrying, setRetrying] = useState(false);
  const [state, setState] = useState<StateView | null>(null);
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [runDetail, setRunDetail] = useState<RunView | null>(null);
  const [timeline, setTimeline] = useState<EventView[]>([]);
  const [timelineHasMore, setTimelineHasMore] = useState(false);
  const [streamStatus, setStreamStatus] = useState<StreamStatus>("connecting");
  const [streamDetail, setStreamDetail] = useState<string | null>(null);
  /** 手动重试事件流：自增后 effect 重建 stream（自动重连已在上限处停止）。 */
  const [streamEpoch, setStreamEpoch] = useState(0);
  const [connectionLost, setConnectionLost] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [errorBanner, setErrorBanner] = useState<string | null>(null);
  const [toast, setToast] = useState<ToastState | null>(null);
  const [newTaskOpen, setNewTaskOpen] = useState(false);
  const [confirm, setConfirm] = useState<ConfirmState | null>(null);
  const [undoPlan, setUndoPlan] = useState<UndoPlanState | null>(null);
  const [undoError, setUndoError] = useState<string | null>(null);

  const hostRef = useRef<HostClient | null>(null);
  const stateRef = useRef<StateView | null>(null);
  const selectedTaskRef = useRef<string | null>(null);
  const selectedRunRef = useRef<string | null>(null);
  const runDetailRef = useRef<RunView | null>(null);
  const timelineRef = useRef<EventView[]>([]);
  const timelineRunRef = useRef<string | null>(null);
  const connectAttemptRef = useRef(0);
  const retryInFlightRef = useRef(false);
  const pollFailuresRef = useRef(0);
  const runFailuresRef = useRef(0);
  const connectionLostRef = useRef(false);
  const refreshTimerRef = useRef<ReturnType<typeof window.setTimeout> | null>(null);
  const runFetchRef = useRef<string | null>(null);

  useEffect(() => {
    stateRef.current = state;
  }, [state]);

  useEffect(() => {
    selectedTaskRef.current = selectedTaskId;
  }, [selectedTaskId]);

  useEffect(() => {
    selectedRunRef.current = selectedRunId;
  }, [selectedRunId]);

  const showToast = useCallback((text: string, tone: ToastTone = "info") => {
    setToast({ id: Date.now(), text, tone });
  }, []);

  useEffect(() => {
    if (toast === null) return;
    const timer = window.setTimeout(() => setToast(null), 4_500);
    return () => window.clearTimeout(timer);
  }, [toast]);

  useEffect(
    () => () => {
      if (refreshTimerRef.current !== null) window.clearTimeout(refreshTimerRef.current);
    },
    [],
  );

  /** 切换 Run：清空该 Run 的时间线，立刻用已知快照占位，随后由 effect 拉取完整详情。 */
  const selectRun = useCallback((run: RunView | null) => {
    selectedRunRef.current = run?.run_id ?? null;
    setSelectedRunId(run?.run_id ?? null);
    setRunDetail(run);
    runDetailRef.current = run;
    timelineRef.current = [];
    timelineRunRef.current = null;
    setTimeline([]);
    setTimelineHasMore(false);
  }, []);

  const refreshSnapshot = useCallback(
    async (host: HostClient): Promise<void> => {
      try {
        const next = await host.getState();
        setState(next);
        stateRef.current = next;
        pollFailuresRef.current = 0;
        if (connectionLostRef.current) {
          connectionLostRef.current = false;
          setConnectionLost(false);
        }

        const currentTaskId = selectedTaskRef.current;
        const selectedTask =
          currentTaskId === null
            ? pickInitialTask(next)
            : next.tasks.find((task) => task.task_id === currentTaskId) ??
              pickInitialTask(next);

        if (selectedTask === null) {
          if (currentTaskId !== null) {
            selectedTaskRef.current = null;
            setSelectedTaskId(null);
            selectRun(null);
          }
          return;
        }

        if (selectedTask.task_id !== currentTaskId) {
          selectedTaskRef.current = selectedTask.task_id;
          setSelectedTaskId(selectedTask.task_id);
          selectRun(pickRunForTask(next, selectedTask));
          return;
        }

        const currentRunId = selectedRunRef.current;
        if (currentRunId !== null) {
          const stillThere =
            next.runs.some((run) => run.run_id === currentRunId) ||
            runDetailRef.current?.run_id === currentRunId;
          if (!stillThere) selectRun(pickRunForTask(next, selectedTask));
        } else if (selectedTask.active_run_id !== null) {
          const active = next.runs.find((run) => run.run_id === selectedTask.active_run_id);
          if (active !== undefined) selectRun(active);
        }
      } catch {
        pollFailuresRef.current += 1;
        if (pollFailuresRef.current >= 2 && !connectionLostRef.current) {
          connectionLostRef.current = true;
          setConnectionLost(true);
        }
      }
    },
    [selectRun],
  );

  const fetchEventsPage = useCallback(
    async (
      host: HostClient,
      runId: string,
      afterSeq: number,
    ): Promise<{ events: EventView[]; hasMore: boolean }> => {
      const collected: EventView[] = [];
      let after = afterSeq;
      let hasMore = false;
      for (let attempt = 0; attempt < EVENT_PAGE_LOOPS; attempt += 1) {
        const result = await host.rpc<EventsResult>(RPC.eventsRun, {
          run_id: runId,
          after_seq: after,
          limit: EVENT_PAGE_LIMIT,
        });
        const page = normalizeEvents(result.events);
        collected.push(...page);
        hasMore = Boolean(result.has_more);
        const nextAfter = maxSeqOf(page);
        if (!hasMore || nextAfter <= after) break;
        after = nextAfter;
      }
      return { events: collected, hasMore };
    },
    [],
  );

  const refreshRun = useCallback(
    async (host: HostClient, runId: string): Promise<void> => {
      if (runFetchRef.current === runId) return;
      runFetchRef.current = runId;
      const sameRun = timelineRunRef.current === runId;
      const after = sameRun ? maxSeqOf(timelineRef.current) : 0;
      try {
        const [runEnvelope, page] = await Promise.all([
          host.rpc<RunEnvelope>(RPC.runStatus, { run_id: runId }),
          fetchEventsPage(host, runId, after),
        ]);
        if (selectedRunRef.current !== runId) return;
        const normalizedRun = normalizeRun(runEnvelope.run);
        if (normalizedRun !== null) {
          setRunDetail(normalizedRun);
          runDetailRef.current = normalizedRun;
        }
        timelineRunRef.current = runId;
        setTimeline((previous) => {
          const base = sameRun ? previous : [];
          const merged = mergeEvents(base, page.events);
          timelineRef.current = merged;
          return merged;
        });
        setTimelineHasMore(page.hasMore);
        runFailuresRef.current = 0;
      } catch {
        runFailuresRef.current += 1;
        if (runFailuresRef.current >= 3 && !connectionLostRef.current) {
          connectionLostRef.current = true;
          setConnectionLost(true);
        }
      } finally {
        if (runFetchRef.current === runId) runFetchRef.current = null;
      }
    },
    [fetchEventsPage],
  );

  const refreshSelectedRun = useCallback(
    (host: HostClient): void => {
      const runId = selectedRunRef.current;
      if (runId !== null) void refreshRun(host, runId);
    },
    [refreshRun],
  );

  const scheduleRefresh = useCallback((): void => {
    if (refreshTimerRef.current !== null) return;
    refreshTimerRef.current = window.setTimeout(() => {
      refreshTimerRef.current = null;
      const host = hostRef.current;
      if (host === null) return;
      void refreshSnapshot(host);
      refreshSelectedRun(host);
    }, REFRESH_DEBOUNCE_MS);
  }, [refreshSnapshot, refreshSelectedRun]);

  const receiveEvent = useCallback(
    (event: EventView): void => {
      const runId = selectedRunRef.current;
      if (event.run_id !== null && event.run_id === runId) {
        timelineRunRef.current = event.run_id;
        setTimeline((previous) => {
          const merged = mergeEvents(previous, [event]);
          timelineRef.current = merged;
          return merged;
        });
      }
      scheduleRefresh();
    },
    [scheduleRefresh],
  );

  const connect = useCallback(
    async (options: { silent?: boolean } = {}): Promise<void> => {
      const attempt = ++connectAttemptRef.current;
      if (!options.silent) setPhase("booting");
      setBootError(null);
      if (options.silent) {
        if (retryInFlightRef.current) return;
        retryInFlightRef.current = true;
        setRetrying(true);
      }
      try {
        const token = demoMode ? null : readHostToken();
        if (!demoMode && token === null) {
          if (attempt === connectAttemptRef.current) setPhase("no-token");
          return;
        }
        const host: HostClient = demoMode ? createDemoHostClient() : createRealHostClient(token ?? "");
        const next = await host.getState();
        if (attempt !== connectAttemptRef.current) return;
        hostRef.current = host;
        setState(next);
        stateRef.current = next;

        const task = pickInitialTask(next);
        selectedTaskRef.current = task?.task_id ?? null;
        setSelectedTaskId(task?.task_id ?? null);
        selectRun(pickRunForTask(next, task));

        pollFailuresRef.current = 0;
        runFailuresRef.current = 0;
        connectionLostRef.current = false;
        setConnectionLost(false);
        setPhase("ready");
        setRetrying(false);
      } catch (error) {
        if (attempt !== connectAttemptRef.current) return;
        hostRef.current = null;
        setBootError(humanizeHostError(error));
        setPhase("unreachable");
      } finally {
        if (attempt === connectAttemptRef.current) {
          retryInFlightRef.current = false;
          setRetrying(false);
        }
      }
    },
    [demoMode, selectRun],
  );

  useEffect(() => {
    void connect();
  }, [connect]);

  useEffect(() => {
    if (phase !== "unreachable") return;
    const timer = window.setInterval(() => {
      if (retryInFlightRef.current) return;
      void connect({ silent: true });
    }, 5_000);
    return () => window.clearInterval(timer);
  }, [phase, connect]);

  useEffect(() => {
    if (phase !== "ready") return;
    const host = hostRef.current;
    if (host === null) return;
    const handle = startEventStream({
      host,
      initialSeq: () => Math.max(1, (stateRef.current?.event_seq_head ?? 0) + 1),
      onEvent: receiveEvent,
      onStatus: (status, detail) => {
        setStreamStatus(status);
        setStreamDetail(detail ?? null);
      },
      onEnd: () => {
        const currentHost = hostRef.current;
        if (currentHost === null) return;
        void refreshSnapshot(currentHost);
        refreshSelectedRun(currentHost);
      },
    });
    return () => {
      handle.stop();
    };
  }, [phase, streamEpoch, receiveEvent, refreshSnapshot, refreshSelectedRun]);

  useEffect(() => {
    if (phase !== "ready" || selectedRunId === null) return;
    const host = hostRef.current;
    if (host === null) return;
    void refreshRun(host, selectedRunId);
  }, [phase, selectedRunId, refreshRun]);

  useEffect(() => {
    if (phase !== "ready") return;
    const tick = (): void => {
      const host = hostRef.current;
      if (host === null || document.hidden) return;
      void refreshSnapshot(host);
      const runId = selectedRunRef.current;
      if (runId !== null) void refreshRun(host, runId);
    };
    const timer = window.setInterval(tick, POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [phase, refreshSnapshot, refreshRun]);

  const requireHost = useCallback((): HostClient => {
    const host = hostRef.current;
    if (host === null) {
      throw new HostRpcError("unreachable", "还没有连接守护进程");
    }
    return host;
  }, []);

  const handleSelectTask = useCallback(
    (taskId: string) => {
      if (selectedTaskRef.current === taskId) return;
      const current = stateRef.current;
      if (current === null) return;
      const task = current.tasks.find((item) => item.task_id === taskId);
      if (task === undefined) return;
      selectedTaskRef.current = taskId;
      setSelectedTaskId(taskId);
      selectRun(pickRunForTask(current, task));
    },
    [selectRun],
  );

  const handleSelectRun = useCallback(
    (runId: string) => {
      if (selectedRunRef.current === runId) return;
      const run = stateRef.current?.runs.find((item) => item.run_id === runId) ?? null;
      if (run !== null) {
        selectRun(run);
      } else {
        selectedRunRef.current = runId;
        setSelectedRunId(runId);
      }
    },
    [selectRun],
  );

  const handleRefresh = useCallback((): void => {
    const host = hostRef.current;
    if (host === null) return;
    void refreshSnapshot(host);
    refreshSelectedRun(host);
  }, [refreshSnapshot, refreshSelectedRun]);

  const handleCreateTask = useCallback(
    async (input: CreateTaskInput): Promise<void> => {
      const host = requireHost();
      setBusy("create-task");
      try {
        let created: TaskView | null;
        try {
          const envelope = await host.rpc<TaskEnvelope>(RPC.taskCreate, {
            goal: input.goal,
            workspaces: input.workspaces,
          });
          created = normalizeTask(envelope.task);
        } catch (error) {
          throw new Error(humanizeHostError(error));
        }
        if (created === null) {
          throw new Error("任务创建成功，但守护进程返回的数据无法识别。");
        }
        const task = created;
        setNewTaskOpen(false);
        setState((previous) =>
          previous === null
            ? previous
            : {
                ...previous,
                tasks: [task, ...previous.tasks.filter((item) => item.task_id !== task.task_id)],
              },
        );
        selectedTaskRef.current = task.task_id;
        setSelectedTaskId(task.task_id);
        selectRun(null);
        showToast("任务已创建，正在启动后台运行", "success");

        try {
          const runEnvelope = await host.rpc<RunEnvelope>(RPC.runStart, {
            task_id: task.task_id,
          });
          const run = normalizeRun(runEnvelope.run);
          if (run !== null) {
            selectRun(run);
            showToast("运行已在后台开始；关掉界面它也会继续", "success");
          }
          await refreshSnapshot(host);
          if (run !== null) await refreshRun(host, run.run_id);
        } catch (error) {
          setErrorBanner(
            `任务已创建，但启动运行失败：${humanizeHostError(error)}。可以在任务详情里点「开始运行」重试。`,
          );
          await refreshSnapshot(host);
        }
      } finally {
        setBusy(null);
      }
    },
    [refreshRun, refreshSnapshot, requireHost, selectRun, showToast],
  );

  const handleStartRun = useCallback(
    (taskId: string): void => {
      const host = hostRef.current;
      if (host === null) {
        setErrorBanner("还没有连接守护进程");
        return;
      }
      setBusy(`start-run:${taskId}`);
      void (async () => {
        try {
          const envelope = await host.rpc<RunEnvelope>(RPC.runStart, { task_id: taskId });
          const run = normalizeRun(envelope.run);
          if (run === null) throw new HostRpcError("bad_response", "守护进程返回的运行数据无法识别");
          selectRun(run);
          showToast("运行已在后台开始；关掉界面它也会继续", "success");
          await refreshSnapshot(host);
          await refreshRun(host, run.run_id);
        } catch (error) {
          setErrorBanner(humanizeHostError(error));
        } finally {
          setBusy(null);
        }
      })();
    },
    [refreshRun, refreshSnapshot, selectRun, showToast],
  );

  const handleRevise = useCallback(
    async (taskId: string, goal: string): Promise<void> => {
      const host = requireHost();
      setBusy(`revise:${taskId}`);
      try {
        let updated: TaskView | null;
        try {
          const envelope = await host.rpc<TaskEnvelope>(RPC.taskRevise, { task_id: taskId, goal });
          updated = normalizeTask(envelope.task);
        } catch (error) {
          throw new Error(humanizeHostError(error));
        }
        if (updated === null) {
          throw new Error("目标已更新，但守护进程返回的数据无法识别，请刷新后再确认。");
        }
        const nextTask = updated;
        setState((previous) =>
          previous === null
            ? previous
            : {
                ...previous,
                tasks: previous.tasks.map((item) =>
                  item.task_id === nextTask.task_id ? nextTask : item,
                ),
              },
        );
        showToast("目标已更新；旧目标下的运行会被停止", "info");
        await refreshSnapshot(host);
        refreshSelectedRun(host);
      } finally {
        setBusy(null);
      }
    },
    [refreshSelectedRun, refreshSnapshot, requireHost, showToast],
  );

  const handleControl = useCallback(
    (action: "pause" | "resume" | "cancel", run: RunView): void => {
      const host = hostRef.current;
      if (host === null) {
        setErrorBanner("还没有连接守护进程");
        return;
      }
      setBusy(`control:${action}`);
      void (async () => {
        try {
          const method =
            action === "pause" ? RPC.runPause : action === "resume" ? RPC.runResume : RPC.runCancel;
          const envelope = await host.rpc<RunEnvelope>(method, { run_id: run.run_id });
          const updated = normalizeRun(envelope.run);
          if (updated !== null && selectedRunRef.current === updated.run_id) {
            setRunDetail(updated);
            runDetailRef.current = updated;
          }
          showToast(
            action === "pause"
              ? "已请求暂停；执行器会在安全点停下"
              : action === "resume"
                ? "已请求继续运行"
                : "已请求取消；执行器会在安全点停下",
            "info",
          );
          await refreshSnapshot(host);
          if (selectedRunRef.current === run.run_id) await refreshRun(host, run.run_id);
        } catch (error) {
          setErrorBanner(humanizeHostError(error));
        } finally {
          setBusy(null);
        }
      })();
    },
    [refreshRun, refreshSnapshot, showToast],
  );

  const requestCancel = useCallback(
    (run: RunView): void => {
      setConfirm({
        title: "取消这次运行？",
        body: "取消后，已经生效的修改不会被自动回滚。运行停止后，可以在「运行详情」里撤销这次修改。",
        confirmText: "取消运行",
        danger: true,
        onConfirm: () => {
          setConfirm(null);
          handleControl("cancel", run);
        },
      });
    },
    [handleControl],
  );

  const handleDecide = useCallback(
    (approval: ApprovalView, decision: "approve" | "reject", reason?: string): void => {
      const host = hostRef.current;
      if (host === null) {
        setErrorBanner("还没有连接守护进程");
        return;
      }
      setBusy(`approval:${approval.approval_id}`);
      void (async () => {
        try {
          const params: Record<string, unknown> = {
            approval_id: approval.approval_id,
            decision,
          };
          if (reason !== undefined && reason !== "") params.reason = reason;
          const result = await host.rpc<ApprovalDecisionResult>(RPC.approvalsDecide, params);
          // 不做乐观更新：审批可能被并发处理（你点允许时别的窗口已经拒绝）。
          // 卡片等下一帧 /state 或服务端推送的 approval 事件再更新；这里只负责
          // 把“已处理”的结果告诉用户并立刻拉一次权威状态。
          showToast(
            decision === "approve"
              ? result.resumed
                ? "已允许，任务继续运行"
                : "已允许"
              : "已拒绝，这次操作不会执行",
            decision === "approve" ? "success" : "warn",
          );
          await refreshSnapshot(host);
          if (selectedRunRef.current === approval.run_id) await refreshRun(host, approval.run_id);
        } catch (error) {
          setErrorBanner(humanizeHostError(error));
          // 并发拒绝/过期时服务端状态已经变了：立刻同步一次，别让旧卡片继续挂着。
          await refreshSnapshot(host);
          if (selectedRunRef.current === approval.run_id) await refreshRun(host, approval.run_id);
        } finally {
          setBusy(null);
        }
      })();
    },
    [refreshRun, refreshSnapshot, showToast],
  );

  const handleOpenUndo = useCallback((run: RunView): void => {
    const host = hostRef.current;
    if (host === null) {
      setErrorBanner("还没有连接守护进程");
      return;
    }
    setBusy("undo-plan");
    setUndoError(null);
    void (async () => {
      try {
        const result = await host.rpc<UndoPlanResult>(RPC.undoPlan, { run_id: run.run_id });
        setUndoPlan({ runId: run.run_id, changes: normalizeUndoChanges(result) });
      } catch (error) {
        setErrorBanner(humanizeHostError(error));
      } finally {
        setBusy(null);
      }
    })();
  }, []);

  const handleApplyUndo = useCallback((): void => {
    const plan = undoPlan;
    if (plan === null) return;
    const host = hostRef.current;
    if (host === null) {
      setErrorBanner("还没有连接守护进程");
      return;
    }
    setBusy("undo-apply");
    setUndoError(null);
    void (async () => {
      try {
        const result = await host.rpc<UndoApplyResult>(RPC.undoApply, { run_id: plan.runId });
        setUndoPlan(null);
        if (result.refused !== undefined && result.refused !== null && result.refused !== "") {
          setErrorBanner(`撤销没有完全执行：${result.refused}`);
        } else {
          showToast(`已撤销 ${result.applied} 处修改`, "success");
        }
        await refreshSnapshot(host);
        if (selectedRunRef.current === plan.runId) await refreshRun(host, plan.runId);
      } catch (error) {
        setUndoError(humanizeHostError(error));
      } finally {
        setBusy(null);
      }
    })();
  }, [refreshRun, refreshSnapshot, showToast, undoPlan]);

  const handleRetryConnection = useCallback((): void => {
    setStreamEpoch((value) => value + 1);
    const host = hostRef.current;
    if (host !== null) {
      void refreshSnapshot(host);
      refreshSelectedRun(host);
    }
  }, [refreshSelectedRun, refreshSnapshot]);

  const tasks = state?.tasks ?? [];
  const approvals = state?.approvals ?? [];
  const selectedTask = tasks.find((task) => task.task_id === selectedTaskId) ?? null;
  const taskRuns = useMemo(
    () => (selectedTask === null || state === null ? [] : runsForTask(state, selectedTask.task_id)),
    [selectedTask, state],
  );
  const selectedRun = useMemo(() => {
    if (selectedRunId === null) return null;
    if (runDetail !== null && runDetail.run_id === selectedRunId) return runDetail;
    return state?.runs.find((run) => run.run_id === selectedRunId) ?? null;
  }, [selectedRunId, runDetail, state]);
  const timelineEntries = useMemo(() => reduceEventTimeline(timeline), [timeline]);
  const costSummary = useMemo(() => summarizeCost(timeline), [timeline]);
  const daemon = state?.daemon ?? EMPTY_DAEMON;

  if (phase !== "ready") {
    return (
      <ConnectionScreen
        kind={phase}
        message={phase === "unreachable" ? bootError : null}
        retrying={retrying}
        onRetry={() => void connect()}
      />
    );
  }

  return (
    <div className="app-shell">
      <AppHeader
        daemon={daemon}
        streamStatus={streamStatus}
        streamDetail={streamDetail}
        demo={demoMode}
        connectionLost={connectionLost}
        now={now}
        onRefresh={handleRefresh}
      />

      {demoMode ? (
        <div className="demo-strip">
          <span className="demo-strip-text">
            演示模式：以下内容来自内置示例数据，不会读写真实文件，也不会写入事件日志。
          </span>
          <a className="link-btn" href="?">
            退出演示
          </a>
        </div>
      ) : null}

      {connectionLost || streamStatus === "disconnected" ? (
        <div className="banner banner-warn" role="status">
          <IconAlert size={15} />
          <span>
            {streamStatus === "disconnected"
              ? "与守护进程的连接已断开，正在重试…"
              : "与守护进程的连接已断开，正在重试…（每 3 秒轮询兜底，任务应该仍在后台运行）"}
          </span>
          <button type="button" className="link-btn" onClick={handleRetryConnection}>
            重试
          </button>
        </div>
      ) : null}

      {errorBanner !== null ? (
        <div className="banner banner-danger" role="alert">
          <IconAlert size={15} />
          <span>{errorBanner}</span>
          <button
            type="button"
            className="icon-btn banner-close"
            onClick={() => setErrorBanner(null)}
            aria-label="关闭提示"
          >
            <IconX size={14} />
          </button>
        </div>
      ) : null}

      <div className="app-main">
        <TaskList
          tasks={tasks}
          runs={state?.runs ?? []}
          approvals={approvals}
          selectedTaskId={selectedTaskId}
          now={now}
          onSelectTask={handleSelectTask}
          onNewTask={() => setNewTaskOpen(true)}
        />

        <div className={`center-stack${selectedRun === null ? " is-single" : ""}`}>
          {selectedTask === null ? (
            <section className="pane center-empty">
              <EmptyState
                title="选择一个任务"
                hint="从左侧选择任务查看目标、目录和运行历史；有需要你确认的操作时，右侧会出现审批卡片。"
                action={
                  <button type="button" className="btn btn-primary btn-sm" onClick={() => setNewTaskOpen(true)}>
                    新建任务
                  </button>
                }
              />
            </section>
          ) : (
            <TaskDetail
              task={selectedTask}
              runs={taskRuns}
              approvals={approvals}
              selectedRunId={selectedRunId}
              busy={busy}
              now={now}
              onSelectRun={handleSelectRun}
              onStartRun={handleStartRun}
              onRevise={handleRevise}
              onRequestCancel={requestCancel}
            />
          )}

          {selectedTask !== null && selectedRun !== null ? (
            <RunDetail
              run={selectedRun}
              task={selectedTask}
              entries={timelineEntries}
              hasMore={timelineHasMore}
              cost={costSummary}
              streamStatus={streamStatus}
              busy={busy}
              now={now}
              onPause={() => handleControl("pause", selectedRun)}
              onResume={() => handleControl("resume", selectedRun)}
              onCancel={() => requestCancel(selectedRun)}
              onUndo={() => handleOpenUndo(selectedRun)}
              onRefresh={handleRefresh}
            />
          ) : selectedTask !== null && selectedRunId !== null ? (
            <section className="pane run-pane">
              <div className="pane-loading">
                <Spinner label="正在读取运行详情…" />
              </div>
            </section>
          ) : null}
        </div>

        <ApprovalPanel
          approvals={approvals}
          tasks={tasks}
          busy={busy}
          now={now}
          onDecide={handleDecide}
        />
      </div>

      <footer className="status-bar">
        <span
          className={`status-dot tone-${
            connectionLost || streamStatus === "disconnected"
              ? "danger"
              : streamStatus === "live"
                ? "success"
                : "warn"
          }`}
        />
        <span>
          {connectionLost ? "与守护进程的连接已断开，正在重试…" : streamStatusText(streamStatus, streamDetail)}
        </span>
        <span className="status-sep">·</span>
        <span>
          事件已同步至 <span className="num">#{state?.event_seq_head ?? 0}</span>
        </span>
        <span className="status-sep">·</span>
        <span>每 3 秒自动刷新</span>
        <span className="status-spacer" />
        {daemon.data_root !== null ? (
          <span className="status-path" title={daemon.data_root}>
            数据目录：{shortenPath(daemon.data_root, 52)}
          </span>
        ) : null}
        <button type="button" className="link-btn" onClick={handleRefresh}>
          立即刷新
        </button>
      </footer>

      {newTaskOpen ? (
        <NewTaskDialog
          open
          busy={busy === "create-task"}
          onClose={() => setNewTaskOpen(false)}
          onSubmit={handleCreateTask}
        />
      ) : null}

      {confirm !== null ? (
        <Modal
          title={confirm.title}
          onClose={() => setConfirm(null)}
          footer={
            <>
              <span className="modal-foot-hint">运行状态以守护进程为准。</span>
              <span className="modal-foot-actions">
                <button type="button" className="btn btn-ghost" onClick={() => setConfirm(null)}>
                  先不
                </button>
                <button
                  type="button"
                  className={confirm.danger ? "btn btn-danger" : "btn btn-primary"}
                  onClick={confirm.onConfirm}
                >
                  {confirm.confirmText}
                </button>
              </span>
            </>
          }
        >
          <p className="confirm-body">{confirm.body}</p>
        </Modal>
      ) : null}

      {undoPlan !== null ? (
        <UndoDialog
          plan={undoPlan}
          busy={busy === "undo-apply"}
          error={undoError}
          onConfirm={handleApplyUndo}
          onClose={() => {
            if (busy === "undo-apply") return;
            setUndoPlan(null);
            setUndoError(null);
          }}
        />
      ) : null}

      {toast !== null ? (
        <div className={`toast toast-${toast.tone}`} role="status">
          {toast.text}
        </div>
      ) : null}
    </div>
  );
}