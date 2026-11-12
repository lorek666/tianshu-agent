import { describe, expect, it } from "vitest";

import type { MemorySearchResult, MemoryView } from "../../api/types";
import {
  memoryCapabilityText,
  memoryEvidenceText,
  memoryLayerLabel,
  memoryOriginLabel,
  memoryScoreText,
  memoryScopesText,
  memoryStaleText,
  memoryStatusLabel,
  memorySummaryText,
} from "../memoryView";

function view(overrides: Partial<MemoryView> = {}): MemoryView {
  return {
    memory_id: "mem_1",
    revision: 1,
    scope: "user",
    scope_id: "local",
    kind: "preference",
    content_ref: "sha256:abc",
    origin: "user_explicit",
    status: "active",
    source_refs: [],
    proof_count: 0,
    layer: "l1",
    erased_at: null,
    created_at: 1,
    updated_at: 1,
    content: null,
    content_state: null,
    score: null,
    score_kind: null,
    stale: undefined,
    ...overrides,
  };
}

function result(overrides: Partial<MemorySearchResult> = {}): MemorySearchResult {
  return {
    items: [],
    scopes: ["memory://user/local"],
    query: "周报",
    semantic: false,
    degraded: "未配置向量 provider",
    fused: false,
    limit: 5,
    ...overrides,
  };
}

describe("记忆人话层", () => {
  it("状态/来源/层都翻成人话，未知值原样说明而不是空白", () => {
    expect(memoryStatusLabel("active")).toContain("生效");
    expect(memoryStatusLabel("candidate")).toContain("候选");
    expect(memoryOriginLabel("user_confirmed")).toContain("确认");
    expect(memoryLayerLabel("l3")).toContain("偏好");
    expect(memoryStatusLabel("weird")).toContain("weird");
  });

  it("stale 三态不许压成两态：后端省略就是判不了", () => {
    expect(memoryStaleText(view({ stale: undefined }))).toBeNull();
    expect(memoryStaleText(view({ stale: true }))).toContain("过时");
    expect(memoryStaleText(view({ stale: false }))).toContain("一致");
  });

  it("排序依据分两种口径，不混成一句分数", () => {
    expect(memoryScoreText(view({ score_kind: "origin" }))).toContain("来源权重");
    expect(memoryScoreText(view({ score_kind: "rrf" }))).toContain("融合");
    expect(memoryScoreText(view({ score_kind: null }))).toBeNull();
  });

  it("能力标注：没有语义路时把降级原因说出来（不许说成没找到）", () => {
    expect(memoryCapabilityText(result())).toContain("只有词面召回");
    expect(memoryCapabilityText(result())).toContain("未配置向量 provider");
    expect(memoryCapabilityText(result({ semantic: true, degraded: null, fused: true }))).toContain(
      "融合",
    );
  });

  it("可见作用域与证据条数分开说（空结果时人才能分清没这条/没覆盖到）", () => {
    expect(memoryScopesText(["memory://user/local", "memory://task/t1"])).toContain(
      "memory://task/t1",
    );
    expect(memoryScopesText([])).toContain("没有可见作用域");
    expect(memoryEvidenceText(view({ source_refs: ["a", "b"], proof_count: 3 }))).toBe(
      "来源 2 条 · 证明 3 条",
    );
  });

  it("列表摘要只给一段；没有正文时说清楚要点开才加载", () => {
    expect(memorySummaryText(null)).toContain("点开");
    expect(memorySummaryText("一".repeat(200), 10)).toHaveLength(11);
    expect(memorySummaryText("  多   空格\n换行  ")).toBe("多 空格 换行");
  });
});
