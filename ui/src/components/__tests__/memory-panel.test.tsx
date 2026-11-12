import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import type { MemorySearchResult, MemoryView } from "../../api/types";
import type { MemoryPanelState } from "../../lib/memoryController";
import { MemoryPanelView } from "../MemoryPanel";

function view(overrides: Partial<MemoryView> = {}): MemoryView {
  return {
    memory_id: "mem_1",
    revision: 1,
    scope: "user",
    scope_id: "local",
    kind: "preference",
    content_ref: "sha256:abc",
    origin: "user_confirmed",
    status: "candidate",
    source_refs: ["seq:3"],
    proof_count: 1,
    layer: "l2",
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

function state(overrides: Partial<MemoryPanelState> = {}): MemoryPanelState {
  return {
    phase: "ready",
    items: [view()],
    result: null,
    query: "",
    scope: { scope: "user", scope_id: null },
    notice: null,
    error: null,
    busy: null,
    ...overrides,
  };
}

const RESULT: MemorySearchResult = {
  items: [view()],
  scopes: ["memory://user/local"],
  query: "周报",
  semantic: false,
  degraded: "未配置向量 provider",
  fused: false,
  limit: 5,
};

function render(panelState: MemoryPanelState): string {
  return renderToStaticMarkup(
    <MemoryPanelView
      state={panelState}
      onSearch={() => undefined}
      onRefresh={() => undefined}
      onScopeChange={() => undefined}
      onOpenContent={() => undefined}
      onConfirm={() => undefined}
      onForget={() => undefined}
      onErase={() => undefined}
      onRemember={() => undefined}
    />,
  );
}

describe("记忆面板渲染", () => {
  it("六项判据与「凭什么」在主界面可见（不需要点开）", () => {
    const html = render(state());
    expect(html).toContain("候选");
    expect(html).toContain("你确认过的");
    expect(html).toContain("带证据的归纳");
    expect(html).toContain("来源 1 条 · 证明 1 条");
    expect(html).toContain("r1");
    expect(html).toContain("确认并启用");
  });

  it("正文是**纯文本**：XSS 载荷被转义，不进 DOM", () => {
    const payload = '<img src=x onerror="alert(1)"><script>alert(2)</script>';
    const html = render(state({ items: [view({ content: payload })] }));
    expect(html).not.toContain("<img");
    expect(html).not.toContain("<script>");
    expect(html).toContain("&lt;img");
  });

  it("检索的能力标注照实显示（只有词面召回 + 原因 + 可见作用域）", () => {
    const html = render(state({ result: RESULT, items: RESULT.items }));
    expect(html).toContain("只有词面召回");
    expect(html).toContain("未配置向量 provider");
    expect(html).toContain("memory://user/local");
  });

  it("空结果两种含义分开说：没记忆 vs 没命中", () => {
    const noMemory = render(state({ items: [] }));
    expect(noMemory).toContain("还没有记忆");
    const noHit = render(state({ items: [], result: RESULT }));
    expect(noHit).toContain("没有命中");
    expect(noHit).toContain("只有词面召回");
  });

  it("stale 三态：判不了就不显示「一致」", () => {
    const unknown = render(state({ items: [view({ stale: undefined })] }));
    expect(unknown).not.toContain("与最新证据一致");
    const stale = render(state({ items: [view({ stale: true })] }));
    expect(stale).toContain("过时");
  });

  it("错误与提示分别是 alert / status，不混成一条", () => {
    const html = render(state({ error: "连不上守护进程", notice: null }));
    expect(html).toContain('role="alert"');
    expect(html).toContain("连不上守护进程");
  });

  it("已忘记的记忆不再提供「忘记」，已擦除的不提供「删除」", () => {
    const html = render(state({ items: [view({ status: "forgotten" })] }));
    expect(html).toContain("忘记");
    expect(html).not.toContain("确认并启用");
    const erased = render(
      state({ items: [view({ status: "forgotten", erased_at: 10, content: null })] }),
    );
    // 只断言**行内按钮**：下面的"亲手记住一条"说明里也会出现"删除正文副本"这几个字。
    expect(erased).not.toContain('btn-danger btn-sm">删除正文副本');
  });

  it("删除按钮写的是「删除正文副本」，与「忘记」（停召回）分开", () => {
    const html = render(state());
    expect(html).toContain('btn-danger btn-sm">删除正文副本');
    expect(html).toContain("事件日志里的读快照会保留");
  });
});
