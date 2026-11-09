/**
 * 真实 Host 客户端：同源回环 HTTP。
 *
 * - `POST /rpc`   请求体 `{method, params}`，响应 `{ok, result}` / `{ok:false, error}`
 * - `GET /state`  首屏快照（等价 rpc `state`）
 * - `GET /events?since_seq=N`  SSE；断开后由 `api/stream.ts` 带新 seq 重连
 *
 * 每个请求都带 `X-Tianshu-Token`。令牌只存在内存里（从 index.html 的 meta 读），
 * 不写 localStorage/sessionStorage、不进 URL、不打日志。
 */

import type { EventView, StateView } from "./types";
import { normalizeEvent, normalizeState } from "./normalize";
import { errorFromEnvelope, HostRpcError } from "./errors";
import { createSseParser } from "./sse";
import { asNullableRecord, asRecord, asString } from "../lib/values";

export type RpcParams = Record<string, unknown>;

export interface RequestOptions {
  timeoutMs?: number;
}

export interface EventStreamCallbacks {
  onEvent: (event: EventView) => void;
  /** 收到响应头、开始读流时触发（用于把状态从「连接中」切到「已连接」）。 */
  onOpen?: () => void;
  /** 单个 SSE 帧解析失败；不应中断整条流。 */
  onParseError?: (error: unknown) => void;
}

export interface HostClient {
  readonly mode: "host" | "demo";
  rpc<T = unknown>(method: string, params?: RpcParams, options?: RequestOptions): Promise<T>;
  getState(options?: RequestOptions): Promise<StateView>;
  /**
   * 打开事件流；服务端结束时 resolve，网络/HTTP 错误时 reject。
   * 调用方（stream.ts）负责用新 seq 重连，所以这里不做重试。
   */
  openEvents(sinceSeq: number, callbacks: EventStreamCallbacks, signal: AbortSignal): Promise<void>;
}

const RPC_TIMEOUT_MS = 15_000;
const STATE_TIMEOUT_MS = 8_000;

function parseJson(text: string): unknown {
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return null;
  }
}

function unwrapEnvelope(raw: unknown): unknown {
  const record = asNullableRecord(raw);
  if (record === null) {
    if (raw === null || raw === undefined) {
      throw new HostRpcError("bad_response", "守护进程返回了空响应");
    }
    return raw;
  }
  if (record.ok === false) {
    const fromEnvelope = errorFromEnvelope(record);
    throw new HostRpcError(fromEnvelope?.code ?? "unknown", fromEnvelope?.message ?? "请求失败");
  }
  if (record.ok === true) return record.result;
  // 宽容：没有 ok 字段时按原始结果返回，避免 Host 侧轻微改形直接白屏。
  return raw;
}

function httpStatusMessage(status: number): string {
  if (status === 401 || status === 403) return "没有访问守护进程的权限";
  if (status === 404) return "没有找到对应的接口";
  if (status >= 500) return "守护进程暂时不可用";
  return `守护进程返回 HTTP ${status}`;
}

class RealHostClient implements HostClient {
  readonly mode = "host" as const;
  private readonly token: string;
  private readonly origin: string;

  constructor(token: string, origin: string) {
    this.token = token;
    this.origin = origin;
  }

  private url(path: string): URL {
    return new URL(path, this.origin);
  }

  private headers(extra: Record<string, string> = {}): Record<string, string> {
    return { "X-Tianshu-Token": this.token, ...extra };
  }

  private async requestJson(
    path: string,
    init: { method: "GET" | "POST"; body?: string },
    timeoutMs: number,
  ): Promise<unknown> {
    const controller = new AbortController();
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, timeoutMs);

    let response: Response;
    try {
      response = await fetch(this.url(path), {
        method: init.method,
        headers: this.headers(
          init.body !== undefined ? { "Content-Type": "application/json" } : {},
        ),
        body: init.body,
        signal: controller.signal,
        cache: "no-store",
        credentials: "same-origin",
      });
    } catch (error) {
      if (timedOut) {
        throw new HostRpcError("timeout", "守护进程响应超时", { httpStatus: null });
      }
      if (error instanceof DOMException && error.name === "AbortError") {
        throw new HostRpcError("timeout", "对守护进程的请求被中止");
      }
      throw new HostRpcError("unreachable", "无法连接守护进程");
    } finally {
      clearTimeout(timer);
    }

