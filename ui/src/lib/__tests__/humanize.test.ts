import { describe, expect, it } from "vitest";
import type { ApprovalView, EventView } from "../../api/types";
import {
  actionLabel,
  approvalStateLabel,
  controlLabel,
  describeApproval,
  effectStateLabel,
  eventDetail,
  humanizeEvent,
  outcomeLabel,
  reduceEventTimeline,
  runStateLabel,
  taskStatusLabel,
  workspaceModeLabel,
} from "../humanize";

function event(
  type: string,
  payload: Record<string, unknown> = {},
  overrides: Partial<EventView> = {},
): EventView {
  return {
    seq: 1,
    type,
    run_id: "run_abc123",
    task_id: "task_abc123",
    ts: 1_700_000_000_000,
    actor_kind: "core",
    payload,
    ...overrides,
  };
}

describe("枚举 → 人话", () => {
  it("运行状态全部有中文映射，未知值不直接露出枚举", () => {
    const known: Array<[string, string]> = [
      ["created", "已创建"],
      ["queued", "排队中"],
      ["running", "运行中"],
      ["waiting_approval", "等待你的批准"],
      ["waiting_user", "等待你的输入"],
      ["paused", "已暂停"],
      ["reconciling", "正在核对结果"],
      ["cancelling", "正在取消"],
      ["succeeded", "已成功"],
      ["failed", "失败"],
      ["cancelled", "已取消"],
      ["fenced", "已被新目标取代"],
    ];
    for (const [value, label] of known) expect(runStateLabel(value)).toBe(label);
    expect(runStateLabel("future_state")).toBe("状态未知");
    expect(runStateLabel("future_state")).not.toBe("future_state");
    expect(runStateLabel(null)).toBe("状态未知");
    expect(runStateLabel(undefined)).toBe("状态未知");
  });

  it("任务状态有独立的人话（succeeded = 已完成），未知有兜底", () => {
    expect(taskStatusLabel("running")).toBe("运行中");
    expect(taskStatusLabel("succeeded")).toBe("已完成");
    expect(taskStatusLabel("fenced")).toBe("已被新目标取代");
    expect(taskStatusLabel("no_such_status")).toBe("状态未知");
  });

  it("结果的三种取值 + 空值 + 未知值", () => {
    expect(outcomeLabel("succeeded")).toBe("成功");
    expect(outcomeLabel("failed")).toBe("失败");
    expect(outcomeLabel("cancelled")).toBe("已取消");
    expect(outcomeLabel(null)).toBe("尚未结束");
    expect(outcomeLabel("blocked")).toBe("结果未知");
  });

  it("partial / unknown 的效果文案是固定的两句话（M5 核心语义）", () => {
    expect(effectStateLabel("partial")).toBe("已停止，但部分修改已生效");
    expect(effectStateLabel("unknown")).toBe("无法确认是否已生效");
    expect(effectStateLabel("applied")).toBe("修改已生效");
    expect(effectStateLabel("none_applied")).toBe("没有修改生效");
    expect(effectStateLabel("none")).toBe("没有产生修改");
    expect(effectStateLabel(null)).toBe("还没有效果记录");
    expect(effectStateLabel("mystery")).toBe("效果状态未知");
    expect(effectStateLabel("mystery")).not.toBe("mystery");
  });

  it("审批状态、控制请求、目录权限都有中文兜底", () => {
    expect(approvalStateLabel("pending")).toBe("等待你的决定");
    expect(approvalStateLabel("consumed")).toBe("已生效");
    expect(approvalStateLabel("weird")).toBe("状态未知");
    expect(controlLabel("pause")).toBe("暂停");
    expect(controlLabel("cancel")).toBe("取消");
    expect(controlLabel("reboot")).toBe("运行控制");
    expect(workspaceModeLabel("readonly")).toBe("只读");
    expect(workspaceModeLabel("writable")).toBe("可写");
    expect(workspaceModeLabel("chmod")).toBe("权限未知");
  });

  it("C4 动作/规则编号映射到人话，未知编号回落到统一文案", () => {
    expect(actionLabel("c4.delete")).toBe("删除文件");
    expect(actionLabel("c4.overwrite")).toBe("覆盖已有内容");
    expect(actionLabel("c4.outside_workspace")).toBe("访问目录外的文件");
    expect(actionLabel("c4.exec")).toBe("执行命令");
    expect(actionLabel("c4.auto")).toBe("自动放行");
    expect(actionLabel("c4.approval_rejected")).toBe("你之前拒绝过这个操作");
    expect(actionLabel("c4.unknown_future_rule")).toBe("需要你确认的操作");
    expect(actionLabel("")).toBe("需要你确认的操作");
    expect(actionLabel(null)).toBe("需要你确认的操作");
  });

  it("审批展示把动作、规则与资源数量拼成一句人话", () => {
    const approval: ApprovalView = {
      approval_id: "appr_1",
      run_id: "run_1",
      task_id: "task_1",
      action: "c4.delete",
      resources: ["/a.pdf", "/b.pdf", "/c.pdf"],
      reason: "删除不可逆",
      rule_id: "c4.delete",
      state: "pending",
      created_at: 1,
      expires_at: 2,
    };
    const display = describeApproval(approval);
    expect(display.sentence).toBe("等待你批准：删除 3 个文件");
    expect(display.actionText).toBe("删除 3 个文件");
    expect(display.ruleText).toBe("删除文件");
    expect(display.resourceCount).toBe(3);
  });
});

