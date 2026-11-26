// @vitest-environment jsdom
import { afterEach, expect, it, vi } from "vitest";
import { DemoHostClient } from "../demo/host";
import { DemoWorld } from "../demo/world";
import type { RunEnvelope } from "../api/types";

afterEach(() => vi.useRealTimers());

it("独立验证：演示 host 明确拒绝计划，不模拟执行", async () => {
  vi.useFakeTimers();
  const world = new DemoWorld(1_000);
  const task = world.createTask("先只生成修改计划", [
    { root_path: "/tmp/review-only", mode: "writable" },
  ]);
  const host = new DemoHostClient(world);
  const before = structuredClone(world.runs);
  await expect(host.rpc<RunEnvelope>("run.start", {
    task_id: task.task_id,
    mode: "plan",
    request_id: "review-demo-plan",
    execution_policy: { format_version: 1, mode: "effects" },
  })).rejects.toThrow("需要连接真实守护进程");
  await vi.runAllTimersAsync();
  expect(world.runs).toEqual(before);
});
