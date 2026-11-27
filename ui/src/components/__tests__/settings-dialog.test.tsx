import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import type { HostClient } from "../../api/host";
import { createSettingsApi } from "../../api/settings";
import { createDemoHostClient } from "../../demo/host";
import { AppHeader } from "../AppHeader";
import { SettingsDialog } from "../SettingsDialog";

describe("SettingsApi & SettingsDialog", () => {
  it("normalizes settings, credentials, schedules, budget, and mcp without echoing secrets", async () => {
    const calls: { method: string; params: Record<string, unknown> }[] = [];
    const fakeHost: HostClient = {
      mode: "host",
      async getState() {
        throw new Error("unused");
      },
      async openEvents() {
        return;
      },
      async rpc<T>(method: string, params: Record<string, unknown> = {}): Promise<T> {
        calls.push({ method, params });
        if (method === "settings.get" || method === "model.configure") {
          return {
            model: {
              mode: (params["mode"] as string) ?? "openai",
              model_name: (params["model_name"] as string) ?? "gpt-4o-mini",
              base_url: "",
              credential_backend: "env",
              summary: "openai · gpt-4o-mini",
            },
            credentials: [
              {
                provider: "openai",
                capability_class: "llm",
                backend: "env",
                active_backend: true,
                implemented: true,
                secure_storage: false,
                writable: false,
                configured: true,
                env_var: "TIANSHU_API_KEY",
                error: null,
              },
            ],
          } as T;
        }
        if (method === "schedule.list") {
          return {
            schedules: [
              {
                schedule_id: "sched_1",
                goal: "每日检查",
                workspaces: [{ root_path: "/tmp/ws", mode: "writable" }],
                interval_seconds: 3600,
                next_fire_at: 1000,
                enabled: true,
                created_at: 500,
                last_fired_at: null,
                last_error: "",
              },
            ],
          } as T;
        }
        if (method === "budget.view") {
          return {
            dimensions: ["tokens", "micros"],
            accounts: [
              {
                account_id: "acct_root",
                limits: { tokens: 10000 },
                settled: { tokens: 1200, micros: 4500 },
                reserved: { tokens: 300 },
                unknown_reservations: 1,
                cancelled: false,
                overrun: false,
                bound: "estimate",
              },
            ],
            unsettled_count: 1,
          } as T;
        }
        return {
          configured: false,
          reason: "未配置 MCP",
          servers: [],
          tools: [],
          rejected_tools: [],
        } as T;
      },
    };

    const api = createSettingsApi(fakeHost);
    const settings = await api.getSettings();
    expect(settings.model.mode).toBe("openai");
    expect(settings.credentials[0]?.configured).toBe(true);

    const schedules = await api.listSchedules();
    expect(schedules).toHaveLength(1);
    expect(schedules[0]?.goal).toBe("每日检查");
    expect(schedules[0]?.trigger).toEqual({ kind: "interval" });

    const budget = await api.getBudgetOverview();
    expect(budget.unsettled_count).toBe(1);
    expect(budget.accounts[0]?.settled["tokens"]).toBe(1200);

    const mcp = await api.getMcpOverview();
    expect(mcp.configured).toBe(false);
    expect(calls.length).toBe(4);
  });

  it("binds a watched directory read-only and preserves the real trigger shape", async () => {
    const received: Record<string, unknown>[] = [];
    const api = createSettingsApi({
      mode: "host",
      async getState() { throw new Error("unused"); },
      async openEvents() { return; },
      async rpc<T>(method: string, params: Record<string, unknown> = {}): Promise<T> {
        expect(method).toBe("schedule.create");
        received.push(params);
        return { schedule: { ...params, schedule_id: "sched_1", next_fire_at: null } } as T;
      },
    });
    const file = await api.createSchedule({
      goal: "整理收件目录",
      workspace: "/tmp/output",
      trigger: { kind: "file_arrival", directory: "/tmp/inbox" },
    });
    expect(received[0]).toEqual({
      goal: "整理收件目录",
      workspaces: [
        { root_path: "/tmp/output", mode: "writable" },
        { root_path: "/tmp/inbox", mode: "readonly" },
      ],
      trigger: { kind: "file_arrival", directory: "/tmp/inbox" },
    });
    expect(file?.trigger).toEqual({ kind: "file_arrival", directory: "/tmp/inbox" });
    expect(file?.interval_seconds).toBeNull();
    const event = await api.createSchedule({ goal: "本机事件", workspace: "", trigger: { kind: "event" } });
    expect(received[1]).toEqual({ goal: "本机事件", workspaces: [], trigger: { kind: "event" } });
    expect(event?.next_fire_at).toBeNull();
  });

  it("offers four trigger types in the schedule pane", () => {
    const html = renderToStaticMarkup(
      <SettingsDialog open initialTab="schedule" api={createSettingsApi(createDemoHostClient())}
        onClose={() => undefined} />,
    );
    expect(html).toContain('<option value="interval"');
    expect(html).toContain('<option value="calendar"');
    expect(html).toContain('<option value="file_arrival"');
    expect(html).toContain('<option value="event"');
  });

  it("renders SettingsDialog tabs and AppHeader settings entry cleanly", () => {
    const demoApi = createSettingsApi(createDemoHostClient());
    const dialogHtml = renderToStaticMarkup(
      <SettingsDialog open api={demoApi} onClose={() => undefined} />,
    );
    expect(dialogHtml).toContain("设置与运行管理");
    expect(dialogHtml).toContain("模型与密钥");
    expect(dialogHtml).toContain("定时任务");
    expect(dialogHtml).toContain("预算账本");
    expect(dialogHtml).toContain("MCP 服务");

    const headerHtml = renderToStaticMarkup(
      <AppHeader
        daemon={{ version: "0.1.0", started_at: 1000, data_root: "/tmp/data" }}
        streamStatus="live"
        streamDetail={null}
        demo={false}
        connectionLost={false}
        now={2000}
        onRefresh={() => undefined}
        onOpenMemory={() => undefined}
        onOpenSettings={() => undefined}
      />,
    );
    expect(headerHtml).toContain("记忆");
    expect(headerHtml).toContain("设置");
  });
});
