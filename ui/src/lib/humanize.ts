/**
 * 机器枚举 / 原始事件 → 用户能读的中文。
 *
 * PRD 决策 D1：**UI 不展示 digest / 枚举**。可折叠的「详情」里允许出现原始
 * `type` / `rule_id` / `payload`（那是给排查用的），但主界面只出现这里的文案。
 *
 * 这条规则不是审美问题：`waiting_approval` 这种字符串对普通用户没有意义，
 * 更糟的是它会让人误以为界面在“报错”。所以：
 * - 每个枚举都有一张显式映射表，未知值有统一兜底（不白屏、不直接显示原文）。
 * - 事件句子的所有读取都是宽容的：payload 缺字段、类型不对都只是句子更含糊，
 *   不抛异常。
 */

import type { ApprovalView, EventView, TaskView, WorkspaceView } from "../api/types";
import {
  asArray,
  asRecord,
  basename,
  readArray,
  readBoolean,
  readNumber,
  readRecord,
  readString,
  truncate,
} from "./values";
import type { AnyRecord } from "./values";

export type Tone = "neutral" | "info" | "action" | "success" | "warn" | "danger" | "approval";

// ---------------------------------------------------------------- 枚举 → 人话

const RUN_STATE_LABELS: Record<string, string> = {
  created: "已创建",
  queued: "排队中",
  running: "运行中",
  waiting_approval: "等待你的批准",
  waiting_user: "等待你的输入",
  paused: "已暂停",
  reconciling: "正在核对结果",
  cancelling: "正在取消",
  succeeded: "已成功",
  failed: "失败",
  cancelled: "已取消",
  fenced: "已被新目标取代",
};

const TASK_STATUS_LABELS: Record<string, string> = {
  ...RUN_STATE_LABELS,
  // M2 的 task.status 只有 open/closed，和 run.state 不是同一套枚举：
  // open = 任务未关闭（可以改目标、开始新运行）；closed = 任务已关闭。
  // 不映射这两个值的话，界面会永远显示兜底的「状态未知」。
  open: "进行中",
  closed: "已结束",
  idle: "空闲",
  succeeded: "已完成",
  cancelled: "已取消",
};

const OUTCOME_LABELS: Record<string, string> = {
  succeeded: "成功",
  failed: "失败",
  cancelled: "已取消",
};

const EFFECT_STATE_LABELS: Record<string, string> = {
  none: "没有产生修改",
  none_applied: "没有修改生效",
  applied: "修改已生效",
  partial: "已停止，但部分修改已生效",
  unknown: "无法确认是否已生效",
};

const APPROVAL_STATE_LABELS: Record<string, string> = {
  proposed: "等待提交",
  pending: "等待你的决定",
  approved: "已允许",
  consumed: "已生效",
  rejected: "已拒绝",
  expired: "已过期",
  revoked: "已撤回",
  superseded: "已被取代",
};

const CONTROL_LABELS: Record<string, string> = {
  pause: "暂停",
  resume: "继续",
  cancel: "取消",
};

const WORKSPACE_MODE_LABELS: Record<string, string> = {
  readonly: "只读",
  writable: "可写",
};

const ACTOR_LABELS: Record<string, string> = {
  core: "内核",
  harness: "调度器",
  tool: "工具",
  human: "你",
  system: "系统",
};

/**
 * C4 规则编号 → 用户能读的动作/原因。
 *
 * 契约里的六个是界面必显的；其余已登记编号来自 `schemas/c4/rules.json`，
 * 补上它们是为了让「为什么被拒」不显示成 `c4.no_approval` 这样的编号。
 * **未登记的编号一律回落到「需要你确认的操作」**（契约要求）。
 */
