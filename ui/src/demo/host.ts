/**
 * 演示模式的 Host 适配器：实现和真实客户端相同的 `HostClient` 接口。
 *
 * 它只在 `?demo=1` 时启用，数据来自 `DemoWorld` 的内存夹具；所有动作（审批、
 * 运行控制、新建任务、撤销）都被模拟成一段时间后的事件推送，用来验证界面
 * 的增量更新路径。它不发任何网络请求，也不会接触真实工作区。
 */

import type {
  ApprovalDecisionResult,
  ApprovalView,
  ApprovalsEnvelope,
  EventsResult,
  RunExecutionPolicyV1,
  RunEnvelope,
  RunsEnvelope,
  StateView,
  TaskEnvelope,
  TasksEnvelope,
  UndoApplyResult,
  UndoChange,
  UndoPlanResult,
} from "../api/types";
import type { EventStreamCallbacks, HostClient, RpcParams } from "../api/host";
import { HostRpcError } from "../api/errors";
import { DemoWorld } from "./world";

const clone = <T>(value: T): T => structuredClone(value);

function stringParam(params: RpcParams, key: string): string {
  const value = params[key];
  if (typeof value === "string" && value.trim() !== "") return value;
  throw new HostRpcError("invalid", `参数 ${key} 必须是字符串`);
}

