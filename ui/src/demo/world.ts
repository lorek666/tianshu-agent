/**
 * 演示世界：没有后端时 `?demo=1` 用的内存数据。
 *
 * 这是**虚构的示例数据**，不会写入任何事件日志，也不访问网络。它存在的唯一
 * 目的，是让评审在没有 Host 的机器上看见首屏、审批、时间线、成本、撤销这些
 * 交互长什么样。界面上会一直显示「演示模式」标记，避免被误认为真实运行。
 */

import type {
  ApprovalView,
  EventView,
  RunView,
  StateView,
  TaskView,
  UndoChange,
  WorkspaceView,
} from "../api/types";
import { HostRpcError } from "../api/errors";

const clone = <T>(value: T): T => structuredClone(value);

type EventSeed = {
  type: string;
  run_id: string | null;
  task_id: string | null;
  ts: number;
  actor_kind?: string;
  payload?: Record<string, unknown>;
};

function seed(
  type: string,
  runId: string | null,
  taskId: string | null,
  ts: number,
  payload: Record<string, unknown> = {},
  actorKind = "core",
): EventSeed {
  return { type, run_id: runId, task_id: taskId, ts, actor_kind: actorKind, payload };
}

const TASK_RUNNING = "task_4a7c1e90b2d3";
const TASK_TRANSLATED = "task_b19e3d5a6c70";
const TASK_SCREENSHOTS = "task_c8f2a4d6e803";

const RUN_WAITING = "run_7d21c4a8f9b0";
const RUN_TRANSLATED = "run_3f5b9c0d1e72";
const RUN_SCREENSHOTS = "run_e6a1b2c3d4f5";

const WORKSPACES_RUNNING: WorkspaceView[] = [
  { root_path: "~/Downloads", mode: "writable" },
  { root_path: "~/Documents/报告", mode: "writable" },
];
const WORKSPACES_TRANSLATED: WorkspaceView[] = [
  { root_path: "~/Projects/tianshu-agent", mode: "writable" },
];
const WORKSPACES_SCREENSHOTS: WorkspaceView[] = [
  { root_path: "~/Desktop/截图", mode: "writable" },
];

const BILL_PATHS = [
  "~/Downloads/2024-01 宽带账单.pdf",
  "~/Downloads/2024-03 电费账单.pdf",
  "~/Downloads/2024-07 物业费账单.pdf",
];

export class DemoWorld {
  readonly tasks: TaskView[];
  readonly runs: RunView[];
  readonly approvals: ApprovalView[];
  readonly events: EventView[];
  private readonly plans: Record<string, UndoChange[]>;
  private listenerCount = 0;
  private readonly listeners = new Set<(event: EventView) => void>();