const ACTION_LABELS: Record<string, string> = {
  "c4.delete": "删除文件",
  "c4.overwrite": "覆盖已有内容",
  "c4.outside_workspace": "访问目录外的文件",
  "c4.exec": "执行命令",
  "c4.auto": "自动放行",
  "c4.approval_rejected": "你之前拒绝过这个操作",
  // ---- 以下为仓库 C4 规则表里其余用户可见原因（补充映射，不影响兜底） ----
  "c4.no_approval": "缺少可用批准",
  "c4.approval_expired": "批准已过期",
  "c4.approval_mismatch": "批准与本次操作不一致",
  "c4.approval_run_mismatch": "批准属于另一次运行",
  "c4.approval_state": "批准已不可用",
  "c4.create_over_existing": "目标文件已存在",
  "c4.multi_resource": "一次涉及多个文件",
  "c4.no_binding": "任务没有绑定目录",
  "c4.no_resource": "操作没有指定文件",
  "c4.readonly_binding": "目录是只读的",
  "c4.outside_workspace_unsupported": "暂不支持访问目录外的文件",
  "c4.exec_not_configured": "还没有配置命令执行",
  "c4.exec_policy_denied": "执行策略不允许这条命令",
  "c4.exec_unsupported": "当前环境不支持执行命令",
  "c4.unsupported": "不支持这种操作",
  "c4.unhandled": "无法判断这种操作",
  "dispatch.approval_revoked": "批准在派发前被撤回",
  "dispatch.approval_unusable": "批准已不可用",
  "dispatch.claim_lost": "后台执行权已丢失",
  "dispatch.lease_lost": "运行租约已过期",
  "dispatch.not_admitted": "操作没有被放行",
  "dispatch.not_running": "运行已不在运行中",
  "dispatch.refused": "操作被拒绝",
  "dispatch.run_fenced": "运行已被新目标取代",
  "dispatch.stale_revision": "任务目标已经更新",
  "dispatch.target_is_directory": "目标是文件夹，不是文件",
  "dispatch.workspace_blocked": "目录访问被拦截",
};

function lookup(table: Record<string, string>, value: string | null | undefined, fallback: string): string {
  const key = typeof value === "string" ? value.trim() : "";
  if (key === "") return fallback;
  return table[key] ?? fallback;
}

export function runStateLabel(state: string | null | undefined): string {
  return lookup(RUN_STATE_LABELS, state, "状态未知");
}

export function taskStatusLabel(status: string | null | undefined): string {
  return lookup(TASK_STATUS_LABELS, status, "状态未知");
}

export function outcomeLabel(outcome: string | null | undefined): string {
  if (outcome === null || outcome === undefined || outcome.trim() === "") return "尚未结束";
  return lookup(OUTCOME_LABELS, outcome, "结果未知");
}

/** `partial` / `unknown` 是 M5 的核心语义，文案固定成这两句，不要改。 */
export function effectStateLabel(effectState: string | null | undefined): string {
  if (effectState === null || effectState === undefined || effectState.trim() === "") {
    return "还没有效果记录";
  }
  return lookup(EFFECT_STATE_LABELS, effectState, "效果状态未知");
}

export function approvalStateLabel(state: string | null | undefined): string {
  return lookup(APPROVAL_STATE_LABELS, state, "状态未知");
}

export function controlLabel(control: string | null | undefined): string {
  return lookup(CONTROL_LABELS, control, "运行控制");
}

export function workspaceModeLabel(mode: string | null | undefined): string {
  return lookup(WORKSPACE_MODE_LABELS, mode, "权限未知");
}

export function actorLabel(kind: string | null | undefined): string {
  return lookup(ACTOR_LABELS, kind, "系统");
}

export function actionLabel(action: string | null | undefined): string {
  return lookup(ACTION_LABELS, action, "需要你确认的操作");
}

export function isKnownActionId(action: string | null | undefined): boolean {
  const key = typeof action === "string" ? action.trim() : "";
  return key !== "" && ACTION_LABELS[key] !== undefined;
}

/** 审批卡片/事件句子里的动作短语；多个文件时用“删除 3 个文件”这种人话。 */
export function approvalActionPhrase(action: string | null | undefined, resourceCount: number): string {
  const key = typeof action === "string" ? action.trim() : "";
  if (key === "c4.delete" && resourceCount > 1) return `删除 ${resourceCount} 个文件`;
  if (key === "c4.outside_workspace" && resourceCount > 1) {
    return `访问 ${resourceCount} 个目录外的文件`;
  }
  if (key !== "" && ACTION_LABELS[key] === undefined) {
    return resourceCount > 1 ? `确认 ${resourceCount} 项操作` : "需要你确认的操作";
  }
  return actionLabel(key);
}

// ---------------------------------------------------------------- 效果/状态语义

