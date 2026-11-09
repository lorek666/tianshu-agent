/**
 * 宽容读取工具。
 *
 * Host 的 `payload` 没有逐字段冻结（PRD §5.3.4），事件字段缺失、类型不对都
 * 不能让界面白屏。所有读取都经过这里：拿不准就返回 null，由人话层给兜底文案。
 */

export type AnyRecord = Record<string, unknown>;

export function asRecord(value: unknown): AnyRecord {
  if (typeof value === "object" && value !== null && !Array.isArray(value)) {
    return value as AnyRecord;
  }
  return {};
}

export function asNullableRecord(value: unknown): AnyRecord | null {
  if (typeof value === "object" && value !== null && !Array.isArray(value)) {
    return value as AnyRecord;
  }
  return null;
}

export function asString(value: unknown): string | null {
  if (typeof value === "string" && value.trim() !== "") {
    return value;
  }
  return null;
}

export function asNumber(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) {
    return value;
  }
  return null;
}

export function asBoolean(value: unknown): boolean | null {
  return typeof value === "boolean" ? value : null;
}

export function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

/** 依次从多个键里找第一个非空字符串；事件 payload 的键名在不同版本间变化过。 */
export function readString(record: AnyRecord, ...keys: string[]): string | null {
  for (const key of keys) {
    const value = asString(record[key]);
    if (value !== null) return value;
  }
  return null;
}

export function readNumber(record: AnyRecord, ...keys: string[]): number | null {
  for (const key of keys) {
    const value = asNumber(record[key]);
    if (value !== null) return value;
  }
  return null;
}

export function readBoolean(record: AnyRecord, ...keys: string[]): boolean | null {
  for (const key of keys) {
    const value = asBoolean(record[key]);
    if (value !== null) return value;
  }
  return null;
}

export function readRecord(record: AnyRecord, ...keys: string[]): AnyRecord | null {
  for (const key of keys) {
    const value = asNullableRecord(record[key]);
    if (value !== null) return value;
  }
  return null;
}

export function readArray(record: AnyRecord, ...keys: string[]): unknown[] | null {
  for (const key of keys) {
    if (Array.isArray(record[key])) return record[key] as unknown[];
  }
  return null;
}

/** 只用于展示：截断长文本，避免一条事件把时间线撑爆。 */
export function truncate(value: string, max = 120): string {
  const text = value.replace(/\s+/g, " ").trim();
  if (text.length <= max) return text;
  return `${text.slice(0, Math.max(0, max - 1))}…`;
}

export function basename(path: string): string {
  const normalized = path.replace(/[\\/]+$/, "");
  const index = Math.max(normalized.lastIndexOf("/"), normalized.lastIndexOf("\\"));
  return index >= 0 ? normalized.slice(index + 1) : normalized;
}

/** 路径的上一级目录；用于在审批卡片里把长路径折行显示。 */
export function dirname(path: string): string {
  const normalized = path.replace(/[\\/]+$/, "");
  const index = Math.max(normalized.lastIndexOf("/"), normalized.lastIndexOf("\\"));
  if (index <= 0) return "";
  return normalized.slice(0, index);
}