import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { startEventStream } from "../stream";
import type { StreamStatus } from "../stream";
import type { HostClient } from "../host";

function fakeHost(behaviour: () => Promise<void>): HostClient {
  return {
    mode: "host",
    rpc: vi.fn(),
    getState: vi.fn(),
    openEvents: vi.fn(async () => behaviour()) as unknown as HostClient["openEvents"],
  };
}

describe("事件流重连策略", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("连续失败达到上限后停止自动重连，并报告 disconnected", async () => {
    const statuses: StreamStatus[] = [];
    const host = fakeHost(async () => {
      throw new Error("connection refused");
    });
    const handle = startEventStream({
      host,
      initialSeq: () => 1,
      onEvent: () => undefined,
      onStatus: (status) => statuses.push(status),
      maxFailures: 4,
    });

    await vi.advanceTimersByTimeAsync(20_000);
    handle.stop();

    expect(statuses[0]).toBe("connecting");
    expect(statuses.at(-1)).toBe("disconnected");
    expect(statuses.filter((status) => status === "disconnected")).toHaveLength(1);
    expect(host.openEvents).toHaveBeenCalledTimes(4);
    // 停止后不应再自己发起连接。
    await vi.advanceTimersByTimeAsync(30_000);
    expect(host.openEvents).toHaveBeenCalledTimes(4);
  });

  it("正常结束（服务端补齐后关闭）会带新 seq 重新发起，而不是判定为断线", async () => {
    const statuses: StreamStatus[] = [];
    let attempt = 0;
    const host = fakeHost(async () => {
      attempt += 1;
      // 第一次正常结束；第二次挂起，模拟服务端继续推流。
      if (attempt === 1) return;
      await new Promise<void>((resolve) => setTimeout(resolve, 60_000));
    });
    const handle = startEventStream({
      host,
      initialSeq: () => 7,
      onEvent: () => undefined,
      onStatus: (status) => statuses.push(status),
    });

    await vi.advanceTimersByTimeAsync(2_000);
    handle.stop();

    expect(host.openEvents).toHaveBeenCalledTimes(2);
    // 第二次调用仍然从“已知的下一 seq”开始（默认 7），而不是从头。
    const calls = (host.openEvents as unknown as { mock: { calls: unknown[][] } }).mock.calls;
    expect(calls[0]?.[0]).toBe(7);
    expect(calls[1]?.[0]).toBe(7);
    expect(statuses).not.toContain("disconnected");
  });
});