export function runStateTone(state: string | null | undefined): Tone {
  switch (state) {
    case "succeeded":
      return "success";
    case "failed":
      return "danger";
    case "cancelled":
    case "fenced":
    case "paused":
      return "warn";
    case "waiting_approval":
    case "waiting_user":
      return "approval";
    case "running":
      return "action";
    case "reconciling":
    case "cancelling":
      return "warn";
    default:
      return "neutral";
  }
}

export function outcomeTone(outcome: string | null | undefined): Tone {
  switch (outcome) {
    case "succeeded":
      return "success";
    case "failed":
      return "danger";
    case "cancelled":
      return "warn";
    default:
      return "neutral";
  }
}

export function effectStateTone(effectState: string | null | undefined): Tone {
  switch (effectState) {
    case "applied":
      return "success";
    case "partial":
      return "danger";
    case "unknown":
      return "warn";
    case "none_applied":
      return "neutral";
    default:
      return "neutral";
  }
}

export function approvalStateTone(state: string | null | undefined): Tone {
  switch (state) {
    case "pending":
    case "proposed":
      return "approval";
    case "approved":
    case "consumed":
      return "success";
    case "rejected":
    case "expired":
    case "revoked":
    case "superseded":
      return "warn";
    default:
      return "neutral";
  }
}

/** 审批是否还需要用户处理。未知状态按“需要处理”处理：宁可多显示一张卡片。 */
export function isOpenApproval(state: string | null | undefined): boolean {
  const key = typeof state === "string" ? state.trim() : "";
  return !["approved", "consumed", "rejected", "expired", "revoked", "superseded"].includes(key);
}

/** 运行是否已经结束（终态）。未知状态按“未结束”处理，避免误禁用控制按钮。 */
export function isTerminalRunState(state: string | null | undefined): boolean {
  return ["succeeded", "failed", "cancelled", "fenced"].includes(
    typeof state === "string" ? state.trim() : "",
  );
}

// ---------------------------------------------------------------- 审批 / 任务展示

export interface ApprovalDisplay {
  actionText: string;
  ruleText: string;
  resources: string[];
  resourceCount: number;
  sentence: string;
}

export function describeApproval(approval: ApprovalView): ApprovalDisplay {
  const action = approval.action ?? approval.rule_id ?? null;
  const resources = Array.isArray(approval.resources) ? approval.resources : [];
  return {
    actionText: approvalActionPhrase(action, resources.length),
    ruleText: actionLabel(approval.rule_id),
    resources,
    resourceCount: resources.length,
    sentence: `等待你批准：${approvalActionPhrase(action, resources.length)}`,
  };
}

export function taskGoalSummary(task: TaskView, max = 80): string {
  return truncate(task.goal || "（没有填写目标）", max);
}

export function workspaceSummary(workspaces: WorkspaceView[]): string {
  if (workspaces.length === 0) return "没有绑定目录";
  return workspaces
    .map((workspace) => `${workspace.root_path}（${workspaceModeLabel(workspace.mode)}）`)
    .join("、");
}

// ---------------------------------------------------------------- 隔离（F2）

/**
 * 隔离视图 → 人话。**优先级是硬约定**（PRD §8.1）：
 * 1. `contradicted`（自述与实测冲突）
 * 2. `unverified` 且自述不是 none（后端自称隔离但没测）
 * 3. `level === none` 但有部分边界已实测
 * 4. `level === none`
 * 5. 其它等级
 * 顺序反了会把更严重的状态盖掉（例如把"自述 app_container 未实测"说成
 * 受信任执行，或反过来把"没有证据"说成"已隔离"）。
 */
export const ISOLATION_LEVEL_LABELS: Record<string, string> = {
  none: "未隔离",
  path_only: "仅路径控制",
  process_tree: "路径与进程树控制",
  app_container: "应用容器",
  container: "容器",
};

const ISOLATION_BOUNDARY_LABELS: Record<string, string> = {
  files_authority: "文件与权威数据",
  network: "网络",
  credentials: "凭据",
  process_tree: "进程树",
};

function isolationLevelLabel(level: string | null | undefined): string {
  if (level === null || level === undefined || level === "") return "未知";
  return ISOLATION_LEVEL_LABELS[level] ?? "未知";
}

