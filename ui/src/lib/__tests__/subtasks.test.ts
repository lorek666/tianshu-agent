import { describe, expect, it } from "vitest";
import type { EventView, RunView } from "../../api/types";
import { buildSubtaskViews } from "../subtasks";

function event(
  type: string,
  payload: Record<string, unknown>,
  seq: number,
): EventView {
  return {
    seq,
    type,
    run_id: "run_parent",
    task_id: "task_parent",
    ts: seq * 1000,
    actor_kind: "core",
    payload,
  };
}

function run(runId: string, state: string): RunView {
  return {
    run_id: runId,
    task_id: "task_parent",
    state,
    outcome: null,
    effect_state: "none_applied",
    control_requested: null,
    goal_revision: 1,
    lease_epoch: 1,
    started_at: 1,
    ended_at: null,
    n_applied: 0,
    n_unknown: 0,
    undoable: false,
  };
}

const SPAWNED = {
  subtask_id: "st_read_1",
  child_task_id: "task_child_1",
  child_run_id: "run_child_1",
  job_id: "job_child_1",
  goal_digest: "a".repeat(64),
  limits: { max_turns: 2, max_tokens: 3_000, deadline_ms: 300_000 },
};

describe("子任务视图：合并与状态（J1 / J2）", () => {
  it("spawned + finished 合并出终态与用量", () => {
    const rows = buildSubtaskViews(
      [
        event("subtask.spawned", SPAWNED, 1),
        event(
          "subtask.finished",
          { subtask_id: "st_read_1", state: "succeeded", usage: { turns: 2, tokens: 1_842 } },
          5,
        ),
      ],
      [run("run_child_1", "succeeded")],
    );
    expect(rows).toHaveLength(1);
    expect(rows[0]?.finishedState).toBe("succeeded");
    expect(rows[0]?.usage).toEqual({ turns: 2, tokens: 1_842 });
    expect(rows[0]?.limits).toEqual({
      maxTurns: 2,
      maxTokens: 3_000,
      deadlineMs: 300_000,
    });
    expect(rows[0]?.childRunId).toBe("run_child_1");
  });

  it("只有 spawned（未 finished）= 进行中，不冒充终态；子 Run 状态可回填", () => {
    const rows = buildSubtaskViews(
      [event("subtask.spawned", SPAWNED, 1)],
      [run("run_child_1", "running")],
    );
    expect(rows).toHaveLength(1);
    expect(rows[0]?.finishedState).toBeNull();
    expect(rows[0]?.usage).toBeNull();
    expect(rows[0]?.childRunState).toBe("running");
  });

  it("多个子任务按派发顺序排列；不同父 Run 同名 slug 互不干扰", () => {
    const rows = buildSubtaskViews(
      [
        event("subtask.spawned", { ...SPAWNED, subtask_id: "st_b" }, 2),
        event("subtask.spawned", { ...SPAWNED, subtask_id: "st_a" }, 1),
        event("subtask.finished", { subtask_id: "st_a", state: "failed" }, 3),
      ],
      [],
    );
    expect(rows.map((row) => row.subtaskId)).toEqual(["st_a", "st_b"]);
    expect(rows[0]?.finishedState).toBe("failed");
    expect(rows[1]?.finishedState).toBeNull();
  });
});

describe("子任务视图：口径与容错（J3 思路 / J9）", () => {
  it("finished.usage 缺失 = 用量未知（D1 时代事件不写 usage），不当 0", () => {
    const rows = buildSubtaskViews(
      [
        event("subtask.spawned", SPAWNED, 1),
        event("subtask.finished", { subtask_id: "st_read_1", state: "succeeded" }, 4),
      ],
      [],
    );
    expect(rows[0]?.usage).toBeNull();
    expect(rows[0]?.finishedState).toBe("succeeded");
  });

  it("spawned 缺 limits → 限额整块显示未知，不造默认值", () => {
    const payload: Record<string, unknown> = { ...SPAWNED };
    delete payload.limits;
    const rows = buildSubtaskViews([event("subtask.spawned", payload, 1)], []);
    expect(rows[0]?.limits).toBeNull();
  });

  it("limits / usage 是畸形形状时不崩、按未知处理", () => {
    const rows = buildSubtaskViews(
      [
        event("subtask.spawned", { ...SPAWNED, limits: 42 }, 1),
        event(
          "subtask.finished",
          { subtask_id: "st_read_1", state: "succeeded", usage: "3 turns" },
          2,
        ),
      ],
      [],
    );
    expect(rows[0]?.limits).toBeNull();
    expect(rows[0]?.usage).toBeNull();
    expect(rows[0]?.finishedState).toBe("succeeded");
  });

  it("finished 先于 spawned 的异常序列不丢事件（占位行），空数组返回空", () => {
    const rows = buildSubtaskViews(
      [
        event("subtask.finished", { subtask_id: "st_ghost", state: "cancelled" }, 1),
        event("subtask.spawned", { ...SPAWNED, subtask_id: "st_ghost" }, 2),
      ],
      [],
    );
    expect(rows).toHaveLength(1);
    expect(rows[0]?.finishedState).toBe("cancelled");
    expect(rows[0]?.childTaskId).toBe("task_child_1");
    expect(buildSubtaskViews([], [])).toEqual([]);
  });

  it("缺 subtask_id 的事件被丢弃，不产生幽灵行", () => {
    const rows = buildSubtaskViews(
      [event("subtask.spawned", { ...SPAWNED, subtask_id: undefined }, 1)],
      [],
    );
    expect(rows).toEqual([]);
  });
});
