// @vitest-environment jsdom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it } from "vitest";
import type { HostClient } from "../../api/host";
import { createSettingsApi } from "../../api/settings";
import { createDemoHostClient } from "../../demo/host";
import { SettingsDialog } from "../SettingsDialog";

it("只展示环境变量状态，模型和档案保存 env，不提供密钥输入或写入 API", async () => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean })
    .IS_REACT_ACT_ENVIRONMENT = true;
  const calls: { method: string; params: Record<string, unknown> }[] = [];
  const host: HostClient = {
    mode: "host",
    async getState() { throw new Error("unused"); },
    async openEvents() { return; },
    async rpc<T>(method: string, params: Record<string, unknown> = {}): Promise<T> {
      calls.push({ method, params });
      return {
        model: { mode: "openai", model_name: "test", base_url: "", credential_backend: "env" },
        credentials: [
          { provider: "openai", backend: "env", env_var: "TIANSHU_API_KEY", configured: true },
          { provider: "anthropic", backend: "env", env_var: "ANTHROPIC_API_KEY", configured: false },
        ],
        profiles: {},
      } as T;
    },
  };
  const api = createSettingsApi(host);
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  try {
    await act(async () => root.render(<SettingsDialog open api={api} onClose={() => undefined} />));
    expect(container.textContent).not.toMatch(/Keychain|钥匙串/);
    expect(container.querySelector('input[type="password"]')).toBeNull();
    expect(container.querySelector('[aria-label="凭据读取来源"]')).toBeNull();
    expect(container.textContent).toContain("TIANSHU_API_KEY");
    expect(container.textContent).toContain("ANTHROPIC_API_KEY");
    expect(container.textContent).toContain("已配置");
    expect(container.textContent).toContain("未设置");
    const save = Array.from(container.querySelectorAll("button"))
      .find((button) => button.textContent === "保存模型设置")!;
    await act(async () => save.click());
    expect(calls.find((call) => call.method === "model.configure")?.params.credential_backend).toBe("env");
    const profile = container.querySelector<HTMLInputElement>('input[placeholder="例如 Research"]')!;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(profile, "Main");
      profile.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => Array.from(container.querySelectorAll("button"))
      .find((button) => button.textContent === "将上方配置存为档案")!.click());
    expect(calls.find((call) => call.method === "model.profiles.set")?.params)
      .toMatchObject({ name: "Main", config: { credential_backend: "env" } });
    expect("setCredential" in api).toBe(false);
    expect("deleteCredential" in api).toBe(false);
    expect(calls.some((call) => call.method.startsWith("credential."))).toBe(false);
  } finally {
    await act(async () => root.unmount());
    container.remove();
  }
});

it("演示宿主也不伪装密钥写入成功", async () => {
  const host = createDemoHostClient();
  const settings = await createSettingsApi(host).getSettings();
  expect(settings.model.credential_backend).toBe("env");
  expect(settings.credentials.every((item) => item.backend === "env" && !item.writable)).toBe(true);
  await expect(host.rpc("credential.set", { provider: "openai", secret: "test-secret" }))
    .rejects.toThrow();
  await expect(host.rpc("credential.delete", { provider: "openai" })).rejects.toThrow();
});