  constructor(now: number = Date.now()) {
    const minutes = 60_000;
    const seconds = 1_000;

    this.tasks = [
      {
        task_id: TASK_RUNNING,
        goal: "整理下载目录：把 2024 年的旧账单归档到「账单归档」，再生成一份本月摘要。",
        goal_revision: 1,
        status: "waiting_approval",
        active_run_id: RUN_WAITING,
        workspaces: WORKSPACES_RUNNING,
        created_at: now - 12 * minutes,
        updated_at: now - 2 * minutes,
      },
      {
        task_id: TASK_TRANSLATED,
        goal: "把项目 README 的英文段落翻译成中文，写入 README.zh.md。",
        goal_revision: 2,
        status: "succeeded",
        active_run_id: null,
        workspaces: WORKSPACES_TRANSLATED,
        created_at: now - 80 * minutes,
        updated_at: now - 60 * minutes,
      },
      {
        task_id: TASK_SCREENSHOTS,
        goal: "清理「截图」文件夹里重复的图片（先列出来再删）。",
        goal_revision: 1,
        status: "failed",
        active_run_id: null,
        workspaces: WORKSPACES_SCREENSHOTS,
        created_at: now - 45 * minutes,
        updated_at: now - 30 * minutes,
      },
    ];

    this.runs = [
      {
        run_id: RUN_WAITING,
        task_id: TASK_RUNNING,
        state: "waiting_approval",
        outcome: null,
        effect_state: "none_applied",
        control_requested: null,
        goal_revision: 1,
        lease_epoch: 2,
        started_at: now - 150 * seconds,
        ended_at: null,
        n_applied: 0,
        n_unknown: 0,
        undoable: false,
      },
      {
        run_id: RUN_TRANSLATED,
        task_id: TASK_TRANSLATED,
        state: "succeeded",
        outcome: "succeeded",
        effect_state: "applied",
        control_requested: null,
        goal_revision: 2,
        lease_epoch: 3,
        started_at: now - 62 * minutes,
        ended_at: now - 60 * minutes,
        n_applied: 2,
        n_unknown: 0,
        undoable: true,
      },
      {
        run_id: RUN_SCREENSHOTS,
        task_id: TASK_SCREENSHOTS,
        state: "failed",
        outcome: "failed",
        effect_state: "partial",
        control_requested: null,
        goal_revision: 1,
        lease_epoch: 1,
        started_at: now - 32 * minutes,
        ended_at: now - 30 * minutes,
        n_applied: 2,
        n_unknown: 1,
        undoable: true,
      },
    ];

    this.approvals = [
      {
        approval_id: "appr_30d9f8a71c22",
        run_id: RUN_WAITING,
        task_id: TASK_RUNNING,
        action: "c4.delete",
        resources: BILL_PATHS,
        reason: "删除文件不可逆，需要你确认这三份账单确实可以删。",
        rule_id: "c4.delete",
        state: "pending",
        created_at: now - 100 * seconds,
        expires_at: now + 8 * minutes,
      },
      {
        approval_id: "appr_72b4e6c0d918",
        run_id: RUN_WAITING,
        task_id: TASK_RUNNING,
        action: "c4.overwrite",
        resources: ["~/Documents/报告/本月摘要.md"],
        reason: "目标文件已存在，覆盖会替换原有内容。",
        rule_id: "c4.overwrite",
        state: "pending",
        created_at: now - 90 * seconds,
        expires_at: now + 8 * minutes,
      },
    ];

    this.plans = {
      [RUN_TRANSLATED]: [
        {
          path: "~/Projects/tianshu-agent/README.zh.md",
          kind: "delete",
          reason: "这是本次运行新建的文件",
        },
      ],
      [RUN_SCREENSHOTS]: [
        {
          path: "~/Desktop/截图/IMG_0032 副本.jpg",
          kind: "restore",
          reason: "恢复被删除的原文件",
        },
        {
          path: "~/Desktop/截图/IMG_0041 副本.jpg",
          kind: "restore",
          reason: "恢复被删除的原文件",
        },
      ],
    };

    const seeds: EventSeed[] = [
      // 已完成的翻译运行（展示：成本累计 / applied / 可撤销）
      seed("run.started", RUN_TRANSLATED, TASK_TRANSLATED, now - 62 * minutes),
      seed("model.intent", RUN_TRANSLATED, TASK_TRANSLATED, now - 62 * minutes + 10 * seconds, {
        text: "我先读一下 README，找出英文段落。",
      }),
      seed(
        "tool.requested",
        RUN_TRANSLATED,
        TASK_TRANSLATED,
        now - 62 * minutes + 20 * seconds,
        { tool: "read_file", args: { path: "~/Projects/tianshu-agent/README.md" } },
      ),
      seed(
        "tool.result",
        RUN_TRANSLATED,
        TASK_TRANSLATED,
        now - 62 * minutes + 24 * seconds,
        { tool: "read_file", ok: true },
      ),
      seed("model.result", RUN_TRANSLATED, TASK_TRANSLATED, now - 62 * minutes + 40 * seconds, {
        text: "第 3、5、7 节是英文，我翻译后写入 README.zh.md。",
        cost: 0.0412,
      }),
      seed(
        "tool.requested",
        RUN_TRANSLATED,
        TASK_TRANSLATED,
        now - 62 * minutes + 50 * seconds,
        { tool: "write_file", path: "~/Projects/tianshu-agent/README.zh.md" },
      ),
      seed(
        "tool.result",
        RUN_TRANSLATED,
        TASK_TRANSLATED,
        now - 62 * minutes + 58 * seconds,
        { tool: "write_file", ok: true },
      ),
      seed(
        "receipt.created",
        RUN_TRANSLATED,
        TASK_TRANSLATED,
        now - 62 * minutes + 59 * seconds,
        { path: "~/Projects/tianshu-agent/README.zh.md" },
      ),
      seed("model.result", RUN_TRANSLATED, TASK_TRANSLATED, now - 61 * minutes + 10 * seconds, {
        text: "已写入 README.zh.md，翻译完成。",
        cost: 0.0286,
      }),
      seed("run.state_changed", RUN_TRANSLATED, TASK_TRANSLATED, now - 61 * minutes + 15 * seconds, {
        to: "succeeded",
      }),
      seed("run.finished", RUN_TRANSLATED, TASK_TRANSLATED, now - 61 * minutes + 16 * seconds, {
        outcome: "succeeded",
      }),
      seed("run.effect", RUN_TRANSLATED, TASK_TRANSLATED, now - 61 * minutes + 17 * seconds, {
        effect_state: "applied",
        n_applied: 2,
      }),

      // 失败但部分生效的运行（展示：partial 醒目提示 + 可撤销）
      seed("run.started", RUN_SCREENSHOTS, TASK_SCREENSHOTS, now - 32 * minutes),
      seed("model.intent", RUN_SCREENSHOTS, TASK_SCREENSHOTS, now - 32 * minutes + 8 * seconds, {
        text: "先列出截图目录里的文件，找出重复项。",
      }),
      seed(
        "tool.requested",
        RUN_SCREENSHOTS,
        TASK_SCREENSHOTS,
        now - 32 * minutes + 16 * seconds,
        { tool: "list_dir", args: { path: "~/Desktop/截图" } },
      ),
      seed(
        "tool.result",
        RUN_SCREENSHOTS,
        TASK_SCREENSHOTS,
        now - 32 * minutes + 20 * seconds,
        { tool: "list_dir", ok: true },
      ),
      seed("model.result", RUN_SCREENSHOTS, TASK_SCREENSHOTS, now - 31 * minutes + 20 * seconds, {
        text: "发现 4 张相似图片，计划删除其中 2 张重复的。",
        cost: 0.0121,
      }),
      seed(
        "tool.requested",
        RUN_SCREENSHOTS,
        TASK_SCREENSHOTS,
        now - 31 * minutes + 30 * seconds,
        { tool: "delete_file", path: "~/Desktop/截图/IMG_0032 副本.jpg" },
      ),
      seed(
        "tool.result",
        RUN_SCREENSHOTS,
        TASK_SCREENSHOTS,
        now - 31 * minutes + 35 * seconds,
        { tool: "delete_file", ok: true },
      ),
      seed(
        "receipt.created",
        RUN_SCREENSHOTS,
        TASK_SCREENSHOTS,
        now - 31 * minutes + 36 * seconds,
        { path: "~/Desktop/截图/IMG_0032 副本.jpg" },
      ),
      seed(
        "tool.requested",
        RUN_SCREENSHOTS,
        TASK_SCREENSHOTS,
        now - 31 * minutes + 45 * seconds,
        { tool: "delete_file", path: "~/Desktop/截图/IMG_0041 副本.jpg" },
      ),
      seed(
        "tool.result",
        RUN_SCREENSHOTS,
        TASK_SCREENSHOTS,
        now - 31 * minutes + 50 * seconds,
        { tool: "delete_file", ok: false, error: "目标文件在清理前被移动，删除失败。" },
      ),
      seed("model.error", RUN_SCREENSHOTS, TASK_SCREENSHOTS, now - 31 * minutes + 55 * seconds, {
        message: "模型没有给出有效的收尾方案。",
      }),
      seed("run.effect", RUN_SCREENSHOTS, TASK_SCREENSHOTS, now - 31 * minutes + 58 * seconds, {
        effect_state: "partial",
        n_applied: 2,
        n_unknown: 1,
      }),
      seed("run.state_changed", RUN_SCREENSHOTS, TASK_SCREENSHOTS, now - 30 * minutes, {
        to: "failed",
      }),
      seed("run.finished", RUN_SCREENSHOTS, TASK_SCREENSHOTS, now - 30 * minutes + 1 * seconds, {
        outcome: "failed",
      }),

      // 正在等待审批的运行（展示：待审批卡片 / 一次点击后继续）
      seed("run.started", RUN_WAITING, TASK_RUNNING, now - 150 * seconds),
      seed("model.intent", RUN_WAITING, TASK_RUNNING, now - 140 * seconds, {
        text: "我先盘点下载目录里的旧账单。",
      }),
      seed(
        "tool.requested",
        RUN_WAITING,
        TASK_RUNNING,
        now - 130 * seconds,
        { tool: "list_dir", args: { path: "~/Downloads" } },
      ),
      seed(
        "tool.result",
        RUN_WAITING,
        TASK_RUNNING,
        now - 120 * seconds,
        { tool: "list_dir", ok: true },
      ),
      seed("model.result", RUN_WAITING, TASK_RUNNING, now - 110 * seconds, {
        text: "找到 3 份 2024 年的账单 PDF，需要删除；之后更新月度摘要。",
        cost: 0.0093,
      }),
      seed(
        "tool.requested",
        RUN_WAITING,
        TASK_RUNNING,
        now - 105 * seconds,
        { tool: "delete_file", path: BILL_PATHS[0] ?? "" },
      ),
      seed("approval.requested", RUN_WAITING, TASK_RUNNING, now - 100 * seconds, {
        action: "c4.delete",
        rule_id: "c4.delete",
        resources: BILL_PATHS,
        reason: "删除文件不可逆，需要你确认。",
      }),
    ];

    let seq = 0;
    this.events = seeds.map((item) => {
      seq += 1;
      return {
        seq,
        type: item.type,
        run_id: item.run_id,
        task_id: item.task_id,
        ts: item.ts,
        actor_kind: item.actor_kind ?? "core",
        payload: item.payload ?? {},
      };
    });
  }

