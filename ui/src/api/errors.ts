/**
 * Host 调用失败的类型与中文文案。
 *
 * UI 需要能区分「连不上 / 令牌不对 / 状态冲突 / 参数错」——这决定用户该重启
 * 宿主、重试，还是放弃这个操作。错误码由 Host 的 `{"ok":false,"error":{...}}`
 * 信封给出（见 M5 后端契约）。
 */

import type { AnyRecord } from "../lib/values";
import { asNullableRecord, asString, truncate } from "../lib/values";

export class HostRpcError extends Error {
  readonly code: string;
  readonly details: AnyRecord | null;
  readonly httpStatus: number | null;

  constructor(
    code: string,
    message: string,
    options: { details?: AnyRecord | null; httpStatus?: number | null } = {},
  ) {
    super(message);
    this.name = "HostRpcError";
    this.code = code;
    this.details = options.details ?? null;
    this.httpStatus = options.httpStatus ?? null;
  }
}

export function isHostRpcError(error: unknown): error is HostRpcError {
  return error instanceof HostRpcError;
}

const CODE_MESSAGES: Record<string, string> = {
  unauthorized: "没有访问守护进程的权限（令牌可能已失效，请重启宿主）",
  http_401: "没有访问守护进程的权限（令牌可能已失效，请重启宿主）",
  http_403: "没有访问守护进程的权限",
  forbidden: "这个操作不被允许",
  unreachable: "无法连接守护进程",
  timeout: "守护进程响应超时",
  bad_response: "守护进程返回了无法识别的响应",
  not_found: "没有找到要操作的对象（可能已被删除或被新的运行取代）",
  conflict: "操作与当前状态冲突（状态可能已经变化，请刷新后再试）",
  invalid: "请求参数不正确",
  bad_request: "请求参数不正确",
  http_400: "请求参数不正确",
  http_404: "没有找到对应的接口或对象",
  internal: "守护进程内部错误",
  http_500: "守护进程内部错误",
  http_502: "守护进程暂时不可用",
  http_503: "守护进程暂时不可用",
};

/** 已知错误码 → 中文；未知错误码 → 带原始 message 的中文兜底（不白屏）。 */
export function humanizeHostError(error: unknown): string {
  if (isHostRpcError(error)) {
    const known = CODE_MESSAGES[error.code];
    const message = error.message.trim();
    if (
      error.code === "internal" &&
      (message.startsWith("dispatch.disk_space_low:") ||
        message.startsWith("dispatch.disk_space_unknown:"))
    ) {
      return truncate(message, 140);
    }
    if (known !== undefined) return known;
    return message === "" ? "操作失败，请重试" : `操作失败：${truncate(message, 140)}`;
  }
  if (error instanceof Error) {
    const message = error.message.trim();
    if (message === "" || message === "Failed to fetch" || message === "Load failed") {
      return "无法连接守护进程";
    }
    return `操作失败：${truncate(message, 140)}`;
  }
  return "操作失败，请重试";
}

/** 从响应体的错误信封里提取 code/message（宽容处理缺失字段）。 */
export function errorFromEnvelope(raw: unknown): { code: string; message: string } | null {
  const record = asNullableRecord(raw);
  if (record === null || record.ok !== false) return null;
  const error = asNullableRecord(record.error) ?? {};
  const code = asString(error.code) ?? "unknown";
  const message = asString(error.message) ?? "请求失败";
  return { code, message };
}
