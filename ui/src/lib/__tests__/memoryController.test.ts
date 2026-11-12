import { describe, expect, it } from "vitest";

import type { MemoryApi } from "../../api/memory";
import type { MemoryListResult, MemorySearchResult, MemoryView } from "../../api/types";
import { MemoryController } from "../memoryController";
import type { MemoryPanelState } from "../memoryController";

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

class FakeApi implements MemoryApi {
  listCalls = 0;
  searchCalls = 0;
  confirmCalls = 0;
  forgetCalls = 0;
  eraseCalls = 0;
  rememberCalls = 0;
  items: MemoryView[] = [view()];
  searchResult: MemorySearchResult = {
    items: [view()],
    scopes: ["memory://user/local"],
    query: "周报",
    semantic: false,
    degraded: "未配置向量 provider",
    fused: false,
    limit: 5,
  };
  failNext: { code: string; message: string } | null = null;
  failListAfterWrite = false;

  private maybeFail(): void {
    if (this.failNext !== null) {
      const error = Object.assign(new Error(this.failNext.message), {
        code: this.failNext.code,
      });
      this.failNext = null;
      throw error;
    }
  }

  async list(): Promise<MemoryListResult> {
    this.listCalls += 1;
    return { items: this.items, scopes: ["memory://user/local"] };
  }

  async search(): Promise<MemorySearchResult> {
    this.searchCalls += 1;
    return this.searchResult;
  }

  async get(): Promise<MemoryView | null> {
    return view({ content: "<b>报告</b>用中文" });
  }

  async remember(): Promise<MemoryView | null> {
    this.rememberCalls += 1;
    this.maybeFail();
    return null;
  }

  async confirm(): Promise<MemoryView | null> {
    this.confirmCalls += 1;
    this.maybeFail();
    return null;
  }

  async forget(): Promise<MemoryView | null> {
    this.forgetCalls += 1;
    this.maybeFail();
    return null;
  }

  async erase(): Promise<MemoryView | null> {
    this.eraseCalls += 1;
    this.maybeFail();
    return null;
  }
}

function last(seen: MemoryPanelState[]): MemoryPanelState {
  const value = seen[seen.length - 1];
  if (value === undefined) throw new Error("controller 没有 emit 任何状态");
  return value;
}

function firstItem(controller: MemoryController): MemoryView {
  const item = controller.current.items[0];
  if (item === undefined) throw new Error("列表为空，用例前提不成立");
  return item;
}

function controllerFor(api: FakeApi) {
  const seen: MemoryPanelState[] = [];
  const controller = new MemoryController(api, (state) => seen.push(state));
  return { controller, seen, last: () => last(seen) };
}

describe("记忆面板状态机", () => {
  it("加载成功后进入 ready，并带上条目", async () => {
    const api = new FakeApi();
    const { controller, last } = controllerFor(api);
    await controller.load();
    expect(last().phase).toBe("ready");
    expect(last().items).toHaveLength(1);
    expect(api.listCalls).toBe(1);
  });

  it("检索把能力标注交给界面（空结果不是错误）", async () => {
    const api = new FakeApi();
    api.searchResult = { ...api.searchResult, items: [] };
    const { controller, last } = controllerFor(api);
    await controller.search("周报");
    expect(last().phase).toBe("ready");
    expect(last().items).toEqual([]);
    expect(last().result?.semantic).toBe(false);
    expect(last().result?.degraded).toContain("provider");
  });

  it("空检索词退回列表（不发明一个空 query 请求）", async () => {
    const api = new FakeApi();
    const { controller } = controllerFor(api);
    await controller.search("   ");
    expect(api.searchCalls).toBe(0);
    expect(api.listCalls).toBe(1);
  });

  it("写操作不乐观更新：成功后重新拉一遍列表", async () => {
    const api = new FakeApi();
    const { controller, last } = controllerFor(api);
    await controller.load();
    const before = api.listCalls;
    await controller.forget(firstItem(controller));
    expect(api.forgetCalls).toBe(1);
    expect(api.listCalls).toBe(before + 1);
    expect(last().notice).toContain("已忘记");
    expect(last().busy).toBeNull();
  });

  it("revision 过期（conflict）：刷新 + 提示，**不自动重试**写操作", async () => {
    const api = new FakeApi();
    const { controller, last } = controllerFor(api);
    await controller.load();
    api.failNext = { code: "conflict", message: "revision 不是当前版本" };
    await controller.confirm(firstItem(controller));
    expect(api.confirmCalls).toBe(1);
    expect(last().notice).toContain("过期");
    expect(last().error).toBeNull();
    // 自动重放会把"你看到的版本"悄悄变成库里现在的版本——这里必须只发生一次。
    expect(api.confirmCalls).toBe(1);
    expect(last().items).toHaveLength(1);
  });

  it("写失败是明确错误，不是静默成功", async () => {
    const api = new FakeApi();
    const { controller, last } = controllerFor(api);
    await controller.load();
    api.failNext = { code: "not_found", message: "记忆不存在" };
    await controller.forget(firstItem(controller));
    expect(last().phase).toBe("error");
    expect(last().error).toContain("不存在");
    expect(last().notice).toBeNull();
  });

  it("切换作用域会重新拉列表（不是把已有结果过滤一遍）", async () => {
    const api = new FakeApi();
    const { controller, last } = controllerFor(api);
    await controller.load();
    await controller.setScope({ scope: "task", scope_id: "task_1" });
    expect(api.listCalls).toBe(2);
    expect(last().scope).toEqual({ scope: "task", scope_id: "task_1" });
    expect(controller.current.result).toBeNull();
  });

  it("记住一条也走同一套：不猜状态、重新拉列表", async () => {
    const api = new FakeApi();
    const { controller, last } = controllerFor(api);
    await controller.load();
    await controller.remember("preference", "周报用中文，先给结论");
    expect(api.rememberCalls).toBe(1);
    expect(api.listCalls).toBe(2);
    expect(last().notice).toContain("已记住");
  });
});