  get eventSeqHead(): number {
    return this.events.length;
  }

  snapshot(): StateView {
    return clone({
      tasks: this.tasks,
      runs: this.runs,
      approvals: this.approvals,
      event_seq_head: this.eventSeqHead,
      daemon: {
        version: "0.1.0-demo",
        started_at: this.events[0]?.ts ?? Date.now(),
        data_root: "~/Library/Application Support/AgentOS（演示）",
      },
    });
  }

  subscribe(listener: (event: EventView) => void): () => void {
    this.listeners.add(listener);
    this.listenerCount += 1;
    return () => {
      this.listeners.delete(listener);
      this.listenerCount = Math.max(0, this.listenerCount - 1);
    };
  }

  get subscriberCount(): number {
    return this.listenerCount;
  }

  emit(
    type: string,
    runId: string | null,
    taskId: string | null,
    payload: Record<string, unknown> = {},
    actorKind = "core",
  ): EventView {
    const event: EventView = {
      seq: this.events.length + 1,
      type,
      run_id: runId,
      task_id: taskId,
      ts: Date.now(),
      actor_kind: actorKind,
      payload,
    };
    this.events.push(event);
    for (const listener of [...this.listeners]) listener(clone(event));
    return event;
  }

  findTask(taskId: string): TaskView {
    const task = this.tasks.find((item) => item.task_id === taskId);
    if (task === undefined) throw new HostRpcError("not_found", "没有找到这个任务");
    return task;
  }