function isolationBoundaryLabel(boundary: string): string {
  return ISOLATION_BOUNDARY_LABELS[boundary] ?? boundary;
}

export function isolationSentence(
  value: unknown,
): { text: string; tone: Tone } | null {
  const record = asRecord(value);
  if (Object.keys(record).length === 0) return null;
  const level = readString(record, "level") ?? "";
  const declared = readString(record, "declared") ?? "none";
  const measured = readString(record, "measured") ?? level;
  const unverified = readBoolean(record, "unverified") ?? false;
  const contradicted = readBoolean(record, "contradicted") ?? false;
  const enforced = (readArray(record, "enforced_boundaries") ?? []).filter(
    (item): item is string => typeof item === "string",
  );

  if (contradicted) {
    return {
      text: `后端自述「${isolationLevelLabel(declared)}」，实测「${isolationLevelLabel(measured)}」`,
      tone: "warn",
    };
  }
  if (unverified && declared !== "none") {
    return {
      text: `后端自述「${isolationLevelLabel(declared)}」，未实测`,
      tone: "warn",
    };
  }
  if (level === "" || !(level in ISOLATION_LEVEL_LABELS)) {
    return { text: "隔离情况未知（视图字段不完整）", tone: "warn" };
  }
  if (level === "none") {
    if (enforced.length > 0) {
      const names = enforced.map((item) => `「${isolationBoundaryLabel(item)}」`).join("、");
      // "已实测的边界"容易被读成"部分隔离 = 更安全"。先给整体结论，再说
      // 哪一条边界有证据——PRD §8.1 要的是真实权限范围，不是分数。
      return {
        text: `受信任执行：整体未隔离；仅${names}边界有实测证据`,
        tone: "warn",
      };
    }
    return { text: "受信任执行：未隔离", tone: "warn" };
  }
  return { text: `隔离：${isolationLevelLabel(level)}`, tone: "info" };
}

function isolationOf(payload: AnyRecord | null | undefined): unknown {
  if (payload === null || payload === undefined) return null;
  const direct = payload["isolation"];
  if (direct !== null && direct !== undefined) return direct;
  const output = readRecord(payload, "output");
  return output === null ? null : output["isolation"];
}

export function isolationSuffix(payload: AnyRecord | null | undefined): string {
  const view = isolationSentence(isolationOf(payload));
  return view === null ? "" : `（${view.text}）`;
}

// ---------------------------------------------------------------- 工具动作 → 短语

const TOOL_VERBS: Record<string, string> = {
  write_file: "修改",
  write_text: "修改",
  edit_file: "修改",
  apply_patch: "修改",
  str_replace: "修改",
  create_file: "新建",
  create_text: "新建",
  delete_file: "删除",
  remove_file: "删除",
  delete: "删除",
  rm: "删除",
  read_file: "读取",
  read_text: "读取",
  list_dir: "查看",
  list_files: "查看",
  glob: "查看",
  run_command: "执行命令",
  exec: "执行命令",
  shell: "执行命令",
  mkdir: "新建目录",
  move: "移动",
  rename: "重命名",
};

function toolNameOf(payload: Record<string, unknown>): string | null {
  const direct = readString(payload, "tool", "tool_id", "tool_name", "name", "kind");
  if (direct !== null) return direct;
  const nested = asRecord(payload.tool);
  return readString(nested, "name", "tool_id", "id");
}

function resourcesOf(payload: Record<string, unknown>): string[] {
  const collected: string[] = [];
  for (const item of asArray(payload.resources)) {
    if (typeof item === "string" && item.trim() !== "") {
      collected.push(item);
    } else {
      const record = asRecord(item);
      const path = readString(record, "path", "file_path", "target", "name");
      if (path !== null) collected.push(path);
    }
  }
  const single = readString(payload, "path", "file_path", "target");
  if (single !== null) collected.push(single);
  const list = readArray(payload, "paths", "files");
  if (list !== null) {
    for (const item of list) {
      if (typeof item === "string" && item.trim() !== "") collected.push(item);
    }
  }
  const args = asRecord(payload.args ?? payload.arguments ?? payload.input ?? payload.params);
  const argPath = readString(args, "path", "file_path", "target");
  if (argPath !== null) collected.push(argPath);
  const argPaths = readArray(args, "paths", "files");
  if (argPaths !== null) {
    for (const item of argPaths) {
      if (typeof item === "string" && item.trim() !== "") collected.push(item);
    }
  }
  return [...new Set(collected)];
}