describe("事件 → 时间线句子", () => {
  it("未知类型、空 payload、缺字段都不抛异常", () => {
    const unknown = humanizeEvent(event("future.event.type"));
    expect(unknown.sentence).toBe("发生了一个操作");
    expect(unknown.tone).toBe("neutral");

    const empty = humanizeEvent(event("tool.requested"));
    expect(empty.sentence.length).toBeGreaterThan(0);

    const malformed = humanizeEvent({
      ...event("model.result"),
      payload: null as unknown as Record<string, unknown>,
    });
    expect(malformed.sentence).toBe("模型完成了一次调用");
    expect(malformed.raw.payload).toEqual({});

    const totallyBroken = humanizeEvent({} as EventView);
    expect(totallyBroken.sentence).toBe("发生了一个操作");
  });

  it("运行生命周期事件", () => {
    expect(humanizeEvent(event("run.started")).sentence).toBe("运行开始");
    expect(humanizeEvent(event("run.start")).sentence).toBe("运行开始");
    expect(humanizeEvent(event("run.finished", { outcome: "succeeded" })).sentence).toBe(
      "运行结束：成功",
    );
    expect(humanizeEvent(event("run.finish", { outcome: "failed" })).tone).toBe("danger");
    expect(humanizeEvent(event("run.state_changed", { to: "running" })).sentence).toBe(
      "状态变为：运行中",
    );
    expect(humanizeEvent(event("run.state_changed")).sentence).toBe("运行状态已更新");
    expect(humanizeEvent(event("run.fenced")).sentence).toContain("新目标");
  });

  it("工具请求被压成一句短动作，路径只显示文件名", () => {
    const requested = humanizeEvent(
      event("tool.requested", {
        tool: "delete_file",
        path: "/Users/you/Downloads/旧账单.pdf",
      }),
    );
    expect(requested.sentence).toBe("删除 旧账单.pdf");
    expect(requested.tone).toBe("action");

    const legacyName = humanizeEvent(
      event("tool.intent", {
        tool: "write_file",
        args: { path: "/Users/you/Projects/报告.md" },
      }),
    );
    expect(legacyName.sentence).toBe("修改 报告.md");

    const command = humanizeEvent(
      event("tool.requested", { tool: "run_command", args: { command: "ls -la" } }),
    );
    expect(command.sentence).toContain("执行命令");

    const failed = humanizeEvent(
      event("tool.result", {
        tool: "delete_file",
        path: "a.pdf",
        ok: false,
        error: "文件被移动了",
      }),
    );
    expect(failed.sentence).toContain("未完成");
    expect(failed.sentence).toContain("文件被移动了");
    expect(failed.tone).toBe("danger");
  });

  it("审批事件：等待你批准 / 你批准了 / 你拒绝了", () => {
    const requested = humanizeEvent(
      event("approval.requested", {
        action: "c4.delete",
        resources: ["a.pdf", "b.pdf", "c.pdf"],
      }),
    );
    expect(requested.sentence).toBe("等待你批准：删除 3 个文件");
    expect(requested.tone).toBe("approval");

    expect(humanizeEvent(event("approval.requested")).sentence.length).toBeGreaterThan(0);
    expect(humanizeEvent(event("approval.approved")).sentence).toBe("你批准了这次操作");
    expect(humanizeEvent(event("approval.rejected")).sentence).toBe("你拒绝了这次操作");
    expect(humanizeEvent(event("approval.decided", { decision: "approve" })).sentence).toBe(
      "你批准了这次操作",
    );
    expect(humanizeEvent(event("approval.decided", { decision: "reject" })).sentence).toBe(
      "你拒绝了这次操作",
    );
  });

  it("效果事件使用固定语义：partial 必须显示“已停止，但部分修改已生效”", () => {
    const partial = humanizeEvent(event("run.effect", { effect_state: "partial" }));
    expect(partial.sentence).toContain("已停止，但部分修改已生效");
    expect(["danger", "warn"]).toContain(partial.tone);

    const unknown = humanizeEvent(event("run.effect", { effect_state: "unknown" }));
    expect(unknown.sentence).toContain("无法确认是否已生效");

    const applied = humanizeEvent(event("run.effect", { effect_state: "applied" }));
    expect(applied.sentence).toContain("修改已生效");
    expect(applied.tone).toBe("success");
  });

  it("模型事件读取 text / cost，缺字段也能阅读", () => {
    const withText = humanizeEvent(
      event("model.result", { text: "已写入 README.zh.md。", cost: 0.0123 }),
    );
    expect(withText.sentence).toBe("模型：已写入 README.zh.md。");
    const withoutText = humanizeEvent(event("model.intent", { cost: null }));
    expect(withoutText.sentence).toBe("模型正在计划下一步");
    expect(humanizeEvent(event("model.error", { message: "余额不足" })).sentence).toContain(
      "余额不足",
    );
  });

  it("worker.* 系列与部署事件有兜底句子", () => {
    expect(humanizeEvent(event("worker.heartbeat")).sentence).toBe("后台工作仍在运行");
    expect(humanizeEvent(event("worker.custom")).sentence).toBe("后台工作进程活动");
    expect(humanizeEvent(event("receipt.created", { path: "/tmp/报告.md" })).sentence).toBe(
      "已记录改动：报告.md",
    );
    expect(humanizeEvent(event("undo.completed", { applied: 2 })).sentence).toBe(
      "撤销完成（2 处修改）",
    );
  });

  it("reduceEventTimeline 按 seq 去重并升序排列，原始事件保留在 raw 里", () => {
    const entries = reduceEventTimeline([
      event("model.intent", { text: "第三步" }, { seq: 3 }),
      event("run.started", {}, { seq: 1 }),
      event("model.result", { text: "第二步" }, { seq: 2 }),
      event("run.started", {}, { seq: 1 }),
    ]);
    expect(entries.map((entry) => entry.seq)).toEqual([1, 2, 3]);
    expect(entries[1]?.raw.payload).toEqual({ text: "第二步" });
    expect(entries[0]?.type).toBe("run.started");
  });

  it("eventDetail 只把原始 type / rule_id / payload 交给折叠详情", () => {
    const detail = eventDetail(
      event("dispatch.rejected", { rule_id: "c4.delete", path: "x" }),
    );
    expect(detail.type).toBe("dispatch.rejected");
    expect(detail.ruleId).toBe("c4.delete");
    expect(detail.payload.path).toBe("x");
  });
});