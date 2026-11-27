/**
 * 首日设置、凭据状态、定时任务、预算账本与 MCP 状态的控制面调用封装。
 *
 * 仅管理非敏感配置和凭据状态，不提供密钥输入或写入接口。
 */

import type { HostClient } from "./host";
import { RPC } from "./types";
import { asArray, asBoolean, asNumber, asRecord, readString } from "../lib/values";

export interface ModelProfileInput {
  mode: string;
  model_name: string;
  base_url: string;
  credential_backend: string;
  allow_image_attachments?: boolean;
  compact_threshold?: number;
}

export interface ModelConfigView extends ModelProfileInput {
  mode: "scripted" | "openai" | "anthropic" | string;
  model_name: string;
  base_url: string;
  credential_backend: "env" | string;
  summary: string;
}

export interface CredentialStatusView {
  provider: string;
  capability_class: string;
  backend: string;
  active_backend: boolean;
  implemented: boolean;
  secure_storage: boolean;
  writable: boolean;
  configured: boolean;
  env_var: string | null;
  error: string | null;
}

export interface SettingsView {
  model: ModelConfigView;
  credentials: CredentialStatusView[];
  profiles?: Record<string, ModelProfileInput>;
}

export type ScheduleTrigger =
  | { kind: "interval" }
  | { kind: "calendar"; time: string; timezone: string; weekdays: number[] }
  | { kind: "file_arrival"; directory: string }
  | { kind: "event" };

export type ScheduleInput = { goal: string; workspace: string } & (
  | { interval_seconds: number }
  | { trigger: Exclude<ScheduleTrigger, { kind: "interval" }> }
);

export interface ScheduleView {
  schedule_id: string;
  goal: string;
  workspaces: { root_path: string; mode: string }[];
  trigger: ScheduleTrigger;
  interval_seconds: number | null;
  next_fire_at: number | null;
  enabled: boolean;
  created_at: number;
  last_fired_at: number | null;
  last_error: string;
}

export interface BudgetAccountUsageView {
  account_id: string;
  limits: Record<string, number>;
  settled: Record<string, number>;
  reserved: Record<string, number>;
  unknown_reservations: number;
  cancelled: boolean;
  overrun: boolean;
  bound: string;
}

export interface BudgetOverviewView {
  dimensions: string[];
  accounts: BudgetAccountUsageView[];
  unsettled_count: number;
}

export interface McpServerStateView {
  name: string;
  status: string;
  protocol_version: string | null;
  tools: string[];
  tools_truncated: boolean;
  diagnostics: string[];
}

export interface McpOverviewView {
  configured: boolean;
  reason: string | null;
  servers: McpServerStateView[];
  tools: { tool_id: string; server: string; tool: string }[];
  rejected_tools: { server: string; tool: string; reason: string }[];
}

export interface DiagnosticSectionItem {
  section: string;
  included: boolean;
  description: string;
  record_count: number;
  estimated_bytes: number;
}

export interface DiagnosticManifestView {
  redacted: boolean;
  local_only: boolean;
  sections: DiagnosticSectionItem[];
}

export interface DiagnosticExportResult {
  archive_path: string;
  manifest: DiagnosticManifestView;
}

export interface SettingsApi {
  getSettings(): Promise<SettingsView>;
  configureModel(input: ModelProfileInput): Promise<SettingsView>;
  saveModelProfile?(name: string, config: ModelProfileInput): Promise<SettingsView>;
  removeModelProfile?(name: string): Promise<SettingsView>;
  listSchedules(): Promise<ScheduleView[]>;
  createSchedule(input: ScheduleInput): Promise<ScheduleView | null>;
  setScheduleEnabled(scheduleId: string, enabled: boolean): Promise<ScheduleView | null>;
  removeSchedule(scheduleId: string): Promise<void>;
  getBudgetOverview(): Promise<BudgetOverviewView>;
  getMcpOverview(): Promise<McpOverviewView>;
  previewDiagnostics(sections?: string[]): Promise<DiagnosticManifestView>;
  exportDiagnostics(outDir: string, sections: string[]): Promise<DiagnosticExportResult>;
}

function normalizeCredential(raw: unknown): CredentialStatusView | null {
  const rec = asRecord(raw);
  const provider = readString(rec, "provider");
  const backend = readString(rec, "backend");
  if (provider === null || backend === null) return null;
  return {
    provider,
    capability_class: readString(rec, "capability_class") ?? "llm",
    backend,
    active_backend: asBoolean(rec["active_backend"]) === true,
    implemented: asBoolean(rec["implemented"]) === true,
    secure_storage: asBoolean(rec["secure_storage"]) === true,
    writable: asBoolean(rec["writable"]) === true,
    configured: asBoolean(rec["configured"]) === true,
    env_var: readString(rec, "env_var"),
    error: readString(rec, "error"),
  };
}

