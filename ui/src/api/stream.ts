/**
 * 事件流运行器：断线重连 + 增量游标 + 正常结束后的兜底重连。
 *
 * `since_seq` 语义是“请把 seq >= N 的事件给我”，所以游标是「已收到的最大 seq + 1」。
 * 服务端可能在补齐事件后主动结束流（首屏补齐模式），也可能永不断开；两种都支持：
 * - 流结束：回调 `onEnd`（界面借此再拉一次 `events.run` / `/state` 兜底），
 *   然后在短暂延迟后带新游标重连；
 * - 网络错误：指数退避重连（1s → 5s 封顶），期间界面可继续 3 秒轮询兜底。
 */

import type { EventView } from "./types";
import type { HostClient } from "./host";

export type StreamStatus = "connecting" | "live" | "reconnecting" | "polling" | "stopped";

export interface EventStreamOptions {
  host: HostClient;
  /** 每次重连时读取“当前已知的下一 seq”，与内部游标取最大值。 */
  initialSeq: () => number;
  onEvent: (event: EventView) => void;
  onStatus?: (status: StreamStatus, detail?: string) => void;
  /** 流正常结束（服务端关闭）时触发，用于做一次兜底拉取。 */
  onEnd?: () => void;
}

export interface EventStreamHandle {
  stop: () => void;
}

const NORMAL_END_DELAY_MS = 400;
const EMPTY_END_DELAY_MS = 1_200;
const RETRY_BASE_MS = 800;
const RETRY_MAX_MS = 5_000;
const POLLING_AFTER_FAILURES = 3;

export function startEventStream(options: EventStreamOptions): EventStreamHandle {
  let stopped = false;
  let controller: AbortController | null = null;
  let sleepTimer: ReturnType<typeof setTimeout> | null = null;
  let wakeFromSleep: (() => void) | null = null;
  let cursor = Math.max(0, Math.trunc(options.initialSeq()));

  const sleep = (ms: number): Promise<void> =>
    new Promise<void>((resolve) => {
      sleepTimer = setTimeout(() => {
        sleepTimer = null;
        wakeFromSleep = null;
        resolve();
      }, ms);
      wakeFromSleep = () => {
        if (sleepTimer !== null) clearTimeout(sleepTimer);
        sleepTimer = null;
        wakeFromSleep = null;
        resolve();
      };
    });

  const run = async (): Promise<void> => {
    options.onStatus?.("connecting");
    let failures = 0;

    while (!stopped) {
      controller = new AbortController();
      const since = Math.max(cursor, Math.max(0, Math.trunc(options.initialSeq())));
      let delivered = 0;

      try {
        await options.host.openEvents(
          since,
          {
            onOpen: () => {
              failures = 0;
              options.onStatus?.("live");
            },
            onEvent: (event) => {
              cursor = Math.max(cursor, event.seq + 1);
              delivered += 1;
              options.onEvent(event);
            },
          },
          controller.signal,
        );
        if (stopped) break;
        options.onEnd?.();
        options.onStatus?.("reconnecting", "事件流已结束，正在补齐缺口");
        await sleep(delivered === 0 ? EMPTY_END_DELAY_MS : NORMAL_END_DELAY_MS);
      } catch {
        if (stopped) break;
        failures += 1;
        const delay = Math.min(RETRY_MAX_MS, RETRY_BASE_MS * 2 ** Math.min(failures - 1, 3));
        if (failures >= POLLING_AFTER_FAILURES) {
          options.onStatus?.("polling", "事件流暂时不可用，已回退到每 3 秒轮询");
        } else {
          options.onStatus?.("reconnecting", "事件流断开，正在重连");
        }
        await sleep(delay);
      } finally {
        controller = null;
      }
    }
    options.onStatus?.("stopped");
  };

  void run();

  return {
    stop: () => {
      if (stopped) return;
      stopped = true;
      wakeFromSleep?.();
      controller?.abort();
    },
  };
}