  findRun(runId: string): RunView {
    const run = this.runs.find((item) => item.run_id === runId);
    if (run === undefined) throw new HostRpcError("not_found", "没有找到这次运行");
    return run;
  }

  findApproval(approvalId: string): ApprovalView {
    const approval = this.approvals.find((item) => item.approval_id === approvalId);
    if (approval === undefined) throw new HostRpcError("not_found", "没有找到这条审批");
    return approval;
  }

  setRunState(run: RunView, state: string, payload: Record<string, unknown> = {}): void {
    run.state = state;
    this.emit("run.state_changed", run.run_id, run.task_id, { to: state, ...payload });
  }

  setTaskStatus(task: TaskView, status: string): void {
    task.status = status;
    task.updated_at = Date.now();
  }

  finishRun(
    run: RunView,
    task: TaskView,
    outcome: "succeeded" | "failed" | "cancelled",
    effectState: string,
    options: { nApplied?: number; nUnknown?: number; undoable?: boolean } = {},
  ): void {
    // 运行一旦结束，同一 Run 上还没处理的审批立即失效——不能让它们在
    // 运行结束后继续挂着，用户点了也没有意义。
    for (const other of this.approvals) {
      if (other.run_id === run.run_id && other.state === "pending") {
        other.state = "superseded";
        this.emit("approval.superseded", run.run_id, task.task_id, {
          approval_id: other.approval_id,
        });
      }
    }
    run.state = outcome === "succeeded" ? "succeeded" : outcome;
    run.outcome = outcome;
    run.effect_state = effectState;
    run.ended_at = Date.now();
    run.n_applied = options.nApplied ?? run.n_applied;
    run.n_unknown = options.nUnknown ?? run.n_unknown;
    run.undoable = options.undoable ?? false;
    run.control_requested = null;
    this.emit("run.state_changed", run.run_id, task.task_id, { to: run.state });
    this.emit("run.effect", run.run_id, task.task_id, {
      effect_state: effectState,
      n_applied: run.n_applied,
      n_unknown: run.n_unknown,
    });
    this.emit("run.finished", run.run_id, task.task_id, { outcome });
    this.setTaskStatus(task, run.state);
    task.active_run_id = null;
  }