function normalizeProfile(rec: Record<string, unknown>): ModelProfileInput {
  const threshold = asNumber(rec["compact_threshold"]);
  return {
    mode: readString(rec, "mode") ?? "scripted",
    model_name: readString(rec, "model_name") ?? "",
    base_url: readString(rec, "base_url") ?? "",
    credential_backend: readString(rec, "credential_backend") ?? "env",
    ...(rec["allow_image_attachments"] === true ? { allow_image_attachments: true } : {}),
    ...(threshold === null ? {} : { compact_threshold: threshold }),
  };
}

export function normalizeSettings(raw: unknown): SettingsView {
  const rec = asRecord(raw);
  const modelRec = asRecord(rec["model"]);
  const model = normalizeProfile(modelRec);
  return {
    model: {
      ...model,
      summary: readString(modelRec, "summary") ??
        (model.mode === "scripted" ? "演示脚本模型（离线）" : `${model.mode} · ${model.model_name}`),
    },
    profiles: Object.fromEntries(Object.entries(asRecord(rec["profiles"]))
      .map(([name, profile]) => [name, normalizeProfile(asRecord(profile))])),
    credentials: asArray(rec["credentials"])
      .map(normalizeCredential)
      .filter((item): item is CredentialStatusView => item !== null),
  };
}

function normalizeScheduleTrigger(raw: unknown, interval: number | null): ScheduleTrigger | null {
  const rec = asRecord(raw);
  switch (rec["kind"]) {
    case "interval": return interval === null ? null : { kind: "interval" };
    case "calendar": {
      const time = readString(rec, "time");
      const timezone = readString(rec, "timezone");
      const weekdays = asArray(rec["weekdays"]).filter((day): day is number => typeof day === "number");
      return time && timezone && weekdays.length ? { kind: "calendar", time, timezone, weekdays } : null;
    }
    case "file_arrival": {
      const directory = readString(rec, "directory");
      return directory ? { kind: "file_arrival", directory } : null;
    }
    case "event": return { kind: "event" };
    default: return raw === undefined && interval !== null ? { kind: "interval" } : null;
  }
}

function normalizeSchedule(raw: unknown): ScheduleView | null {
  const rec = asRecord(raw);
  const id = readString(rec, "schedule_id");
  const interval = asNumber(rec["interval_seconds"]);
  const trigger = normalizeScheduleTrigger(rec["trigger"], interval);
  if (id === null || trigger === null) return null;
  const workspaces = asArray(rec["workspaces"]).flatMap((ws) => {
    const wrec = asRecord(ws);
    const root = readString(wrec, "root_path");
    if (root === null) return [];
    return [{ root_path: root, mode: readString(wrec, "mode") ?? "writable" }];
  });
  return {
    schedule_id: id,
    goal: readString(rec, "goal") ?? "",
    workspaces,
    trigger,
    interval_seconds: interval,
    next_fire_at: asNumber(rec["next_fire_at"]),
    enabled: asBoolean(rec["enabled"]) !== false,
    created_at: asNumber(rec["created_at"]) ?? 0,
    last_fired_at: asNumber(rec["last_fired_at"]),
    last_error: readString(rec, "last_error") ?? "",
  };
}

function normalizeNumberMap(raw: unknown): Record<string, number> {
  const rec = asRecord(raw);
  const out: Record<string, number> = {};
  for (const [key, val] of Object.entries(rec)) {
    const num = asNumber(val);
    if (num !== null) out[key] = num;
  }
  return out;
}

function normalizeDiagManifest(raw: unknown): DiagnosticManifestView {
  const rec = asRecord(raw);
  const sections = asArray(rec["sections"]).flatMap((item) => {
    const srec = asRecord(item);
    const section = readString(srec, "section");
    if (section === null) return [];
    return [
      {
        section,
        included: asBoolean(srec["included"]) !== false,
        description: readString(srec, "description") ?? "",
        record_count: asNumber(srec["record_count"]) ?? 0,
        estimated_bytes: asNumber(srec["estimated_bytes"]) ?? 0,
      },
    ];
  });
  return {
    redacted: asBoolean(rec["redacted"]) !== false,
    local_only: asBoolean(rec["local_only"]) !== false,
    sections,
  };
}