function commandOf(payload: Record<string, unknown>): string | null {
  const direct = readString(payload, "command", "cmd", "script");
  if (direct !== null) return direct;
  const args = asRecord(payload.args ?? payload.arguments ?? payload.input ?? payload.params);
  return readString(args, "command", "cmd", "script");
}

/** 把一条工具事件压成一句短动作，例如「删除 旧账单.pdf」「执行命令「ls -la」」。 */
export function describeToolAction(payload: Record<string, unknown>): string | null {
  const tool = toolNameOf(payload);
  const paths = resourcesOf(payload);
  const command = commandOf(payload);
  const verb = tool !== null ? TOOL_VERBS[tool] ?? "操作" : "操作";

  if (command !== null) {
    return `执行命令「${truncate(command, 40)}」`;
  }
  if (paths.length > 1) {
    return tool === "delete_file" || tool === "delete" || tool === "remove_file"
      ? `删除 ${paths.length} 个文件`
      : `${verb} ${paths.length} 个文件`;
  }
  const path = paths[0];
  if (path !== undefined) {
    return `${verb} ${basename(path) || path}`;
  }
  const text = readString(payload, "text", "summary", "message");
  if (text !== null) return truncate(text, 100);
  if (tool !== null) return verb;
  return null;
}

function isFailureResult(payload: Record<string, unknown>): boolean {
  if (readBoolean(payload, "ok", "success") === false) return true;
  const status = readString(payload, "status", "result", "outcome");
  if (status !== null && ["failed", "error", "denied", "rejected", "cancelled"].includes(status)) {
    return true;
  }
  return readString(payload, "error", "error_message") !== null;
}

// ---------------------------------------------------------------- 事件 → 时间线句子

export interface TimelineEntry {
  seq: number;
  ts: number;
  type: string;
  sentence: string;
  tone: Tone;
  /** 原始事件留在折叠详情里，排查时能对上事件日志。 */
  raw: EventView;
}

function toTimelineEntry(event: EventView, sentence: string, tone: Tone): TimelineEntry {
  const seq = typeof event?.seq === "number" && Number.isFinite(event.seq) ? event.seq : 0;
  const ts = typeof event?.ts === "number" && Number.isFinite(event.ts) ? event.ts : 0;
  const type = typeof event?.type === "string" ? event.type : "";
  return {
    seq,
    ts,
    type,
    sentence,
    tone,
    raw: {
      seq,
      ts,
      type,
      run_id: typeof event?.run_id === "string" ? event.run_id : null,
      task_id: typeof event?.task_id === "string" ? event.task_id : null,
      actor_kind: typeof event?.actor_kind === "string" ? event.actor_kind : "system",
      payload: asRecord(event?.payload),
    },
  };
}

function ensureSentence(text: string | null, fallback: string): string {
  return text !== null && text.trim() !== "" ? text : fallback;
}

/**
 * 一条事件 = 一句中文。
 *
 * 兼容两套事件名：M5 Host 对 UI 暴露的名字（`run.started` / `tool.requested` /
 * `approval.approved` ...）和仓库 C3 日志里的历史名字（`run.start` /
 * `tool.intent` / `approval.decided` ...）。两套都落到同一句人话，未知类型
 * 统一兜底——事件契约以后加类型时，界面最多变得含糊，不会崩。
 */