  approveContinuation(approval: ApprovalView): void {
    const run = this.findRun(approval.run_id);
    const task = this.findTask(approval.task_id);
    approval.state = "approved";
    this.emit("approval.decided", run.run_id, task.task_id, {
      approval_id: approval.approval_id,
      decision: "approve",
    });
    this.setRunState(run, "running");
    this.setTaskStatus(task, "running");
    const resources = approval.resources.length > 0 ? approval.resources : [];
    window.setTimeout(() => {
      for (const path of resources) {
        this.emit("tool.result", run.run_id, task.task_id, { tool: "delete_file", ok: true, path });
        this.emit("receipt.created", run.run_id, task.task_id, { path });
      }
      this.emit("model.result", run.run_id, task.task_id, {
        text: "账单已删除，正在更新本月摘要。",
        cost: 0.0074,
      });
      this.emit("tool.requested", run.run_id, task.task_id, {
        tool: "write_file",
        path: "~/Documents/报告/本月摘要.md",
      });
      this.emit("tool.result", run.run_id, task.task_id, {
        tool: "write_file",
        ok: true,
        path: "~/Documents/报告/本月摘要.md",
      });
      this.emit("receipt.created", run.run_id, task.task_id, {
        path: "~/Documents/报告/本月摘要.md",
      });
      this.plans[run.run_id] = [
        ...resources.map((path): UndoChange => ({
          path,
          kind: "restore",
          reason: "恢复被删除的账单",
        })),
        {
          path: "~/Documents/报告/本月摘要.md",
          kind: "restore",
          reason: "恢复被覆盖的原内容",
        },
      ];
      this.finishRun(run, task, "succeeded", "applied", {
        nApplied: resources.length + 1,
        nUnknown: 0,
        undoable: true,
      });
    }, 1_600);
  }

  rejectApproval(approval: ApprovalView): void {
    const run = this.findRun(approval.run_id);
    const task = this.findTask(approval.task_id);
    approval.state = "rejected";
    this.emit("approval.decided", run.run_id, task.task_id, {
      approval_id: approval.approval_id,
      decision: "reject",
    });
    this.finishRun(run, task, "failed", "none_applied", { nApplied: run.n_applied, undoable: false });
  }