export function createSettingsApi(client: HostClient): SettingsApi {
  return {
    async getSettings() {
      return normalizeSettings(await client.rpc<unknown>(RPC.settingsGet, {}));
    },
    async saveModelProfile(name, config) {
      await client.rpc(RPC.modelProfileSet, { name, config });
      return normalizeSettings(await client.rpc(RPC.settingsGet, {}));
    },
    async removeModelProfile(name) {
      await client.rpc(RPC.modelProfileRemove, { name });
      return normalizeSettings(await client.rpc(RPC.settingsGet, {}));
    },
    async configureModel(input) {
      return normalizeSettings(await client.rpc<unknown>(RPC.modelConfigure, { ...input }));
    },
    async listSchedules() {
      const raw = asRecord(await client.rpc<unknown>(RPC.scheduleList, {}));
      return asArray(raw["schedules"])
        .map(normalizeSchedule)
        .filter((item): item is ScheduleView => item !== null);
    },
    async createSchedule(input) {
      const workspaces =
        input.workspace.trim() === ""
          ? []
          : [{ root_path: input.workspace.trim(), mode: "writable" }];
      if ("trigger" in input && input.trigger.kind === "file_arrival") {
        workspaces.push({ root_path: input.trigger.directory, mode: "readonly" });
      }
      const triggerInput = "trigger" in input
        ? { trigger: input.trigger }
        : { interval_seconds: input.interval_seconds };
      const raw = asRecord(
        await client.rpc<unknown>(RPC.scheduleCreate, {
          goal: input.goal,
          workspaces,
          ...triggerInput,
        }),
      );
      return normalizeSchedule(raw["schedule"]);
    },
    async setScheduleEnabled(scheduleId, enabled) {
      const raw = asRecord(
        await client.rpc<unknown>(RPC.scheduleSetEnabled, {
          schedule_id: scheduleId,
          enabled,
        }),
      );
      return normalizeSchedule(raw["schedule"]);
    },
    async removeSchedule(scheduleId) {
      await client.rpc<unknown>(RPC.scheduleRemove, { schedule_id: scheduleId });
    },
    async getBudgetOverview() {
      const raw = asRecord(await client.rpc<unknown>(RPC.budgetView, {}));
      const accounts = asArray(raw["accounts"]).flatMap((item) => {
        const rec = asRecord(item);
        const accountId = readString(rec, "account_id");
        if (accountId === null) return [];
        return [
          {
            account_id: accountId,
            limits: normalizeNumberMap(rec["limits"]),
            settled: normalizeNumberMap(rec["settled"]),
            reserved: normalizeNumberMap(rec["reserved"]),
            unknown_reservations: asNumber(rec["unknown_reservations"]) ?? 0,
            cancelled: asBoolean(rec["cancelled"]) === true,
            overrun: asBoolean(rec["overrun"]) === true,
            bound: readString(rec, "bound") ?? "",
          },
        ];
      });
      return {
        dimensions: asArray(raw["dimensions"]).filter(
          (d): d is string => typeof d === "string",
        ),
        accounts,
        unsettled_count: asNumber(raw["unsettled_count"]) ?? 0,
      };
    },
    async getMcpOverview() {
      const raw = asRecord(await client.rpc<unknown>(RPC.mcpList, {}));
      const servers = asArray(raw["servers"]).flatMap((item) => {
        const rec = asRecord(item);
        const name = readString(rec, "name");
        if (name === null) return [];
        return [
          {
            name,
            status: readString(rec, "status") ?? "unknown",
            protocol_version: readString(rec, "protocol_version"),
            tools: asArray(rec["tools"]).filter((t): t is string => typeof t === "string"),
            tools_truncated: asBoolean(rec["tools_truncated"]) === true,
            diagnostics: asArray(rec["diagnostics"]).filter(
              (d): d is string => typeof d === "string",
            ),
          },
        ];
      });
      const tools = asArray(raw["tools"]).flatMap((item) => {
        const rec = asRecord(item);
        const toolId = readString(rec, "tool_id");
        if (toolId === null) return [];
        return [
          {
            tool_id: toolId,
            server: readString(rec, "server") ?? "",
            tool: readString(rec, "tool") ?? "",
          },
        ];
      });
      const rejectedTools = asArray(raw["rejected_tools"]).flatMap((item) => {
        const rec = asRecord(item);
        const tool = readString(rec, "tool") ?? readString(rec, "name");
        if (tool === null) return [];
        return [
          {
            server: readString(rec, "server") ?? "",
            tool,
            reason: readString(rec, "reason") ?? "",
          },
        ];
      });
      return {
        configured: asBoolean(raw["configured"]) === true,
        reason: readString(raw["reason"] ? raw : { reason: raw["reason"] }, "reason"),
        servers,
        tools,
        rejected_tools: rejectedTools,
      };
    },
    async previewDiagnostics(sections) {
      const params: Record<string, unknown> = {};
      if (sections !== undefined) params["sections"] = sections;
      const raw = asRecord(await client.rpc<unknown>(RPC.diagnosticsPreview, params));
      return normalizeDiagManifest(raw["manifest"]);
    },
    async exportDiagnostics(outDir, sections) {
      const raw = asRecord(
        await client.rpc<unknown>(RPC.diagnosticsExport, {
          out_dir: outDir,
          sections,
          redact_sensitive: true,
        }),
      );
      return {
        archive_path: readString(raw, "archive_path") ?? "",
        manifest: normalizeDiagManifest(raw["manifest"]),
      };
    },
  };
}
