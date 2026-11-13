import { describe, expect, it } from "vitest";

import { normalizeMemoryList, normalizeMemorySearch } from "../../api/memory";
import type { MemoryApi } from "../../api/memory";
import type { MemoryListResult, MemorySearchResult } from "../../api/types";
import { MemoryController } from "../memoryController";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function list(id: string, scope = "user"): MemoryListResult {
  return normalizeMemoryList({ items: [{
    memory_id: id, revision: 1, scope, scope_id: scope === "task" ? "task_1" : "local",
    kind: "preference", status: "active", origin: "user_explicit",
  }] });
}

function apiWith(overrides: Partial<MemoryApi>): MemoryApi {
  return {
    list: async () => list("user_1"),
    search: async () => normalizeMemorySearch({ items: [], query: "query" }),
    get: async () => null,
    remember: async () => null,
    confirm: async () => null,
    forget: async () => null,
    erase: async () => null,
    ...overrides,
  };
}

const unreachable = () => Object.assign(new Error("connection lost"), { code: "unreachable" });

describe("记忆操作结果和请求顺序", () => {
  it("写入已确认后刷新失败仍保留写入成功事实", async () => {
    let writes = 0;
    const controller = new MemoryController(apiWith({
      remember: async () => { writes += 1; return null; },
      list: async () => { throw unreachable(); },
    }), () => {});
    await controller.remember("preference", "中文");
    expect(writes).toBe(1);
    expect(controller.current.notice).toContain("已记住");
    expect(controller.current.error).toContain("刷新");
    expect(controller.current.error).not.toContain("没有执行");
    expect(controller.current.busy).toBeNull();
  });

  it("写请求断线只能报告结果无法确认，不能推断未执行", async () => {
    let writes = 0;
    const controller = new MemoryController(apiWith({
      remember: async () => { writes += 1; throw unreachable(); },
    }), () => {});
    await controller.remember("preference", "中文");
    expect(writes).toBe(1);
    expect(controller.current.notice).toBeNull();
    expect(controller.current.error).toContain("无法确认");
    expect(controller.current.error).not.toContain("没有执行");
  });

  it.each(["success", "failure"])("旧列表的%s不能覆盖新task作用域", async (outcome) => {
    const old = deferred<MemoryListResult>();
    const controller = new MemoryController(apiWith({
      list: async (scope) => scope?.scope === "task" ? list("task_1", "task") : old.promise,
    }), () => {});
    const pending = controller.load();
    await controller.setScope({ scope: "task", scope_id: "task_1" });
    if (outcome === "success") old.resolve(list("old_user"));
    else old.reject(unreachable());
    await pending;
    expect(controller.current.scope).toEqual({ scope: "task", scope_id: "task_1" });
    expect(controller.current.items[0]?.memory_id).toBe("task_1");
    expect(controller.current.phase).toBe("ready");
    expect(controller.current.error).toBeNull();
  });

  it("旧搜索结果不覆盖后发搜索，清空搜索回到列表也清空query", async () => {
    const old = deferred<MemorySearchResult>();
    const controller = new MemoryController(apiWith({
      search: async (query) => query === "old" ? old.promise : normalizeMemorySearch({
        items: list("new").items, query,
      }),
    }), () => {});
    const pending = controller.search("old");
    await controller.search("new");
    old.resolve(normalizeMemorySearch({ items: list("old").items, query: "old" }));
    await pending;
    expect(controller.current.items[0]?.memory_id).toBe("new");
    expect(controller.current.result?.query).toBe("new");
    await controller.search(" ");
    expect(controller.current.query).toBe("");
    expect(controller.current.result).toBeNull();
  });

  it("写后刷新晚于切换作用域时，不覆盖新列表但结束busy并保留写结果", async () => {
    const refresh = deferred<MemoryListResult>();
    const refreshing = deferred<void>();
    const controller = new MemoryController(apiWith({
      list: async (scope) => {
        if (scope?.scope === "task") return list("task_1", "task");
        refreshing.resolve();
        return refresh.promise;
      },
    }), () => {});
    const writing = controller.remember("preference", "中文");
    await refreshing.promise;
    await controller.setScope({ scope: "task", scope_id: "task_1" });
    refresh.resolve(list("old_user"));
    await writing;
    expect(controller.current.items[0]?.memory_id).toBe("task_1");
    expect(controller.current.result).toBeNull();
    expect(controller.current.busy).toBeNull();
    expect(controller.current.notice).toContain("已记住");
  });

  it("冲突后的刷新失败不得显示已刷新列表", async () => {
    const controller = new MemoryController(apiWith({
      remember: async () => { throw Object.assign(new Error("old revision"), { code: "conflict" }); },
      list: async () => { throw unreachable(); },
    }), () => {});
    await controller.remember("preference", "中文");
    expect(controller.current.error).toContain("过期");
    expect(controller.current.error).not.toContain("已刷新");
    expect(controller.current.busy).toBeNull();
  });
});