  undoPlan(runId: string): UndoChange[] {
    const run = this.findRun(runId);
    if (!run.undoable) throw new HostRpcError("conflict", "这次运行没有可撤销的修改");
    return clone(this.plans[runId] ?? []);
  }

  undoApply(runId: string): { applied: number; refused?: string } {
    const run = this.findRun(runId);
    const changes = this.plans[runId] ?? [];
    run.undoable = false;
    this.emit("undo.completed", runId, run.task_id, { applied: changes.length });
    return { applied: changes.length };
  }

  createTask(goal: string, workspaces: WorkspaceView[]): TaskView {
    const now = Date.now();
    const task: TaskView = {
      task_id: `task_${now.toString(16).padStart(12, "0").slice(-12)}`,
      goal,
      goal_revision: 1,
      status: "created",
      active_run_id: null,
      workspaces,
      created_at: now,
      updated_at: now,
    };
    this.tasks.unshift(task);
    this.emit("task.created", null, task.task_id, { task_id: task.task_id, goal });
    return task;
  }

  reviseTask(taskId: string, goal: string): TaskView {
    const task = this.findTask(taskId);
    const activeRunId = task.active_run_id;
    task.goal = goal;
    task.goal_revision += 1;
    task.updated_at = Date.now();
    this.emit("task.revised", activeRunId, task.task_id, {
      task_id: task.task_id,
      goal,
      goal_revision: task.goal_revision,
    });
    if (activeRunId !== null) {
      const run = this.findRun(activeRunId);
      run.state = "fenced";
      run.ended_at = Date.now();
      run.control_requested = null;
      this.emit("run.fenced", run.run_id, task.task_id, { reason: "goal_revised" });
      task.active_run_id = null;
      this.setTaskStatus(task, "created");
    }
    return task;
  }

  startRun(taskId: string): RunView {
    const task = this.findTask(taskId);
    if (task.active_run_id !== null) {
      throw new HostRpcError("conflict", "这个任务已有正在进行的运行");
    }
    const now = Date.now();
    const run: RunView = {
      run_id: `run_${now.toString(16).padStart(12, "0").slice(-12)}`,
      task_id: task.task_id,
      state: "queued",
      outcome: null,
      effect_state: "none_applied",
      control_requested: null,
      goal_revision: task.goal_revision,
      lease_epoch: task.goal_revision,
      started_at: now,
      ended_at: null,
      n_applied: 0,
      n_unknown: 0,
      undoable: false,
    };
    this.runs.unshift(run);
    task.active_run_id = run.run_id;
    this.setTaskStatus(task, "queued");
    this.emit("run.started", run.run_id, task.task_id, { goal_revision: task.goal_revision });

    window.setTimeout(() => {
      this.setRunState(run, "running");
      this.setTaskStatus(task, "running");
      this.emit("model.intent", run.run_id, task.task_id, {
        text: "先确认目标目录里有什么，再动手。",
      });
      this.emit("model.result", run.run_id, task.task_id, {
        text: "目录已确认，生成了处理计划。",
        cost: 0.0051,
      });
    }, 900);
    window.setTimeout(() => {
      const path = task.workspaces[0]?.root_path ?? "工作区";
      this.emit("tool.requested", run.run_id, task.task_id, {
        tool: "write_file",
        path: `${path}/天枢试运行记录.md`,
      });
      this.emit("tool.result", run.run_id, task.task_id, {
        tool: "write_file",
        ok: true,
        path: `${path}/天枢试运行记录.md`,
      });
      this.emit("receipt.created", run.run_id, task.task_id, {
        path: `${path}/天枢试运行记录.md`,
      });
      this.emit("model.result", run.run_id, task.task_id, {
        text: "试运行完成，已写入一条记录。",
        cost: 0.0038,
      });
      this.plans[run.run_id] = [
        {
          path: `${path}/天枢试运行记录.md`,
          kind: "delete",
          reason: "这是本次运行新建的文件",
        },
      ];
      this.finishRun(run, task, "succeeded", "applied", {
        nApplied: 1,
        nUnknown: 0,
        undoable: true,
      });
    }, 3_200);
    return run;
  }
}