/**
 * 记忆（F10 / G2 记忆阶段 M1）：**人自己的**查看与治理入口。
 *
 * 与模型工具面是两条路：模型走 `AgentLoop` → `Dispatcher` → `MemoryExecutor`
 * （按作用域装备过滤）；这里走控制面 RPC（UI / CLI 共用同一套 `memory_control`）。
 * 名字相同不代表权限相同——决定权限的是走哪条路。
 *
 * 这个模块只做三件事：调 RPC、把 Host 的宽松 payload 归一成界面形状、把错误码
 * 翻译成"人该怎么办"。**不做乐观更新**：记忆有 revision，猜状态会骗人。
 */

import type { HostClient } from "./host";
import type {
  MemoryListResult,
  MemoryScopeChoice,
  MemorySearchResult,
  MemoryView,
} from "./types";
import { RPC } from "./types";
import { asArray, asBoolean, asNumber, asRecord, asString, readString } from "../lib/values";

/** 一条记忆的命运：候选待确认 / 生效 / 停召回 / 被新版取代 / 正文已擦除。 */
export const MEMORY_STATUSES = ["candidate", "active", "forgotten", "superseded"] as const;

function normalizeMemory(raw: unknown): MemoryView | null {
  const record = asRecord(raw);
  const memoryId = readString(record, "memory_id", "memoryId", "id");
  if (memoryId === null) return null;
  const content = asString(record["content"]);
  const scoreKind = readString(record, "score_kind");
  // 三态：后端省略这个键 = 判不了；`asBoolean` 的 null 要收回成 undefined。
  const staleValue = asBoolean(record["stale"]);
  const stale = staleValue === null ? undefined : staleValue;
  return {
    memory_id: memoryId,
    revision: asNumber(record["revision"]) ?? 0,
    scope: readString(record, "scope") ?? "user",
    scope_id: readString(record, "scope_id"),
    kind: readString(record, "kind") ?? "fact",
    content_ref: readString(record, "content_ref"),
    origin: readString(record, "origin") ?? "inferred",
    status: readString(record, "status") ?? "active",
    source_refs: asArray(record["source_refs"]).filter(
      (item): item is string => typeof item === "string",
    ),
    proof_count: asNumber(record["proof_count"]) ?? 0,
    layer: readString(record, "layer") ?? "l1",
    erased_at: asNumber(record["erased_at"]),
    created_at: asNumber(record["created_at"]),
    updated_at: asNumber(record["updated_at"]),
    content,
    content_state: readString(record, "content_state"),
    score: asNumber(record["score"]),
    score_kind: scoreKind,
    // 三态：true 陈旧 / false 已确认新鲜 / undefined 不可判定（与后端一致：省略=判不了）
    stale,
  };
}

function normalizeScopes(raw: unknown): string[] {
  return asArray(raw).filter((item): item is string => typeof item === "string");
}

export function normalizeMemoryList(raw: unknown): MemoryListResult {
  const record = asRecord(raw);
  const items = asArray(record["items"])
    .map(normalizeMemory)
    .filter((item): item is MemoryView => item !== null);
  return { items, scopes: normalizeScopes(record["scopes"]) };
}

export function normalizeMemorySearch(raw: unknown): MemorySearchResult {
  const record = asRecord(raw);
  const list = normalizeMemoryList(record);
  return {
    items: list.items,
    scopes: list.scopes,
    query: readString(record, "query") ?? "",
    // 能力标注**不许**压成一句"没找到"：语义路有没有跑、为什么没跑，要原样给 UI。
    semantic: asBoolean(record["semantic"]) === true,
    degraded: readString(record, "degraded"),
    fused: asBoolean(record["fused"]) === true,
    limit: asNumber(record["limit"]),
  };
}

/** 控制面记忆方法的调用面。测试用假实现替换这一个接口即可（组件不直接碰 Host）。 */
export interface MemoryApi {
  list(scope: MemoryScopeChoice | null, status: string | null): Promise<MemoryListResult>;
  search(query: string, scope: MemoryScopeChoice | null): Promise<MemorySearchResult>;
  get(memoryId: string): Promise<MemoryView | null>;
  remember(kind: string, content: string): Promise<MemoryView | null>;
  confirm(memoryId: string, revision: number): Promise<MemoryView | null>;
  forget(memoryId: string, revision: number): Promise<MemoryView | null>;
  erase(memoryId: string, revision: number): Promise<MemoryView | null>;
}

function scopeParams(scope: MemoryScopeChoice | null): Record<string, unknown> {
  if (scope === null || scope.scope === "user") return {};
  return { scope: scope.scope, scope_id: scope.scope_id };
}

export function createMemoryApi(client: HostClient): MemoryApi {
  const writeView = async (method: string, params: Record<string, unknown>) => {
    const raw = await client.rpc<unknown>(method, params);
    const record = asRecord(raw);
    const view = record["memory"];
    return view === undefined ? null : normalizeMemory(view);
  };
  return {
    async list(scope, status) {
      const params: Record<string, unknown> = { ...scopeParams(scope) };
      if (status !== null) params["status"] = status;
      return normalizeMemoryList(await client.rpc<unknown>(RPC.memoryList, params));
    },
    async search(query, scope) {
      const params: Record<string, unknown> = { query, ...scopeParams(scope) };
      return normalizeMemorySearch(await client.rpc<unknown>(RPC.memorySearch, params));
    },
    async get(memoryId) {
      const raw = await client.rpc<unknown>(RPC.memoryGet, { memory_id: memoryId });
      return normalizeMemory(asRecord(raw)["memory"]);
    },
    async remember(kind, content) {
      return writeView(RPC.memoryRemember, { kind, content });
    },
    async confirm(memoryId, revision) {
      return writeView(RPC.memoryConfirm, { memory_id: memoryId, revision });
    },
    async forget(memoryId, revision) {
      return writeView(RPC.memoryForget, { memory_id: memoryId, revision });
    },
    async erase(memoryId, revision) {
      return writeView(RPC.memoryErase, { memory_id: memoryId, revision });
    },
  };
}