export function humanizeEvent(event: EventView): TimelineEntry {
  const payload = asRecord(event?.payload);
  const type = typeof event?.type === "string" ? event.type : "";
  const text = readString(payload, "text", "summary", "message");

  switch (type) {
    case "run.started":
    case "run.start":
      return toTimelineEntry(event, "运行开始", "info");

    case "run.finished":
    case "run.finish":
    case "run.end": {
      const outcome = readString(payload, "outcome", "result");
      return outcome !== null
        ? toTimelineEntry(event, `运行结束：${outcomeLabel(outcome)}`, outcomeTone(outcome))
        : toTimelineEntry(event, "运行结束", "info");
    }

    case "run.state_changed":
    case "run.state": {
      const to = readString(payload, "to", "state", "status");
      if (to === null) return toTimelineEntry(event, "运行状态已更新", "info");
      return toTimelineEntry(event, `状态变为：${runStateLabel(to)}`, runStateTone(to));
    }

    case "run.control": {
      const control = readString(payload, "control", "request", "control_requested", "action");
      return toTimelineEntry(
        event,
        control !== null ? `收到${controlLabel(control)}请求` : "收到运行控制请求",
        "info",
      );
    }

    case "run.effect": {
      const effect = readString(payload, "effect_state", "effect", "state");
      return toTimelineEntry(
        event,
        effect !== null ? `效果：${effectStateLabel(effect)}` : "运行效果已更新",
        effectStateTone(effect),
      );
    }

    case "run.fenced":
      return toTimelineEntry(event, "这次运行已被新目标取代并停止", "warn");

    case "run.revised":
    case "task.revised":
      return toTimelineEntry(event, "任务目标已更新", "warn");

    case "run.cursor":
    case "run.model_turn":
      return toTimelineEntry(event, "运行进度已保存", "neutral");

    case "model.intent":
      return toTimelineEntry(
        event,
        text !== null ? `模型：${truncate(text, 140)}` : "模型正在计划下一步",
        "info",
      );

    case "model.result": {
      const sentence = text !== null ? `模型：${truncate(text, 140)}` : "模型完成了一次调用";
      return toTimelineEntry(event, sentence, "info");
    }

    case "model.error": {
      const reason = readString(payload, "message", "error", "reason");
      return toTimelineEntry(
        event,
        reason !== null ? `模型调用失败：${truncate(reason, 100)}` : "模型调用失败",
        "danger",
      );
    }

    case "tool.requested":
    case "tool.intent": {
      const action = describeToolAction(payload);
      return toTimelineEntry(event, ensureSentence(action, "准备执行一个操作"), "action");
    }

    case "tool.result": {
      const action = describeToolAction(payload) ?? "操作";
      const isolation = isolationSuffix(payload);
      if (isFailureResult(payload)) {
        const reason = readString(payload, "error", "error_message", "reason", "message");
        const detail = reason !== null ? `（${truncate(reason, 80)}）` : "";
        return toTimelineEntry(
          event,
          `未完成：${action}${detail}${isolation}`,
          "danger",
        );
      }
      return toTimelineEntry(event, `已完成：${action}${isolation}`, "success");
    }

    case "tool.not_dispatched":
      return toTimelineEntry(event, "有一个操作没有执行", "warn");

    case "dispatch.requested": {
      const action = readString(payload, "action", "rule_id");
      const count = resourcesOf(payload).length;
      return toTimelineEntry(
        event,
        `申请放行：${approvalActionPhrase(action, count)}${isolationSuffix(payload)}`,
        "approval",
      );
    }

    case "dispatch.admitted": {
      const action = readString(payload, "action", "rule_id");
      const count = resourcesOf(payload).length;
      return toTimelineEntry(event, `已放行：${approvalActionPhrase(action, count)}`, "success");
    }

    case "dispatch.rejected":
    case "policy.denied": {
      const rule = readString(payload, "rule_id", "action");
      const reason = isKnownActionId(rule) ? actionLabel(rule) : null;
      return toTimelineEntry(
        event,
        reason !== null ? `未获放行：${reason}` : "操作被安全策略拦截",
        "danger",
      );
    }

    case "receipt.created":
    case "receipt": {
      const path =
        readString(payload, "path", "file_path", "target") ?? resourcesOf(payload)[0] ?? null;
      return toTimelineEntry(
        event,
        path !== null ? `已记录改动：${basename(path) || path}` : "已记录一次改动",
        "success",
      );
    }

    case "execution.started":
      return toTimelineEntry(event, "开始执行已批准的操作", "action");

    case "grant.issued":
      return toTimelineEntry(event, "已签发一次性权限", "info");

    case "grant.consumed":
      return toTimelineEntry(event, "一次性权限已使用", "neutral");

    case "job.enqueued":
      return toTimelineEntry(event, "已排入后台队列", "neutral");

    case "job.claimed":
      return toTimelineEntry(event, "后台开始处理", "info");

    case "job.requeued":
      return toTimelineEntry(event, "后台作业重新排队", "warn");

    case "job.parked":
      return toTimelineEntry(event, "后台作业暂时搁置", "warn");

    case "job.finished":
      return toTimelineEntry(event, "后台作业完成", "success");

    case "undo.completed": {
      const applied = readNumber(payload, "applied", "count");
      return toTimelineEntry(
        event,
        applied !== null ? `撤销完成（${applied} 处修改）` : "撤销完成",
        "success",
      );
    }

    case "undo.failed": {
      const reason = readString(payload, "reason", "error", "message");
      return toTimelineEntry(
        event,
        reason !== null ? `撤销失败：${truncate(reason, 100)}` : "撤销失败",
        "danger",
      );
    }

    case "recovery.blocked": {
      const reason = readString(payload, "reason", "message");
      return toTimelineEntry(
        event,
        reason !== null ? `恢复受阻：${truncate(reason, 100)}` : "恢复受阻",
        "warn",
      );
    }

    case "recovery.unblocked":
      return toTimelineEntry(event, "恢复流程已解除阻塞", "success");

    case "task.created":
      return toTimelineEntry(event, "任务已创建", "info");

    case "approval.requested": {
      const action = readString(payload, "action", "rule_id");
      const count = resourcesOf(payload).length;
      const isolation = isolationSuffix(payload);
      if (action === null && count === 0) {
        return toTimelineEntry(event, `等待你批准一次操作${isolation}`, "approval");
      }
      return toTimelineEntry(
        event,
        `等待你批准：${approvalActionPhrase(action, count)}${isolation}`,
        "approval",
      );
    }

    case "approval.approved":
      return toTimelineEntry(event, "你批准了这次操作", "success");

    case "approval.rejected":
      return toTimelineEntry(event, "你拒绝了这次操作", "warn");

    case "approval.decided": {
      const decision = readString(payload, "decision", "state", "result");
      if (decision !== null && decision.startsWith("approv")) {
        return toTimelineEntry(event, "你批准了这次操作", "success");
      }
      if (decision !== null && decision.startsWith("reject")) {
        return toTimelineEntry(event, "你拒绝了这次操作", "warn");
      }
      return toTimelineEntry(event, "审批已处理", "info");
    }

    case "approval.expired":
      return toTimelineEntry(event, "审批已过期，操作没有执行", "warn");

    case "approval.consumed":
      return toTimelineEntry(event, "审批已生效", "neutral");

    case "approval.revoked":
      return toTimelineEntry(event, "审批已撤回", "warn");

    case "approval.superseded":
      return toTimelineEntry(event, "审批已失效（目标已更新）", "warn");

    default: {
      if (type.startsWith("worker.")) {
        if (text !== null) return toTimelineEntry(event, truncate(text, 120), "info");
        const suffix = type.slice("worker.".length);
        const workerSentences: Record<string, string> = {
          started: "后台工作开始",
          finished: "后台工作结束",
          failed: "后台工作失败",
          heartbeat: "后台工作仍在运行",
        };
        const sentence = workerSentences[suffix] ?? "后台工作进程活动";
        return toTimelineEntry(event, sentence, suffix === "failed" ? "danger" : "info");
      }
      return toTimelineEntry(event, "发生了一个操作", "neutral");
    }
  }
}

/** 事件列表 → 时间线（去重、按 seq 升序、逐条人话化）。 */
export function reduceEventTimeline(events: EventView[]): TimelineEntry[] {
  const seen = new Set<number>();
  const entries: TimelineEntry[] = [];
  for (const event of Array.isArray(events) ? events : []) {
    if (typeof event?.seq !== "number" || !Number.isFinite(event.seq)) continue;
    if (seen.has(event.seq)) continue;
    seen.add(event.seq);
    entries.push(humanizeEvent(event));
  }
  entries.sort((a, b) => a.seq - b.seq);
  return entries;
}

/** 供组件展示的原始详情（布尔结果只用于“详情”面板，不直接显示给普通用户）。 */
export function eventDetail(event: EventView): { type: string; ruleId: string | null; payload: Record<string, unknown> } {
  const payload = asRecord(event?.payload);
  return {
    type: typeof event?.type === "string" ? event.type : "",
    ruleId: readString(payload, "rule_id"),
    payload,
  };
}