// @vitest-environment jsdom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it } from "vitest";
import { RunTimeline } from "../RunTimeline";
import { ModelStatusControl } from "../ModelStatusControl";
import { SettingsDialog } from "../SettingsDialog";
import { humanizeEvent } from "../../lib/humanize";
import { createSettingsApi } from "../../api/settings";
import type { HostClient } from "../../api/host";

Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
let container: HTMLDivElement | null = null;
let root: ReturnType<typeof createRoot> | null = null;
afterEach(async () => {
  if (root) await act(async () => root?.unmount());
  container?.remove();
  root = null;
  container = null;
});

async function click(element: Element | null): Promise<void> {
  expect(element).not.toBeNull();
  await act(async () => {
    element?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  });
}

async function enter(input: HTMLInputElement, value: string): Promise<void> {
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set?.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

describe("Responses 设置与续接隐私", () => {
  it("时间线详情不渲染密文续接对象且不修改事件", () => {
    const payload = { content: "正常回答", finish_reason: "end_turn", continuation: {
      schema_version: "1.0", output_items: [{ type: "reasoning", encrypted_content: "opaque-private-cipher" }],
    }};
    const event = { seq: 1, type: "model.result", run_id: "r", task_id: "t", ts: 1, actor_kind: "core", payload };
    const html = renderToStaticMarkup(<RunTimeline entries={[humanizeEvent(event)]} hasMore={false} live={false} />);
    expect(html).toContain("正常回答");
    expect(html).not.toContain("opaque-private-cipher");
    expect(html).not.toContain("output_items");
    expect(payload.continuation.output_items[0]?.encrypted_content).toBe("opaque-private-cipher");
  });
  it("配置 RPC 保留独立 mode 与显式 model ID", async () => {
    const calls: Record<string, unknown>[] = [];
    const client = { rpc: async (_method: string, params: Record<string, unknown>) => {
      calls.push(params); return {model: {...params, summary: "Responses"}, credentials: []};
    }} as unknown as HostClient;
    const result = await createSettingsApi(client).configureModel({ mode: "openai-responses", model_name: "explicit-model", base_url: "", credential_backend: "env" });
    expect(result.model.mode).toBe("openai-responses");
    expect(calls[0]?.["model_name"]).toBe("explicit-model");
  });

  it.each(["quick", "settings"] as const)(
    "%s 入口可选择 Responses 并要求显式模型名",
    async (entry) => {
      const configured: Record<string, unknown>[] = [];
      const initial = {
        mode: "scripted", model_name: "", base_url: "",
        credential_backend: "env", summary: "离线",
      };
      const client = {
        rpc: async (method: string, params: Record<string, unknown> = {}) => {
          if (method === "model.configure") configured.push(params);
          return { model: { ...initial, ...params }, credentials: [] };
        },
      } as unknown as HostClient;
      const api = createSettingsApi(client);
      container = document.createElement("div");
      document.body.append(container);
      root = createRoot(container);
      await act(async () => root?.render(
        entry === "quick"
          ? <ModelStatusControl testId="responses" model={initial} settingsApi={api}
              onOpenModelSettings={() => undefined} />
          : <SettingsDialog open api={api} onClose={() => undefined} />,
      ));
      if (entry === "quick") {
        await click(container.querySelector('[data-testid="responses"]'));
        await click(container.querySelector('[data-model-mode="openai-responses"]'));
      } else {
        const select = container.querySelector<HTMLSelectElement>('select[aria-label="模型协议"]');
        expect(select?.querySelector('option[value="openai-responses"]')).not.toBeNull();
        await act(async () => {
          if (select) select.value = "openai-responses";
          select?.dispatchEvent(new Event("change", { bubbles: true }));
        });
      }
      const input = container.querySelector<HTMLInputElement>(
        entry === "quick" ? '[aria-label="快速切换模型 ID"]' : 'input[placeholder*="必填"]',
      );
      expect(input).not.toBeNull();
      expect(input?.value).toBe("");
      const apply = () => entry === "quick"
        ? container?.querySelector('[data-testid="quick-model-apply-btn"]') ?? null
        : Array.from(container?.querySelectorAll("button") ?? [])
          .find((button) => button.textContent?.includes("保存模型")) ?? null;
      await click(apply());
      expect(configured).toEqual([]);
      expect(container.textContent).toContain("模型");
      await enter(input!, "explicit-model");
      await click(apply());
      expect(configured).toEqual([{
        mode: "openai-responses", model_name: "explicit-model",
        base_url: "", credential_backend: "env",
      }]);
    },
  );
});