function numberParam(params: RpcParams, key: string, fallback: number): number {
  const value = params[key];
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function isTerminal(state: string): boolean {
  return ["succeeded", "failed", "cancelled", "fenced"].includes(state);
}

function runPolicyParam(params: RpcParams): RunExecutionPolicyV1 {
  const raw = params.execution_policy;
  if (raw === undefined) {
    return { format_version: 1, mode: "effects" };
  }
  if (
    typeof raw !== "object" ||
    raw === null ||
    Object.keys(raw).sort().join(",") !== "format_version,mode"
  ) {
    throw new HostRpcError("invalid", "execution_policy 必须是精确的 V1 对象");
  }
  const record = raw as Record<string, unknown>;
  if (
    record.format_version !== 1 ||
    (record.mode !== "effects" && record.mode !== "pure_only")
  ) {
    throw new HostRpcError("invalid", "execution_policy 模式或版本不合法");
  }
  return { format_version: 1, mode: record.mode };
}

export class DemoHostClient implements HostClient {
  readonly mode = "demo" as const;
  private readonly world: DemoWorld;

  constructor(world: DemoWorld = new DemoWorld()) {
    this.world = world;
  }

  async rpc<T = unknown>(method: string, params: RpcParams = {}): Promise<T> {
    const world = this.world;
    switch (method) {
      case "state":
        return world.snapshot() as T;

      case "task.list": {
        const envelope: TasksEnvelope = { tasks: clone(world.tasks) };
        return envelope as T;
      }

      case "task.create": {
        const goal = stringParam(params, "goal");
        const rawWorkspaces = params.workspaces;
        const workspaces = Array.isArray(rawWorkspaces)
          ? rawWorkspaces.flatMap((item) => {
              if (typeof item !== "object" || item === null) return [];
              const record = item as Record<string, unknown>;
              const root =
                typeof record.root_path === "string" ? record.root_path.trim() : "";
              if (root === "") return [];
              const mode = record.mode === "writable" ? "writable" : "readonly";
              return [{ root_path: root, mode }];
            })
          : [];
        if (workspaces.length === 0) {
          throw new HostRpcError("invalid", "至少绑定一个目录");
        }
        const envelope: TaskEnvelope = { task: clone(world.createTask(goal, workspaces)) };
        return envelope as T;
      }

      case "task.revise": {
        const envelope: TaskEnvelope = {
          task: clone(world.reviseTask(stringParam(params, "task_id"), stringParam(params, "goal"))),
        };
        return envelope as T;
      }

      case "run.start": {
        const envelope: RunEnvelope = {
          run: clone(
            world.startRun(
              stringParam(params, "task_id"),
              runPolicyParam(params),
            ),
          ),
        };
        return envelope as T;
      }

      case "run.list": {
        const taskId = stringParam(params, "task_id");
        const envelope: RunsEnvelope = {
          runs: clone(world.runs.filter((run) => run.task_id === taskId)),
        };
        return envelope as T;
      }

      case "run.status": {
        const envelope: RunEnvelope = { run: clone(world.findRun(stringParam(params, "run_id"))) };
        return envelope as T;
      }

      case "run.pause": {
        const run = world.findRun(stringParam(params, "run_id"));
        if (isTerminal(run.state)) throw new HostRpcError("conflict", "这次运行已经结束");
        run.control_requested = "pause";
        world.emit("run.control", run.run_id, run.task_id, { control: "pause" });
        window.setTimeout(() => {
          const task = world.findTask(run.task_id);
          run.control_requested = null;
          world.setRunState(run, "paused");
          world.setTaskStatus(task, "paused");
        }, 500);
        const envelope: RunEnvelope = { run: clone(run) };
        return envelope as T;
      }

      case "run.resume": {
        const run = world.findRun(stringParam(params, "run_id"));
        if (isTerminal(run.state)) throw new HostRpcError("conflict", "这次运行已经结束");
        run.control_requested = null;
        world.setRunState(run, "running");
        world.setTaskStatus(world.findTask(run.task_id), "running");
        const envelope: RunEnvelope = { run: clone(run) };
        return envelope as T;
      }

      case "run.cancel": {
        const run = world.findRun(stringParam(params, "run_id"));
        if (isTerminal(run.state)) throw new HostRpcError("conflict", "这次运行已经结束");
        run.control_requested = "cancel";
        world.emit("run.control", run.run_id, run.task_id, { control: "cancel" });
        window.setTimeout(() => {
          const task = world.findTask(run.task_id);
          world.finishRun(run, task, "cancelled", "none_applied", { undoable: false });
        }, 700);
        const envelope: RunEnvelope = { run: clone(run) };
        return envelope as T;
      }

      case "approvals.list": {
        const runId = typeof params.run_id === "string" ? params.run_id : null;
        const envelope: ApprovalsEnvelope = {
          approvals: clone(
            world.approvals.filter(
              (approval) =>
                (runId === null || approval.run_id === runId) && approval.state !== "superseded",
            ),
          ),
        };
        return envelope as T;
      }

      case "approvals.decide": {
        const approval = world.findApproval(stringParam(params, "approval_id"));
        const decision = stringParam(params, "decision");
        if (approval.state !== "pending") {
          throw new HostRpcError("conflict", "这条审批已经处理过了");
        }
        if (decision === "approve") {
          world.approveContinuation(approval);
          const result: ApprovalDecisionResult = { approval: clone(approval), resumed: true };
          return result as T;
        }
        if (decision === "reject") {
          world.rejectApproval(approval);
          const result: ApprovalDecisionResult = { approval: clone(approval), resumed: false };
          return result as T;
        }
        throw new HostRpcError("invalid", "decision 只能是 approve 或 reject");
      }

      case "events.run": {
        const runId = stringParam(params, "run_id");
        const afterSeq = numberParam(params, "after_seq", 0);
        const limit = Math.max(1, numberParam(params, "limit", 200));
        const events = world.events
          .filter((event) => event.run_id === runId && event.seq > afterSeq)
          .sort((a, b) => a.seq - b.seq);
        const page = events.slice(0, limit);
        const result: EventsResult = {
          events: clone(page),
          seq_head: world.eventSeqHead,
          has_more: events.length > page.length,
        };
        return result as T;
      }

      case "undo.plan": {
        const result: UndoPlanResult = { changes: world.undoPlan(stringParam(params, "run_id")) };
        return result as T;
      }

      case "undo.apply": {
        const result: UndoApplyResult = world.undoApply(stringParam(params, "run_id"));
        return result as T;
      }

      default:
        throw new HostRpcError("not_found", `演示模式没有实现方法：${method}`);
    }
  }

  async getState(): Promise<StateView> {
    return this.world.snapshot();
  }

  async openEvents(
    sinceSeq: number,
    callbacks: EventStreamCallbacks,
    signal: AbortSignal,
  ): Promise<void> {
    callbacks.onOpen?.();
    for (const event of this.world.events.filter((item) => item.seq >= sinceSeq)) {
      callbacks.onEvent(clone(event));
    }
    await new Promise<void>((resolve) => {
      if (signal.aborted) {
        resolve();
        return;
      }
      const unsubscribe = this.world.subscribe((event) => {
        if (event.seq >= sinceSeq) callbacks.onEvent(event);
      });
      const finish = (): void => {
        signal.removeEventListener("abort", finish);
        unsubscribe();
        resolve();
      };
      signal.addEventListener("abort", finish, { once: true });
    });
  }
}

export function createDemoHostClient(): HostClient {
  return new DemoHostClient();
}

/** 只为测试/调试保留：判断一条审批的展示动作。 */
export function demoApprovalAction(approval: ApprovalView): string {
  return approval.action !== "" ? approval.action : approval.rule_id ?? "";
}

/** 撤销计划的类型别名导出，方便组件在不 import DemoWorld 的情况下复用类型。 */
export type DemoUndoChange = UndoChange;
