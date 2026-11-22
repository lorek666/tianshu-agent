import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type {
  ApprovalDecisionResult,
  ApprovalView,
  CreateTaskInput,
  EventView,
  EventsResult,
  ModelStreamEvent,
  RunAttachmentInput,
  RunEnvelope,
  RunExecutionPolicyV1,
  RunModelSelection,
  RunView,
  StateView,
  StreamingPreview,
  TaskEnvelope,
  TaskView,
  UndoApplyResult,
  UndoPlanResult,
} from "./api/types";
import { EFFECTS_RUN_POLICY, RPC } from "./api/types";
import { createRealHostClient } from "./api/host";
import type { HostClient } from "./api/host";
import { createSkillsApi } from "./api/skills";
import type { SkillsApi } from "./api/skills";
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
import { isOpenApproval, reduceEventTimeline } from "./lib/humanize";
import { maxSeqOf, mergeEvents, summarizeCost } from "./lib/timeline";
import { ConnectionScreen } from "./components/ConnectionScreen";
import { MemoryDialog } from "./components/MemoryDialog";
import { createMemoryApi } from "./api/memory";
import type { MemoryApi } from "./api/memory";
import { SettingsDialog } from "./components/SettingsDialog";
import type { SettingsTab, UiPreferences } from "./components/SettingsDialog";
import { createSettingsApi } from "./api/settings";
import type { SettingsApi, SettingsView } from "./api/settings";
import { UndoDialog } from "./components/UndoDialog";
import type { UndoPlanState } from "./components/UndoDialog";
import { Modal } from "./components/Modal";
import { Sidebar, basenameOfPath } from "./components/Sidebar";
import type { ProjectWorkspace } from "./components/Sidebar";
import { WelcomeComposer } from "./components/WelcomeComposer";
import { TaskWorkspaceView } from "./components/TaskWorkspaceView";
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

const INITIAL_PREFERENCES: UiPreferences = {
  theme: "light",
  enablePet: true,
  enableParallax: true,
  reduceMotion: false,
  defaultPolicy: EFFECTS_RUN_POLICY,
  previewPose: "welcome",
};

function useNow(intervalMs: number): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(timer);
  }, [intervalMs]);
  return now;
}