    let text = "";
    try {
      text = await response.text();
    } catch {
      text = "";
    }
    const raw = text.trim() === "" ? null : parseJson(text);

    if (!response.ok) {
      const fromEnvelope = errorFromEnvelope(raw);
      if (fromEnvelope !== null) {
        throw new HostRpcError(fromEnvelope.code, fromEnvelope.message, {
          httpStatus: response.status,
        });
      }
      throw new HostRpcError(`http_${response.status}`, httpStatusMessage(response.status), {
        httpStatus: response.status,
      });
    }

    return unwrapEnvelope(raw);
  }

  async rpc<T = unknown>(
    method: string,
    params: RpcParams = {},
    options: RequestOptions = {},
  ): Promise<T> {
    const result = await this.requestJson(
      "/rpc",
      { method: "POST", body: JSON.stringify({ method, params }) },
      options.timeoutMs ?? RPC_TIMEOUT_MS,
    );
    return result as T;
  }

  async getState(options: RequestOptions = {}): Promise<StateView> {
    const raw = await this.requestJson(
      "/state",
      { method: "GET" },
      options.timeoutMs ?? STATE_TIMEOUT_MS,
    );
    return normalizeState(raw);
  }

  async openEvents(
    sinceSeq: number,
    callbacks: EventStreamCallbacks,
    signal: AbortSignal,
  ): Promise<void> {
    const url = this.url("/events");
    url.searchParams.set("since_seq", String(Math.max(0, Math.trunc(sinceSeq))));

    let response: Response;
    try {
      response = await fetch(url, {
        method: "GET",
        headers: this.headers({ Accept: "text/event-stream" }),
        signal,
        cache: "no-store",
        credentials: "same-origin",
      });
    } catch (error) {
      if (signal.aborted) throw error;
      if (error instanceof DOMException && error.name === "AbortError") throw error;
      throw new HostRpcError("unreachable", "无法连接事件流");
    }

    if (!response.ok) {
      let text = "";
      try {
        text = await response.text();
      } catch {
        text = "";
      }
      const fromEnvelope = errorFromEnvelope(text.trim() === "" ? null : parseJson(text));
      throw new HostRpcError(
        fromEnvelope?.code ?? `http_${response.status}`,
        fromEnvelope?.message ?? `事件流请求失败（${httpStatusMessage(response.status)}）`,
        { httpStatus: response.status },
      );
    }
    if (response.body === null) {
      throw new HostRpcError("bad_response", "守护进程没有返回事件流");
    }

    callbacks.onOpen?.();

    const parser = createSseParser(({ event, data }) => {
      if (event === "ping" || data.trim() === "") return;
      let parsed: unknown;
      try {
        parsed = JSON.parse(data) as unknown;
      } catch (error) {
        callbacks.onParseError?.(error);
        return;
      }
      const normalized = normalizeEvent(parsed);
      if (normalized === null) {
        callbacks.onParseError?.(new Error("事件帧缺少 seq/type"));
        return;
      }
      callbacks.onEvent(normalized);
    });

    const reader = response.body.getReader();
    const decoder = new TextDecoder("utf-8");
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        parser.push(decoder.decode(value, { stream: true }));
      }
    } finally {
      try {
        reader.releaseLock();
      } catch {
        // 流已经被中止时 releaseLock 可能抛错；此时连接马上会被丢弃。
      }
    }
    parser.push(decoder.decode());
    parser.flush();
  }
}

export function createRealHostClient(token: string, origin?: string): HostClient {
  const base = origin ?? (typeof location === "undefined" ? "http://127.0.0.1" : location.origin);
  return new RealHostClient(token, base);
}

/** 供测试/诊断：把 SSE 帧里的裸 JSON 变成事件视图（导出给 demo 之外的地方复用）。 */
export function parseEventFrameData(data: string): EventView | null {
  return normalizeEvent(parseJson(data));
}

/** 只用于读取 daemon 版本等展示信息，避免组件直接依赖内部方法。 */
export function daemonVersionOf(state: StateView | null): string | null {
  const record = asRecord(state?.daemon);
  return asString(record.version);
}