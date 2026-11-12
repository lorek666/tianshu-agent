/**
 * 记忆的人话层：机器枚举（status / origin / layer / score_kind / stale）只在
 * 折叠详情里原样出现，主界面一律翻成人话（与 `lib/humanize.ts` 同一条纪律）。
 *
 * 两个刻意不做的简化：
 * 1. **`stale` 三态不许压成两态**：后端省略这个键 = "判不了"，写成"新鲜"就是
 *    谎报（L2/L3 合成页可能已经落后于作用域里的新 L1）。
 * 2. **能力标注不许压成"没找到"**：`semantic=false` + `degraded` 是"这次只有
 *    词面召回"，与"库里没有"是两件事。
 */

import type { MemorySearchResult, MemoryView } from "../api/types";

const STATUS_LABELS: Record<string, string> = {
  candidate: "候选（等你确认）",
  active: "生效中（会参与召回）",
  forgotten: "已忘记（不再召回）",
  superseded: "已被新版取代（旧版可查）",
};

const ORIGIN_LABELS: Record<string, string> = {
  user_explicit: "你亲口说的",
  user_confirmed: "你确认过的",
  inferred: "系统推断（未经你确认不召回）",
};

const LAYER_LABELS: Record<string, string> = {
  l1: "原子事实",
  l2: "带证据的归纳",
  l3: "偏好页",
};

const SCORE_KIND_LABELS: Record<string, string> = {
  origin: "排序依据：来源权重",
  rrf: "排序依据：语义 + 词面融合分",
};

export function memoryStatusLabel(status: string): string {
  return STATUS_LABELS[status] ?? `未知状态（${status}）`;
}

export function memoryOriginLabel(origin: string): string {
  return ORIGIN_LABELS[origin] ?? `未知来源（${origin}）`;
}

export function memoryLayerLabel(layer: string): string {
  return LAYER_LABELS[layer] ?? `未知层（${layer}）`;
}

/** 陈旧度：`undefined` = 后端没给 = 判不了，不冒充"新鲜"。 */
export function memoryStaleText(item: MemoryView): string | null {
  if (item.stale === undefined) return null;
  return item.stale ? "这页可能过时：作用域里已有更新的记忆，请回看原子事实" : "与最新证据一致";
}

export function memoryScoreText(item: MemoryView): string | null {
  if (item.score_kind === null) return null;
  return SCORE_KIND_LABELS[item.score_kind] ?? `排序依据：${item.score_kind}`;
}

/** 能力标注：有语义路返回 null（不必打扰人），没有就如实说"只有词面召回"。 */
export function memoryCapabilityText(result: MemorySearchResult): string | null {
  if (result.semantic) {
    return result.fused ? "语义 + 词面融合排序" : "语义检索可用";
  }
  return result.degraded !== null
    ? `这次只有词面召回：${result.degraded}`
    : "这次只有词面召回（未启用语义检索）";
}

/** 可见作用域：空结果时人才能分清"没这条"与"没覆盖到"。 */
export function memoryScopesText(scopes: string[]): string {
  if (scopes.length === 0) return "当前没有可见作用域";
  return `可见作用域：${scopes.join("、")}`;
}

/** 一条记忆的"凭什么"：来源条数与证明条数必须分开说（F10 §8）。 */
export function memoryEvidenceText(item: MemoryView): string {
  return `来源 ${item.source_refs.length} 条 · 证明 ${item.proof_count} 条`;
}

/** 列表页正文摘要：只给一段，详细正文要 `memory.get`（列表刻意不带正文）。 */
export function memorySummaryText(content: string | null, limit = 90): string {
  if (content === null) return "（点开才加载正文）";
  const compact = content.replace(/\s+/g, " ").trim();
  if (compact === "") return "（正文为空）";
  return compact.length <= limit ? compact : `${compact.slice(0, limit)}…`;
}