function useSystemDarkScheme(): boolean {
  const [systemDark, setSystemDark] = useState<boolean>(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
      return false;
    }
    return window.matchMedia("(prefers-color-scheme: dark)").matches;
  });

  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
      return;
    }
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const listener = (event: MediaQueryListEvent) => setSystemDark(event.matches);
    media.addEventListener("change", listener);
    return () => media.removeEventListener("change", listener);
  }, []);

  return systemDark;
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
  const [memoryApi, setMemoryApi] = useState<MemoryApi | null>(null);
  const [settingsApi, setSettingsApi] = useState<SettingsApi | null>(null);
  const [skillsApi, setSkillsApi] = useState<SkillsApi | null>(null);
  const now = useNow(10_000);
  const systemDark = useSystemDarkScheme();

  const [preferences, setPreferences] = useState<UiPreferences>(INITIAL_PREFERENCES);
  const effectiveTheme: "light" | "dark" =
    preferences.theme === "system" ? (systemDark ? "dark" : "light") : preferences.theme;

  useEffect(() => {
    document.documentElement.setAttribute("data-theme", effectiveTheme);
  }, [effectiveTheme]);

  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [composingNewTask, setComposingNewTask] = useState(false);
  const [activeProject, setActiveProject] = useState<ProjectWorkspace | null>(null);
  const [pinnedProjects, setPinnedProjects] = useState<ProjectWorkspace[]>([]);

  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsTab, setSettingsTab] = useState<SettingsTab>("models");
  const [modelSettings, setModelSettings] = useState<SettingsView | null>(null);

  const [phase, setPhase] = useState<Phase>("booting");
  const [bootError, setBootError] = useState<string | null>(null);
  const [retrying, setRetrying] = useState(false);
  const [state, setState] = useState<StateView | null>(null);
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [runPolicies, setRunPolicies] = useState<Record<string, RunExecutionPolicyV1>>({});
  const [runModes, setRunModes] = useState<Record<string, "execute" | "plan">>({});
  const [runDetail, setRunDetail] = useState<RunView | null>(null);
  const [timeline, setTimeline] = useState<EventView[]>([]);
  const [timelineHasMore, setTimelineHasMore] = useState(false);
  const [streamingPreviews, setStreamingPreviews] = useState<Record<string, StreamingPreview>>({});
  const [streamStatus, setStreamStatus] = useState<StreamStatus>("connecting");
  const [streamDetail, setStreamDetail] = useState<string | null>(null);
  const [streamEpoch, setStreamEpoch] = useState(0);
  const [connectionLost, setConnectionLost] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [errorBanner, setErrorBanner] = useState<string | null>(null);
  const [toast, setToast] = useState<ToastState | null>(null);
  const [memoryOpen, setMemoryOpen] = useState(false);
  const [confirm, setConfirm] = useState<ConfirmState | null>(null);
  const [undoPlan, setUndoPlan] = useState<UndoPlanState | null>(null);
  const [undoError, setUndoError] = useState<string | null>(null);

  const hostRef = useRef<HostClient | null>(null);
  const stateRef = useRef<StateView | null>(null);
  const composingNewTaskRef = useRef(false);
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
  const unsentAttachments = useRef(new Map<string, {
    revision: number; files: RunAttachmentInput[]; selection: RunModelSelection;
  }>());
  const workflowRequests = useRef(new Map<string, string>());

  const requestIdFor = useCallback((key: string): string => {
    const existing = workflowRequests.current.get(key);
    if (existing !== undefined) return existing;
    const id = crypto.randomUUID();
    workflowRequests.current.set(key, id);
    return id;
  }, []);

  useEffect(() => {
    stateRef.current = state;
  }, [state]);

  useEffect(() => {
    composingNewTaskRef.current = composingNewTask;
  }, [composingNewTask]);

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

  const updatePreferences = useCallback((patch: Partial<UiPreferences>) => {
    setPreferences((previous) => ({ ...previous, ...patch }));
  }, []);

  const handleModelUpdated = useCallback(
    (updated: SettingsView) => {
      setModelSettings(updated);
      showToast(`模型已切换为：${updated.model.summary}`, "success");
    },
    [showToast],
  );

  const openSettingsAt = useCallback((tab: SettingsTab = "models") => {
    setSettingsTab(tab);
    setSettingsOpen(true);
  }, []);

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

        // 用户主动停留在新建任务欢迎界面时，轮询不应强行跳回旧会话
        if (composingNewTaskRef.current) {
          return;
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

  const receiveModelStream = useCallback((stream: ModelStreamEvent): void => {
    setStreamingPreviews((previous) => {
      if (stream.done) {
        if (!(stream.run_id in previous)) return previous;
        const next = { ...previous };
        delete next[stream.run_id];
        return next;
      }
      if (stream.delta === null) return previous;
      const cur = previous[stream.run_id];
      const sameTurn = cur !== undefined && cur.turnIndex === stream.turn_index;
      const baseText = sameTurn ? cur.text : "";
      const baseTools = sameTurn ? cur.toolNames : [];
      const nextText =
        stream.delta.kind === "text" ? baseText + stream.delta.text : baseText;
      const nextTools =
        stream.delta.kind === "tool_call_start" &&
        stream.delta.tool_name &&
        !baseTools.includes(stream.delta.tool_name)
          ? [...baseTools, stream.delta.tool_name]
          : baseTools;
      return {
        ...previous,
        [stream.run_id]: {
          turnIndex: stream.turn_index,
          text: nextText,
          toolNames: nextTools,
        },
      };
    });
  }, []);

  const receiveEvent = useCallback(
    (event: EventView): void => {
      if (
        event.run_id !== null &&
        (event.type === "model.result" ||
          event.type === "model.error" ||
          event.type === "run.end")
      ) {
        const settledRunId = event.run_id;
        setStreamingPreviews((previous) => {
          if (!(settledRunId in previous)) return previous;
          const next = { ...previous };
          delete next[settledRunId];
          return next;
        });
      }
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
        const host: HostClient = demoMode
          ? createDemoHostClient()
          : createRealHostClient(token ?? "");
        const next = await host.getState();
        if (attempt !== connectAttemptRef.current) return;
        const nextSettingsApi = createSettingsApi(host);
        let initialSettings: SettingsView | null = null;
        try {
          initialSettings = await nextSettingsApi.getSettings();
        } catch {
          initialSettings = null;
        }
        if (attempt !== connectAttemptRef.current) return;
        hostRef.current = host;
        setMemoryApi((previous) => previous ?? createMemoryApi(host));
        setSettingsApi((previous) => previous ?? nextSettingsApi);
        setSkillsApi(createSkillsApi(host));
        setModelSettings(initialSettings);
        setState(next);
        stateRef.current = next;

        const task = pickInitialTask(next);
        composingNewTaskRef.current = task === null;
        setComposingNewTask(task === null);
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
        setMemoryApi(null);
        setSettingsApi(null);
        setSkillsApi(null);
        setModelSettings(null);
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
      onModelStream: receiveModelStream,
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

  const handleNewFreeTask = useCallback(() => {
    composingNewTaskRef.current = true;
    setComposingNewTask(true);
    setActiveProject(null);
    selectedTaskRef.current = null;
    setSelectedTaskId(null);
    selectRun(null);
  }, [selectRun]);

  const handleSelectProject = useCallback(
    (project: ProjectWorkspace) => {
      composingNewTaskRef.current = true;
      setComposingNewTask(true);
      setActiveProject(project);
      selectedTaskRef.current = null;
      setSelectedTaskId(null);
      selectRun(null);
    },
    [selectRun],
  );

  const handleAddProject = useCallback(
    (rootPath: string) => {
      const project: ProjectWorkspace = {
        root_path: rootPath,
        mode: "writable",
        name: basenameOfPath(rootPath),
      };
      setPinnedProjects((previous) => {
        if (previous.some((item) => item.root_path === rootPath)) return previous;
        return [project, ...previous];
      });
      handleSelectProject(project);
    },
    [handleSelectProject],
  );

  const handleSelectTask = useCallback(
    (taskId: string) => {
      if (!composingNewTaskRef.current && selectedTaskRef.current === taskId) return;
      const current = stateRef.current;
      if (current === null) return;
      const task = current.tasks.find((item) => item.task_id === taskId);
      if (task === undefined) return;
      composingNewTaskRef.current = false;
      setComposingNewTask(false);
      setActiveProject(null);
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
      const { execution_policy, attachments, model_profile, fallback_profiles, mode, ...taskInput } = input;
      setBusy("create-task");
      try {
        let created: TaskView | null;
        try {
          const envelope = await host.rpc<TaskEnvelope>(RPC.taskCreate, taskInput);
          created = normalizeTask(envelope.task);
        } catch (error) {
          throw new Error(humanizeHostError(error));
        }
        if (created === null) {
          throw new Error("任务创建成功，但守护进程返回的数据无法识别。");
        }
        const task = created;
        composingNewTaskRef.current = false;
        setComposingNewTask(false);
        setActiveProject(null);
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
        setRunPolicies((previous) => ({
          ...previous,
          [task.task_id]: execution_policy,
        }));
        setRunModes((previous) => ({
          ...previous,
          [task.task_id]: mode ?? "execute",
        }));
        selectRun(null);
        showToast("任务已创建，正在启动后台运行", "success");

        if (attachments?.length || model_profile) {
          unsentAttachments.current.set(task.task_id, {
            revision: task.goal_revision, files: attachments ?? [],
            selection: model_profile ? {model_profile, fallback_profiles: fallback_profiles ?? []} : {},
          });
        }
        try {
          const key = `start:${task.task_id}:${task.goal_revision}:${mode ?? "execute"}:${input.execution_policy.mode}`;
          const runEnvelope = await host.rpc<RunEnvelope>(RPC.runStart, {
            task_id: task.task_id,
            execution_policy,
            ...(attachments?.length ? { attachments } : {}),
            ...(model_profile ? { model_profile, fallback_profiles: fallback_profiles ?? [] } : {}),
            mode: mode ?? "execute",
            request_id: requestIdFor(key),
          });
          unsentAttachments.current.delete(task.task_id);
          const run = normalizeRun(runEnvelope.run);
          if (run !== null) {
            workflowRequests.current.delete(key);
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
    [refreshRun, refreshSnapshot, requireHost, requestIdFor, selectRun, showToast],
  );

  const handleStartRun = useCallback(
    async (
      taskId: string, executionPolicy: RunExecutionPolicyV1, mode: "execute" | "plan" = "execute",
      selection?: RunModelSelection,
    ): Promise<void> => {
      const host = hostRef.current;
      if (host === null) {
        throw new Error("还没有连接守护进程");
      }
      setRunPolicies((previous) => ({
        ...previous,
        [taskId]: executionPolicy,
      }));
      setRunModes((previous) => ({ ...previous, [taskId]: mode }));
      setBusy(`start-run:${taskId}`);
      const revision = stateRef.current?.tasks.find((task) => task.task_id === taskId)?.goal_revision;
      const key = `start:${taskId}:${revision}:${mode}:${executionPolicy.mode}`;
        try {
          const draft = unsentAttachments.current.get(taskId);
          const attachments = draft?.revision === revision ? draft?.files : undefined;
          const envelope = await host.rpc<RunEnvelope>(RPC.runStart, {
            task_id: taskId,
            execution_policy: executionPolicy,
            ...(draft?.revision === revision ? draft?.selection : {}),
            ...selection,
            ...(attachments?.length ? { attachments } : {}),
            mode,
            request_id: requestIdFor(key),
          });
          unsentAttachments.current.delete(taskId);
          const run = normalizeRun(envelope.run);
          if (run === null) {
            throw new HostRpcError("bad_response", "守护进程返回的运行数据无法识别");
          }
          workflowRequests.current.delete(key);
          selectRun(run);
          showToast("运行已在后台开始；关掉界面它也会继续", "success");
          await refreshSnapshot(host);
          await refreshRun(host, run.run_id);
        } catch (error) {
          setErrorBanner(humanizeHostError(error));
          throw new Error(humanizeHostError(error));
        } finally {
          setBusy(null);
        }
    },
    [refreshRun, refreshSnapshot, requestIdFor, selectRun, showToast],
  );

  const handleExecutionPolicyChange = useCallback(
    (taskId: string, policy: RunExecutionPolicyV1): void => {
      setRunPolicies((previous) => ({ ...previous, [taskId]: policy }));
    },
    [],
  );

  const handleRevise = useCallback(
    async (taskId: string, goal: string): Promise<void> => {
      const host = requireHost();
      setBusy(`revise:${taskId}`);
      try {
        let updated: TaskView | null;
        try {
          const envelope = await host.rpc<TaskEnvelope>(RPC.taskRevise, {
            task_id: taskId,
            goal,
          });
          updated = normalizeTask(envelope.task);
        } catch (error) {
          throw new Error(humanizeHostError(error));
        }
        if (updated === null) {
          throw new Error("目标已更新，但守护进程返回的数据无法识别，请刷新后再确认。");
        }
        const nextTask = updated;
        unsentAttachments.current.delete(taskId);
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

  const handleFollowUpSubmit = useCallback(
    async (
      task: TaskView,
      source: RunView,
      message: string,
      policy: RunExecutionPolicyV1,
      selection?: RunModelSelection,
      attachments?: RunAttachmentInput[],
      mode: "execute" | "plan" = "execute",
    ): Promise<void> => {
      const host = requireHost();
      try {
        const key = `follow-up:${source.run_id}:${mode}:${policy.mode}:${message}`;
        const envelope = await host.rpc<RunEnvelope>(
          source.clarification ? RPC.runAnswerClarification : RPC.runFollowUp, {
          task_id: task.task_id,
          source_run_id: source.run_id,
          mode,
          request_id: requestIdFor(key),
          message,
          execution_policy: policy,
          ...selection,
          ...(attachments?.length ? { attachments } : {}),
        });
        const run = normalizeRun(envelope.run);
        if (run === null) throw new Error("无法识别新运行，请刷新。");
        workflowRequests.current.delete(key);
        if (run.workflow?.mode === "plan" || mode === "plan") {
          setRunModes((previous) => ({ ...previous, [task.task_id]: "plan" }));
        }
        selectRun(run);
        await refreshSnapshot(host);
        await refreshRun(host, run.run_id);
      } catch (error) {
        throw new Error(humanizeHostError(error));
      }
    },
    [refreshRun, refreshSnapshot, requireHost, requestIdFor, selectRun],
  );

  const handleGuide = useCallback(async (run: RunView, text: string): Promise<void> => {
    const host = requireHost();
    try {
      await host.rpc(RPC.runGuide, { run_id: run.run_id, text });
      showToast("补充要求已提交，会在下一次模型调用前读取", "info");
    } catch (error) {
      throw new Error(humanizeHostError(error));
    }
  }, [requireHost, showToast]);
  const handleWorkflowCommand = useCallback(
    async (action: "compact" | "execute", task: TaskView, run: RunView | null): Promise<void> => {
      const host = requireHost();
      if (action === "execute" && (run?.workflow?.mode !== "plan" || run.clarification)) {
        throw new Error("请先选择一份成功完成的计划。");
      }
      const key = `${action}:${task.task_id}:${task.goal_revision}:${run?.run_id ?? ""}`;
      setBusy(`workflow:${action}`);
      try {
        const envelope = await host.rpc<RunEnvelope>(
          action === "compact" ? RPC.runCompact : RPC.planExecute,
          action === "compact"
            ? { task_id: task.task_id, ...(run ? {source_run_id: run.run_id} : {}),
                request_id: requestIdFor(key) }
            : { plan_run_id: run!.run_id, goal_revision: task.goal_revision,
                request_id: requestIdFor(key) },
        );
        const next = normalizeRun(envelope.run);
        if (next === null) throw new Error("守护进程返回的运行数据无法识别。");
        workflowRequests.current.delete(key);
        selectRun(next);
        showToast(action === "compact" ? "正在后台压缩上下文" : "已确认计划，执行已入队", "success");
        await refreshSnapshot(host);
        await refreshRun(host, next.run_id);
      } catch (error) {
        throw new Error(humanizeHostError(error));
      } finally {
        setBusy(null);
      }
    },
    [refreshRun, refreshSnapshot, requireHost, requestIdFor, selectRun, showToast],
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
            action === "pause"
              ? RPC.runPause
              : action === "resume"
                ? RPC.runResume
                : RPC.runCancel;
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
        body: "取消后，已经生效的修改不会被自动回滚。运行停止后，可以在右上角点「撤销这次修改」。",
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
          const result = await host.rpc<ApprovalDecisionResult>(
            RPC.approvalsDecide,
            params,
          );
          showToast(
            decision === "approve"
              ? result.resumed
                ? "已允许，任务继续运行"
                : "已允许"
              : "已拒绝，这次操作不会执行",
            decision === "approve" ? "success" : "warn",
          );
          await refreshSnapshot(host);
          if (selectedRunRef.current === approval.run_id) {
            await refreshRun(host, approval.run_id);
          }
        } catch (error) {
          setErrorBanner(humanizeHostError(error));
          await refreshSnapshot(host);
          if (selectedRunRef.current === approval.run_id) {
            await refreshRun(host, approval.run_id);
          }
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
        const result = await host.rpc<UndoPlanResult>(RPC.undoPlan, {
          run_id: run.run_id,
        });
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
        const result = await host.rpc<UndoApplyResult>(RPC.undoApply, {
          run_id: plan.runId,
        });
        setUndoPlan(null);
        if (
          result.refused !== undefined &&
          result.refused !== null &&
          result.refused !== ""
        ) {
          setErrorBanner(`撤销没有完全执行：${result.refused}`);
        } else {
          showToast(`已撤销 ${result.applied} 处修改`, "success");
        }
        await refreshSnapshot(host);
        if (selectedRunRef.current === plan.runId) {
          await refreshRun(host, plan.runId);
        }
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

  useEffect(() => {
    if (phase !== "ready") return;
    const handleGlobalShortcuts = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "n") {
        event.preventDefault();
        handleNewFreeTask();
      } else if ((event.metaKey || event.ctrlKey) && event.key === ",") {
        event.preventDefault();
        openSettingsAt("models");
      }
    };
    window.addEventListener("keydown", handleGlobalShortcuts);
    return () => window.removeEventListener("keydown", handleGlobalShortcuts);
  }, [phase, handleNewFreeTask, openSettingsAt]);

  const tasks = state?.tasks ?? [];
  const approvals = state?.approvals ?? [];
  const allRuns = state?.runs ?? [];
  const selectedTask = composingNewTask
    ? null
    : tasks.find((task) => task.task_id === selectedTaskId) ?? null;

  const projects = useMemo<ProjectWorkspace[]>(() => {
    const seen = new Set<string>();
    const list: ProjectWorkspace[] = [];
    for (const pinned of pinnedProjects) {
      if (!seen.has(pinned.root_path)) {
        seen.add(pinned.root_path);
        list.push(pinned);
      }
    }
    for (const task of tasks) {
      for (const ws of task.workspaces) {
        const rootPath = ws.root_path.trim();
        if (rootPath !== "" && !seen.has(rootPath)) {
          seen.add(rootPath);
          list.push({
            root_path: rootPath,
            mode: ws.mode === "readonly" ? "readonly" : "writable",
            name: basenameOfPath(rootPath),
          });
        }
      }
    }
    return list;
  }, [pinnedProjects, tasks]);

  const openApprovals = useMemo(
    () => approvals.filter((approval) => isOpenApproval(approval.state)),
    [approvals],
  );

  const taskRuns = useMemo(
    () =>
      selectedTask === null || state === null
        ? []
        : runsForTask(state, selectedTask.task_id),
    [selectedTask, state],
  );

  const selectedRun = useMemo(() => {
    if (selectedRunId === null || selectedTask === null) return null;
    if (runDetail !== null && runDetail.run_id === selectedRunId) return runDetail;
    return state?.runs.find((run) => run.run_id === selectedRunId) ?? null;
  }, [selectedRunId, selectedTask, runDetail, state]);

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
    <div className="app-shell" data-theme={effectiveTheme}>
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

      <div className="ts-workspace-layout">
        <Sidebar
          tasks={tasks}
          runs={allRuns}
          approvals={approvals}
          projects={projects}
          selectedTaskId={selectedTask?.task_id ?? null}
          composingNewTask={selectedTask === null}
          activeProjectPath={activeProject?.root_path ?? null}
          collapsed={sidebarCollapsed}
          effectiveTheme={effectiveTheme}
          daemon={daemon}
          streamStatus={streamStatus}
          streamDetail={streamDetail}
          connectionLost={connectionLost}
          demo={demoMode}
          now={now}
          onToggleCollapse={() => setSidebarCollapsed((previous) => !previous)}
          onNewFreeTask={handleNewFreeTask}
          onSelectProject={handleSelectProject}
          onAddProject={handleAddProject}
          onSelectTask={handleSelectTask}
          onOpenMemory={() => setMemoryOpen(true)}
          onToggleTheme={() =>
            updatePreferences({
              theme: effectiveTheme === "light" ? "dark" : "light",
            })
          }
          onOpenSettings={() => openSettingsAt("models")}
        />

        <main className="ts-canvas">
          {selectedTask === null ? (
            <WelcomeComposer
              activeProject={activeProject}
              projects={projects}
              defaultPolicy={preferences.defaultPolicy}
              enablePet={preferences.enablePet}
              enableParallax={preferences.enableParallax}
              reduceMotion={preferences.reduceMotion}
              previewPose={preferences.previewPose}
              connectionLost={connectionLost}
              streamStatus={streamStatus}
              streamDetail={streamDetail}
              openApprovalsCount={openApprovals.length}
              busy={busy === "create-task"}
              modelConfig={modelSettings?.model ?? null}
              settingsApi={settingsApi}
              skillsApi={skillsApi}
              onModelUpdated={handleModelUpdated}
              onJumpToPendingApproval={() => {
                const target = openApprovals[0];
                if (target !== undefined) handleSelectTask(target.task_id);
              }}
              onRefresh={handleRefresh}
              onOpenModelSettings={() => openSettingsAt("models")}
              profiles={modelSettings?.profiles}
              onSubmit={handleCreateTask}
            />
          ) : (
            <TaskWorkspaceView
              task={selectedTask}
              artifactHost={demoMode ? null : hostRef.current}
              taskRuns={taskRuns}
              allRuns={allRuns}
              approvals={approvals}
              selectedRun={selectedRun}
              selectedRunId={selectedRunId}
              entries={timelineEntries}
              events={timeline}
              hasMore={timelineHasMore}
              cost={costSummary}
              streamStatus={streamStatus}
              streamingPreview={
                selectedRun !== null ? (streamingPreviews[selectedRun.run_id] ?? null) : null
              }
              connectionLost={connectionLost}
              eventSeqHead={state?.event_seq_head ?? 0}
              busy={busy}
              now={now}
              executionPolicy={
                runPolicies[selectedTask.task_id] ?? preferences.defaultPolicy
              }
              defaultMode={runModes[selectedTask.task_id]
                ?? (taskRuns.find((run) => run.workflow?.mode !== "compact")
                  ?.workflow?.mode === "plan" ? "plan" : "execute")}
              enablePet={preferences.enablePet}
              reduceMotion={preferences.reduceMotion}
              modelConfig={modelSettings?.model ?? null}
              settingsApi={settingsApi}
              profiles={modelSettings?.profiles}
              skillsApi={skillsApi}
              onModelUpdated={handleModelUpdated}
              onSelectRun={handleSelectRun}
              onExecutionPolicyChange={handleExecutionPolicyChange}
              onStartRun={handleStartRun}
              onRevise={handleRevise}
              onFollowUpSubmit={handleFollowUpSubmit}
              onGuide={demoMode ? undefined : handleGuide}
              onWorkflowCommand={handleWorkflowCommand}
              onPause={(run) => handleControl("pause", run)}
              onResume={(run) => handleControl("resume", run)}
              onRequestCancel={requestCancel}
              onUndo={handleOpenUndo}
              onDecideApproval={handleDecide}
              onJumpToTask={handleSelectTask}
              onOpenModelSettings={() => openSettingsAt("models")}
              onRefresh={handleRefresh}
            />
          )}
        </main>
      </div>

      <SettingsDialog
        open={settingsOpen}
        api={settingsApi}
        initialTab={settingsTab}
        preferences={preferences}
        daemon={daemon}
        streamStatus={streamStatus}
        streamDetail={streamDetail}
        connectionLost={connectionLost}
        eventSeqHead={state?.event_seq_head ?? 0}
        now={now}
        onUpdatePreferences={updatePreferences}
        onOpenMemory={() => setMemoryOpen(true)}
        onRefresh={handleRefresh}
        onReconnectStream={handleRetryConnection}
        onSettingsSaved={(_summary, updated) => {
          if (updated !== undefined) {
            setModelSettings(updated);
          }
          showToast(`模型已切换为：${_summary}`, "success");
        }}
        onClose={() => setSettingsOpen(false)}
      />

      {memoryOpen && memoryApi !== null ? (
        <MemoryDialog open api={memoryApi} onClose={() => setMemoryOpen(false)} />
      ) : null}

      {confirm !== null ? (
        <Modal
          title={confirm.title}
          onClose={() => setConfirm(null)}
          footer={
            <>
              <span className="modal-foot-hint">运行状态以守护进程为准。</span>
              <span className="modal-foot-actions">
                <button
                  type="button"
                  className="btn btn-ghost"
                  onClick={() => setConfirm(null)}
                >
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
