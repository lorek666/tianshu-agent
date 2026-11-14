/** 展示用的格式化：时间、金额、路径。全部是纯函数，便于单测。 */

const pad2 = (value: number): string => String(value).padStart(2, "0");

/** 微美元 → `$1.23`（固定两位小数，四舍五入到美分）。 */
export function formatUsd(micros: number): string {
  if (!Number.isFinite(micros)) return "$0.00";
  const sign = micros < 0 ? "-" : "";
  const cents = Math.round(Math.abs(micros) / 10_000);
  const whole = Math.floor(cents / 100);
  const fraction = cents % 100;
  return `${sign}$${whole}.${pad2(fraction)}`;
}

/** 精确到六位小数，只用于 tooltip / 详情，不替代主展示的两位小数。 */
export function formatUsdExact(micros: number): string {
  if (!Number.isFinite(micros)) return "$0.000000";
  const sign = micros < 0 ? "-" : "";
  const abs = Math.round(Math.abs(micros));
  const whole = Math.floor(abs / 1_000_000);
  const fraction = abs % 1_000_000;
  return `${sign}$${whole}.${String(fraction).padStart(6, "0")}`;
}

/**
 * 有成本但不足半美分：四舍五入到两位小数会显示 $0.00，旁边应提示“不足一美分”。
 * 半美分（5000 微美元）及以上会显示 $0.01，不再加这个提示，免得自相矛盾。
 */
export function isSubCent(micros: number): boolean {
  return micros > 0 && micros < 5_000;
}

export interface CostLike {
  totalMicros: number;
  knownCalls: number;
  unknownCalls: number;
  calls: number;
}

/** 成本摘要 → 一行中文。未知成本不会被抹成 0。 */
export function formatCostSummary(summary: CostLike): string {
  if (summary.calls <= 0) return "暂无模型调用";
  if (summary.knownCalls <= 0) {
    return `费用未知（${summary.unknownCalls} 次调用没有价格）`;
  }
  const known = formatUsd(summary.totalMicros);
  if (summary.unknownCalls > 0) {
    return `${known}（${summary.knownCalls} 次调用，另有 ${summary.unknownCalls} 次费用未知）`;
  }
  return `${known}（${summary.knownCalls} 次调用）`;
}

/** 大整数千分位分组（确定性：固定 en-US 规则，不随运行环境 locale 变化）。 */
export function formatInt(value: number): string {
  if (!Number.isFinite(value)) return String(value);
  const sign = value < 0 ? "-" : "";
  const digits = String(Math.abs(Math.trunc(value)));
  return sign + digits.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

export function formatClock(ts: number | null | undefined): string {
  if (typeof ts !== "number" || !Number.isFinite(ts) || ts <= 0) return "--:--:--";
  const date = new Date(ts);
  return `${pad2(date.getHours())}:${pad2(date.getMinutes())}:${pad2(date.getSeconds())}`;
}

export function formatDateTime(ts: number | null | undefined): string {
  if (typeof ts !== "number" || !Number.isFinite(ts) || ts <= 0) return "时间未知";
  const date = new Date(ts);
  return `${date.getMonth() + 1} 月 ${date.getDate()} 日 ${pad2(date.getHours())}:${pad2(date.getMinutes())}`;
}

export function formatRelative(ts: number | null | undefined, now: number = Date.now()): string {
  if (typeof ts !== "number" || !Number.isFinite(ts) || ts <= 0) return "时间未知";
  const diff = now - ts;
  if (diff < 10_000) return "刚刚";
  if (diff < 60_000) return `${Math.floor(diff / 1000)} 秒前`;
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} 小时前`;
  if (diff < 30 * 86_400_000) return `${Math.floor(diff / 86_400_000)} 天前`;
  return formatDateTime(ts);
}

/** 一段持续时长，例如「1 分 05 秒」。 */
export function formatDuration(start: number | null | undefined, end: number | null | undefined): string {
  if (typeof start !== "number" || !Number.isFinite(start) || start <= 0) return "时长未知";
  const until = typeof end === "number" && Number.isFinite(end) && end > 0 ? end : Date.now();
  const seconds = Math.max(0, Math.floor((until - start) / 1000));
  if (seconds < 60) return `${seconds} 秒`;
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  if (minutes < 60) return rest === 0 ? `${minutes} 分钟` : `${minutes} 分 ${pad2(rest)} 秒`;
  const hours = Math.floor(minutes / 60);
  const restMinutes = minutes % 60;
  return restMinutes === 0 ? `${hours} 小时` : `${hours} 小时 ${restMinutes} 分`;
}

/** 审批卡片里的资源路径：太长时保留开头与文件名，中间省略。 */
export function shortenPath(path: string, max = 72): string {
  if (path.length <= max) return path;
  const slash = Math.max(path.lastIndexOf("/"), path.lastIndexOf("\\"));
  if (slash <= 0) return `${path.slice(0, max - 1)}…`;
  const name = path.slice(slash + 1);
  const headLength = Math.max(8, max - name.length - 12);
  return `${path.slice(0, headLength)}…/${name}`;
}