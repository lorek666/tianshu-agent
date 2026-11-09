import { describe, expect, it } from "vitest";
import {
  formatClock,
  formatCostSummary,
  formatDuration,
  formatRelative,
  formatUsd,
  formatUsdExact,
  isSubCent,
  shortenPath,
} from "../format";

describe("金额格式化", () => {
  it("固定两位小数，四舍五入到美分", () => {
    expect(formatUsd(0)).toBe("$0.00");
    expect(formatUsd(4_999)).toBe("$0.00");
    expect(formatUsd(5_000)).toBe("$0.01");
    expect(formatUsd(12_300)).toBe("$0.01");
    expect(formatUsd(1_234_567)).toBe("$1.23");
    expect(formatUsd(-1_234_567)).toBe("-$1.23");
    expect(formatUsd(Number.NaN)).toBe("$0.00");
  });

  it("精确值保留六位小数，只用于悬停/详情", () => {
    expect(formatUsdExact(0)).toBe("$0.000000");
    expect(formatUsdExact(1_234_567)).toBe("$1.234567");
    expect(formatUsdExact(4_999)).toBe("$0.004999");
  });

  it("不足一美分单独判定，避免把“花了钱”显示成没花钱", () => {
    expect(isSubCent(4_999)).toBe(true);
    expect(isSubCent(0)).toBe(false);
    expect(isSubCent(10_000)).toBe(false);
  });

  it("成本摘要区分“未知”与“0 元”", () => {
    expect(formatCostSummary({ totalMicros: 0, knownCalls: 0, unknownCalls: 0, calls: 0 })).toBe(
      "暂无模型调用",
    );
    expect(formatCostSummary({ totalMicros: 0, knownCalls: 0, unknownCalls: 2, calls: 2 })).toBe(
      "费用未知（2 次调用没有价格）",
    );
    expect(formatCostSummary({ totalMicros: 12_300, knownCalls: 1, unknownCalls: 0, calls: 1 })).toBe(
      "$0.01（1 次调用）",
    );
    expect(
      formatCostSummary({ totalMicros: 12_300, knownCalls: 1, unknownCalls: 2, calls: 3 }),
    ).toContain("另有 2 次费用未知");
  });
});

describe("时间格式化", () => {
  const now = 1_700_000_000_000;

  it("无效时间不显示 NaN", () => {
    expect(formatClock(null)).toBe("--:--:--");
    expect(formatClock(0)).toBe("--:--:--");
    expect(formatRelative(null, now)).toBe("时间未知");
  });

  it("相对时间分档", () => {
    expect(formatRelative(now - 5_000, now)).toBe("刚刚");
    expect(formatRelative(now - 30_000, now)).toBe("30 秒前");
    expect(formatRelative(now - 5 * 60_000, now)).toBe("5 分钟前");
    expect(formatRelative(now - 3 * 3_600_000, now)).toBe("3 小时前");
    expect(formatRelative(now - 3 * 86_400_000, now)).toBe("3 天前");
  });

  it("时长格式化", () => {
    expect(formatDuration(1_000, 61_000)).toBe("1 分钟");
    expect(formatDuration(1_000, 66_000)).toBe("1 分 05 秒");
    expect(formatDuration(1_000, 3_661_000)).toBe("1 小时 1 分");
    expect(formatDuration(1_000, null)).toBeTypeOf("string");
  });

  it("长路径中间省略但保留文件名", () => {
    const path = "/Users/you/Downloads/2024 年旧账单合集/2024-03 电费账单.pdf";
    const short = shortenPath(path, 40);
    expect(short.length).toBeLessThanOrEqual(40);
    expect(short).toContain("2024-03 电费账单.pdf");
    expect(shortenPath("/tmp/a.pdf", 100)).toBe("/tmp/a.pdf");
  });
});