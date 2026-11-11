import { describe, expect, it } from "vitest";
import {
  normalizeApproval,
  normalizeEvent,
  normalizeRun,
  normalizeState,
  normalizeTask,
  normalizeUndoChanges,
} from "../normalize";
import { daemonVersionOf } from "../host";

describe("Host 返回值的宽容读取", () => {
  it("state 缺字段 / 类型不对时给安全空值，而不是崩", () => {
    const state = normalizeState({
      tasks: [{ task_id: "task_1", goal: "做事" }, "not-a-task"],
      runs: [{ run_id: "run_1", task_id: "task_1" }],
      approvals: [{ approval_id: "appr_1", resources: [{ path: "/a" }, "b", null] }],
      event_seq_head: "not-a-number",
      daemon: null,
    });
    expect(state.tasks).toHaveLength(1);
    expect(state.tasks[0]?.workspaces).toEqual([]);
    expect(state.tasks[0]?.status).toBe("unknown");
    expect(state.runs[0]?.state).toBe("unknown");
    expect(state.runs[0]?.undoable).toBe(false);
    expect(state.approvals[0]?.resources).toEqual(["/a", "b"]);
    expect(state.event_seq_head).toBe(0);
    expect(state.daemon.version).toBeNull();
  });

  it("守护进程版本优先读 daemon_version（Host 实际字段），兼容旧 version", () => {
    expect(normalizeState({ daemon: { daemon_version: "0.1.0" } }).daemon.version).toBe("0.1.0");
    expect(normalizeState({ daemon: { version: "9.9.9" } }).daemon.version).toBe("9.9.9");
    expect(
      normalizeState({ daemon: { daemon_version: "0.1.0", version: "9.9.9" } }).daemon.version,
    ).toBe("0.1.0");
    expect(daemonVersionOf(normalizeState({ daemon: { daemon_version: "0.1.0" } }))).toBe("0.1.0");
    expect(daemonVersionOf(null)).toBeNull();
  });

  it("事件缺 seq/type 直接丢弃；payload 是 JSON 字符串时尝试解析", () => {
    expect(normalizeEvent({ seq: 1 })).toBeNull();
    expect(normalizeEvent({ type: "run.started" })).toBeNull();
    const event = normalizeEvent({
      seq: 2,
      type: "tool.result",
      payload: '{"path":"/a"}',
    });
    expect(event?.payload).toEqual({ path: "/a" });
  });

  it("任务/运行/审批的枚举字段保留未知值，由人话层兜底", () => {
    expect(normalizeTask({ task_id: "t", status: "brand_new" })?.status).toBe("brand_new");
    expect(normalizeRun({ run_id: "r", task_id: "t", state: "brand_new" })?.state).toBe(
      "brand_new",
    );
    expect(normalizeApproval({ approval_id: "a", state: "brand_new" })?.state).toBe("brand_new");
  });

  it("撤销计划兼容对象与裸数组，丢弃没有 path 的条目", () => {
    expect(
      normalizeUndoChanges({
        changes: [
          { path: "/a", kind: "restore", reason: "恢复" },
          { kind: "delete" },
        ],
      }),
    ).toEqual([{ path: "/a", kind: "restore", reason: "恢复" }]);
    expect(normalizeUndoChanges([{ path: "/b", kind: "delete" }])).toEqual([
      { path: "/b", kind: "delete", reason: null },
    ]);
  });
});

describe("normalizeApproval 的隔离视图", () => {
  it("字段存在但读不懂时保留为未知视图，不隐藏成 null", () => {
    const approval = normalizeApproval({
      approval_id: "appr_1",
      action: "c4.exec",
      state: "pending",
      isolation: { declared: "none", unverified: true },
    });
    expect(approval).not.toBeNull();
    expect(approval?.isolation).not.toBeNull();
    expect(approval?.isolation?.level).toBe("");
  });

  it("完全没有 isolation 字段时是 null（非 exec / 旧事件）", () => {
    const approval = normalizeApproval({
      approval_id: "appr_1",
      action: "c4.delete",
      state: "pending",
    });
    expect(approval?.isolation).toBeNull();
  });
});
