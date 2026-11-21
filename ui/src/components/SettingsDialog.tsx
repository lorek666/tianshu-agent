import { useCallback, useEffect, useState } from "react";
import { humanizeHostError } from "../api/errors";
import type {
  BudgetOverviewView,
  CredentialStatusView,
  DiagnosticManifestView,
  McpOverviewView,
  ScheduleInput,
  ScheduleTrigger,
  ScheduleView,
  SettingsApi,
  SettingsView,
} from "../api/settings";
import type { StreamStatus } from "../api/stream";
import {
  EFFECTS_RUN_POLICY,
  PURE_ONLY_RUN_POLICY,
} from "../api/types";
import type { DaemonView, RunExecutionPolicyV1 } from "../api/types";
import { formatInt, formatRelative, formatUsdExact } from "../lib/format";
import { streamStatusText } from "./AppHeader";
import {
  IconBrain,
  IconChart,
  IconClock,
  IconCpu,
  IconMoon,
  IconRefresh,
  IconShield,
  IconSparkle,
  IconSun,
} from "./Icons";
import { WelcomePet } from "./WelcomePet";
import type { WelcomePetStatus } from "./WelcomePet";

export type ThemeChoice = "light" | "dark" | "system";

export type SettingsTab =
  | "models"
  | "security"
  | "schedule"
  | "budget"
  | "mcp"
  | "memory"
  | "diagnostics"
  | "appearance";

export interface UiPreferences {
  theme: ThemeChoice;
  enablePet: boolean;
  enableParallax: boolean;
  reduceMotion: boolean;
  defaultPolicy: RunExecutionPolicyV1;
  previewPose: WelcomePetStatus;
}

export interface SettingsDialogProps {
  open: boolean;
  api?: SettingsApi | null;
  initialTab?: SettingsTab;
  preferences?: UiPreferences;
  daemon?: DaemonView;
  streamStatus?: StreamStatus;
  streamDetail?: string | null;
  connectionLost?: boolean;
  eventSeqHead?: number;
  now?: number;
  onUpdatePreferences?: (patch: Partial<UiPreferences>) => void;
  onOpenMemory?: () => void;
  onRefresh?: () => void;
  onReconnectStream?: () => void;
  onSettingsSaved?: (summary: string, settings?: SettingsView) => void;
  onClose: () => void;
}

const DEFAULT_PREFERENCES: UiPreferences = {
  theme: "light",
  enablePet: true,
  enableParallax: true,
  reduceMotion: false,
  defaultPolicy: EFFECTS_RUN_POLICY,
  previewPose: "welcome",
};

const EMPTY_DAEMON: DaemonView = {
  version: null,
  started_at: null,
  data_root: null,
};

const POSE_OPTIONS: ReadonlyArray<{ key: WelcomePetStatus; label: string }> = [
  { key: "welcome", label: "欢迎" },
  { key: "idle", label: "待命" },
  { key: "thinking", label: "思考" },
  { key: "executing", label: "执行" },
  { key: "done", label: "完成" },
  { key: "error", label: "异常" },
  { key: "sleeping", label: "休眠" },
];

const SCHEDULE_KINDS = [
  { value: "interval", label: "固定间隔" },
  { value: "calendar", label: "按日历" },
  { value: "file_arrival", label: "目录新增文件" },
  { value: "event", label: "本机事件" },
] as const;

const WEEKDAY_NAMES = ["一", "二", "三", "四", "五", "六", "日"];

function scheduleLabel(item: ScheduleView): string {
  const trigger = item.trigger;
  switch (trigger.kind) {
    case "interval": return `每 ${item.interval_seconds} 秒`;
    case "calendar":
      return `周${trigger.weekdays.map((day) => WEEKDAY_NAMES[day - 1]).join("、")} ${trigger.time}（${trigger.timezone}）`;
    case "file_arrival": return "目录新增文件";
    case "event": return "等待本机事件";
  }
}

export function SettingsDialog({
  open,
  api = null,
  initialTab = "models",
  preferences = DEFAULT_PREFERENCES,
  daemon = EMPTY_DAEMON,
  streamStatus = "connecting",
  streamDetail = null,
  connectionLost = false,
  eventSeqHead = 0,
  now = Date.now(),
  onUpdatePreferences,
  onOpenMemory,
  onRefresh,
  onReconnectStream,
  onSettingsSaved,
  onClose,
}: SettingsDialogProps) {
  const [activeTab, setActiveTab] = useState<SettingsTab>(initialTab);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  // Model & Credentials state (real backend RPCs; secret input cleared immediately on save)
  const [allowImages, setAllowImages] = useState(false);
  const [compactThreshold, setCompactThreshold] = useState("");
  const [profileName, setProfileName] = useState("");
  const [profileNames, setProfileNames] = useState<string[]>([]);
  const [mode, setMode] = useState("scripted");
  const [modelName, setModelName] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [credBackend, setCredBackend] = useState("keychain");
  const [credentials, setCredentials] = useState<CredentialStatusView[]>([]);
  const [keyProvider, setKeyProvider] = useState<"openai" | "anthropic">("openai");
  const [keySecret, setKeySecret] = useState("");

  // Schedule state
  const [schedules, setSchedules] = useState<ScheduleView[]>([]);
  const [schedGoal, setSchedGoal] = useState("");
  const [schedWorkspace, setSchedWorkspace] = useState("");
  const [schedInterval, setSchedInterval] = useState("3600");
  const [schedKind, setSchedKind] = useState<ScheduleTrigger["kind"]>("interval");
  const [schedTime, setSchedTime] = useState("09:00");
  const [schedZone, setSchedZone] = useState("UTC");
  const [schedDays, setSchedDays] = useState("1,2,3,4,5");
  const [schedDirectory, setSchedDirectory] = useState("");

  // Budget, MCP & Diagnostics state
  const [budget, setBudget] = useState<BudgetOverviewView | null>(null);
  const [mcp, setMcp] = useState<McpOverviewView | null>(null);
  const [diagManifest, setDiagManifest] = useState<DiagnosticManifestView | null>(null);
  const [diagSelected, setDiagSelected] = useState<Record<string, boolean>>({});
  const [diagOutDir, setDiagOutDir] = useState("");

  useEffect(() => {
    if (open) setActiveTab(initialTab);
  }, [open, initialTab]);

  useEffect(() => {
    if (!open) return;
    const handleEsc = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handleEsc);
    return () => window.removeEventListener("keydown", handleEsc);
  }, [open, onClose]);

  const loadBackendTab = useCallback(
    async (target: SettingsTab, cancelled: { current: boolean }) => {
      if (
        api === null ||
        (target !== "models" &&
          target !== "schedule" &&
          target !== "budget" &&
          target !== "mcp" &&
          target !== "diagnostics")
      ) {
        return;
      }
      setBusy(true);
      setError(null);
      try {
        if (target === "models") {
          const s = await api.getSettings();
          if (cancelled.current) return;
          setMode(s.model.mode);
          setModelName(s.model.model_name);
          setBaseUrl(s.model.base_url);
          setCredBackend(s.model.credential_backend);
          setCredentials(s.credentials);
          setAllowImages(s.model.allow_image_attachments === true);
          setCompactThreshold(s.model.compact_threshold?.toString() ?? "");
          setProfileNames(Object.keys(s.profiles ?? {}));
        } else if (target === "schedule") {
          const list = await api.listSchedules();
          if (!cancelled.current) setSchedules(list);
        } else if (target === "budget") {
          const overview = await api.getBudgetOverview();
          if (!cancelled.current) setBudget(overview);
        } else if (target === "mcp") {
          const overview = await api.getMcpOverview();
          if (!cancelled.current) setMcp(overview);
        } else if (target === "diagnostics") {
          const manifest = await api.previewDiagnostics();
          if (cancelled.current) return;
          setDiagManifest(manifest);
          setDiagSelected((prev) => {
            if (Object.keys(prev).length > 0) return prev;
            const next: Record<string, boolean> = {};
            for (const item of manifest.sections) {
              next[item.section] = item.included;
            }
            return next;
          });
        }
      } catch (caught) {
        if (!cancelled.current) setError(humanizeHostError(caught));
      } finally {
        if (!cancelled.current) setBusy(false);
      }
    },
    [api],
  );

  useEffect(() => {
    if (!open) {
      setKeySecret("");
      setError(null);
      setNotice(null);
      return;
    }
    const cancelled = { current: false };
    void loadBackendTab(activeTab, cancelled);
    return () => {
      cancelled.current = true;
    };
  }, [open, activeTab, loadBackendTab]);

  if (!open) return null;

  const isHealthy =
    !connectionLost &&
    (streamStatus === "live" ||
      streamStatus === "connecting" ||
      streamStatus === "reconnecting" ||
      streamStatus === "polling");

  const modelInput = () => ({
    mode,
    model_name: mode === "scripted" ? "" : modelName.trim(),
    base_url: baseUrl.trim(),
    credential_backend: credBackend,
    ...(mode === "openai-responses" && allowImages ? { allow_image_attachments: true } : {}),
    ...(mode === "openai-responses" && compactThreshold.trim() !== ""
      ? { compact_threshold: Number(compactThreshold) } : {}),
  });

  const updateProfiles = async (remove?: string): Promise<void> => {
    if (!api) return;
    setBusy(true);
    setError(null);
    try {
      const updated = remove === undefined
        ? await api.saveModelProfile?.(profileName.trim(), modelInput())
        : await api.removeModelProfile?.(remove);
      if (!updated) return;
      setProfileNames(Object.keys(updated.profiles ?? {}));
      setProfileName("");
      onSettingsSaved?.(updated.model.summary, updated);
    } catch (caught) {
      setError(humanizeHostError(caught));
    } finally {
      setBusy(false);
    }
  };

  const handleSaveModel = async (): Promise<void> => {
    if (api === null) return;
    if (mode === "openai-responses" && modelName.trim() === "") {
      setError("Responses 模式必须显式填写模型名称。");
      return;
    }
    if (mode === "anthropic" && modelName.trim() === "") {
      setError("Anthropic 模式必须显式填写模型名称（例如 claude-sonnet-4-5）。");
      return;
    }
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const updated = await api.configureModel(modelInput());
      setMode(updated.model.mode);
      setModelName(updated.model.model_name);
      setBaseUrl(updated.model.base_url);
      setCredBackend(updated.model.credential_backend);
      setCredentials(updated.credentials);
      setAllowImages(updated.model.allow_image_attachments === true);
      setCompactThreshold(updated.model.compact_threshold?.toString() ?? "");
      setNotice(`已保存并切换当前模型：${updated.model.summary}`);
      onSettingsSaved?.(updated.model.summary, updated);
    } catch (caught) {
      setError(humanizeHostError(caught));
    } finally {
      setBusy(false);
    }
  };

  const handleSaveKey = async (): Promise<void> => {
    if (api === null) return;
    if (keySecret.trim() === "") {
      setError("请输入要存入系统钥匙串的 API Key。");
      return;
    }
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const next = await api.setCredential(keyProvider, keySecret.trim(), "keychain");
      setKeySecret("");
      setCredentials(next);
      setNotice(`已将 ${keyProvider} 密钥安全写入系统钥匙串（界面不回显明文）。`);
    } catch (caught) {
      setError(humanizeHostError(caught));
    } finally {
      setBusy(false);
    }
  };

  const handleDeleteKey = async (provider: string): Promise<void> => {
    if (api === null) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const next = await api.deleteCredential(provider, "keychain");
      setCredentials(next);
      setNotice(`已从系统钥匙串移除 ${provider} 密钥。`);
    } catch (caught) {
      setError(humanizeHostError(caught));
    } finally {
      setBusy(false);
    }
  };

  const handleCreateSchedule = async (): Promise<void> => {
    if (api === null) return;
    const common = { goal: schedGoal.trim(), workspace: schedWorkspace.trim() };
    if (common.goal === "") {
      setError("请填写定时任务的目标描述。");
      return;
    }
    let input: ScheduleInput;
    if (schedKind === "interval") {
      const interval = Number(schedInterval);
      if (!Number.isSafeInteger(interval) || interval < 1) {
        setError("触发间隔必须是大于等于 1 的整数秒。");
        return;
      }
      input = { ...common, interval_seconds: interval };
    } else if (schedKind === "calendar") {
      const weekdays = schedDays.split(/[,，]/u).map((day) => Number(day.trim()));
      if (!schedTime || !schedZone.trim() || weekdays.some((day) => !Number.isInteger(day) || day < 1 || day > 7)
          || new Set(weekdays).size !== weekdays.length) {
        setError("请填写时间、时区和不重复的星期。星期一为 1，星期日为 7，用逗号分隔。");
        return;
      }
      input = { ...common, trigger: { kind: "calendar", time: schedTime, timezone: schedZone.trim(), weekdays } };
    } else if (schedKind === "file_arrival") {
      const directory = schedDirectory.trim();
      if (!directory) {
        setError("请填写监听目录的绝对路径。");
        return;
      }
      input = { ...common, trigger: { kind: "file_arrival", directory } };
    } else {
      input = { ...common, trigger: { kind: "event" } };
    }
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await api.createSchedule(input);
      setSchedGoal("");
      setSchedWorkspace("");
      setSchedDirectory("");
      setSchedules(await api.listSchedules());
      setNotice("定时任务已创建，触发后会自动创建任务并入队运行。");
    } catch (caught) {
      setError(humanizeHostError(caught));
    } finally {
      setBusy(false);
    }
  };

  const handleToggleSchedule = async (item: ScheduleView): Promise<void> => {
    if (api === null) return;
    setBusy(true);
    setError(null);
    try {
      await api.setScheduleEnabled(item.schedule_id, !item.enabled);
      setSchedules(await api.listSchedules());
    } catch (caught) {
      setError(humanizeHostError(caught));
    } finally {
      setBusy(false);
    }
  };

  const handleRemoveSchedule = async (scheduleId: string): Promise<void> => {
    if (api === null) return;
    setBusy(true);
    setError(null);
    try {
      await api.removeSchedule(scheduleId);
      setSchedules(await api.listSchedules());
    } catch (caught) {
      setError(humanizeHostError(caught));
    } finally {
      setBusy(false);
    }
  };

  const handleExportDiagnostics = async (): Promise<void> => {
    if (api === null) return;
    const chosen = Object.entries(diagSelected)
      .filter(([, on]) => on)
      .map(([k]) => k);
    if (diagOutDir.trim() === "") {
      setError("请填写诊断包的本地导出目录路径。");
      return;
    }
    if (chosen.length === 0) {
      setError("请至少勾选一项要导出的诊断内容。");
      return;
    }
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const res = await api.exportDiagnostics(diagOutDir.trim(), chosen);
      setDiagManifest(res.manifest);
      setNotice(`已导出默认脱敏诊断包至本地：${res.archive_path}`);
    } catch (caught) {
      setError(humanizeHostError(caught));
    } finally {
      setBusy(false);
    }
  };

  const keychainItems = credentials.filter((c) => c.backend === "keychain");
  const envItems = credentials.filter((c) => c.backend === "env");
  const keychainWritable = keychainItems.some((c) => c.writable);

  const selectTab = (next: SettingsTab): void => {
    setError(null);
    setNotice(null);
    setActiveTab(next);
  };

  const closeButton = (
    <button
      type="button"
      className="chip-btn"
      data-testid="close-settings-btn"
      onClick={onClose}
      disabled={busy}
    >
      完成
    </button>
  );

  return (
    <div
      className="modal-mask"
      role="dialog"
      aria-modal="true"
      aria-label="系统设置"
      onClick={busy ? undefined : onClose}
    >
      <div
        className="settings-dialog"
        onClick={(event) => event.stopPropagation()}
      >
        <nav className="settings-nav" role="tablist" aria-label="设置分类">
          <div className="settings-nav-title">设置与运行管理</div>

          <button
            type="button"
            role="tab"
            aria-selected={activeTab === "models"}
            className={`settings-tab-btn${activeTab === "models" ? " is-active" : ""}`}
            data-settings-tab="models"
            onClick={() => selectTab("models")}
          >
            <IconSparkle size={15} />
            <span>模型与密钥</span>
          </button>

          <button
            type="button"
            role="tab"
            aria-selected={activeTab === "security"}
            className={`settings-tab-btn${activeTab === "security" ? " is-active" : ""}`}
            data-settings-tab="security"
            onClick={() => selectTab("security")}
          >
            <IconShield size={15} />
            <span>安全与执行授权</span>
          </button>

          <button
            type="button"
            role="tab"
            aria-selected={activeTab === "schedule"}
            className={`settings-tab-btn${activeTab === "schedule" ? " is-active" : ""}`}
            data-settings-tab="schedule"
            onClick={() => selectTab("schedule")}
          >
            <IconClock size={15} />
            <span>定时任务</span>
          </button>

          <button
            type="button"
            role="tab"
            aria-selected={activeTab === "budget"}
            className={`settings-tab-btn${activeTab === "budget" ? " is-active" : ""}`}
            data-settings-tab="budget"
            onClick={() => selectTab("budget")}
          >
            <IconChart size={15} />
            <span>预算账本</span>
          </button>

          <button
            type="button"
            role="tab"
            aria-selected={activeTab === "mcp"}
            className={`settings-tab-btn${activeTab === "mcp" ? " is-active" : ""}`}
            data-settings-tab="mcp"
            onClick={() => selectTab("mcp")}
          >
            <IconCpu size={15} />
            <span>MCP 服务</span>
          </button>

          <button
            type="button"
            role="tab"
            aria-selected={activeTab === "memory"}
            className={`settings-tab-btn${activeTab === "memory" ? " is-active" : ""}`}
            data-settings-tab="memory"
            onClick={() => selectTab("memory")}
          >
            <IconBrain size={15} />
            <span>记忆与上下文</span>
          </button>

          <button
            type="button"
            role="tab"
            aria-selected={activeTab === "diagnostics"}
            className={`settings-tab-btn${activeTab === "diagnostics" ? " is-active" : ""}`}
            data-settings-tab="diagnostics"
            onClick={() => selectTab("diagnostics")}
          >
            <IconRefresh size={15} />
            <span>诊断与数据服务</span>
          </button>

          <button
            type="button"
            role="tab"
            aria-selected={activeTab === "appearance"}
            className={`settings-tab-btn${activeTab === "appearance" ? " is-active" : ""}`}
            data-settings-tab="appearance"
            onClick={() => selectTab("appearance")}
          >
            <IconSun size={15} />
            <span>外观与伴侣动效</span>
          </button>
        </nav>

        <div className="settings-body">
          {error !== null ? (
            <p className="banner banner-danger settings-feedback" role="alert">
              {error}
            </p>
          ) : null}
          {notice !== null ? (
            <p className="banner settings-feedback" role="status">
              {notice}
            </p>
          ) : null}

          {activeTab === "models" ? (
            <>
              <div className="settings-head">
                <div>
                  <h2>模型与密钥</h2>
                  <p>热切换本机守护进程模型协议与 Model ID，并将 API Key 安全写入系统钥匙串</p>
                </div>
                {closeButton}
              </div>

              {api === null ? (
                <p className="field-hint">尚未连接守护进程设置接口。</p>
              ) : (
                <>
                  <div className="setting-card">
                    <div className="field">
                      <span className="field-label">模型协议</span>
                      <select
                        className="input"
                        aria-label="模型协议"
                        value={mode}
                        onChange={(e) => {
                          setMode(e.target.value);
                          setAllowImages(false);
                          setCompactThreshold("");
                        }}
                        disabled={busy}
                      >
                        <option value="scripted">离线演示脚本模型（不联网、不计费）</option>
                        <option value="openai">OpenAI 兼容协议（OpenAI / DeepSeek / Moonshot / 本地服务）</option>
                        <option value="openai-responses">OpenAI Responses 协议（文本与函数工具）</option>
                        <option value="anthropic">Anthropic Messages 协议（Claude）</option>
                      </select>
                    </div>

                    {mode !== "scripted" ? (
                      <>
                        <label className="field">
                          <span className="field-label">模型名称（Model ID）</span>
                          <input
                            className="input"
                            type="text"
                            value={modelName}
                            placeholder={
                              mode === "anthropic" ? "必填，例如 claude-sonnet-4-5"
                                : mode === "openai-responses" ? "必填：显式模型 ID" : "默认 gpt-4o-mini"
                            }
                            onChange={(e) => {
                              setModelName(e.target.value);
                              setAllowImages(false);
                              setCompactThreshold("");
                            }}
                            disabled={busy}
                          />
                        </label>

                        <label className="field">
                          <span className="field-label">自定义端点 Base URL（可选）</span>
                          <input
                            className="input"
                            type="text"
                            value={baseUrl}
                            placeholder="留空使用官方默认端点，例如 https://api.openai.com/v1"
                            onChange={(e) => {
                              setBaseUrl(e.target.value);
                              setAllowImages(false);
                              setCompactThreshold("");
                            }}
                            disabled={busy}
                          />
                        </label>

                        {mode === "openai-responses" ? (
                          <>
                            <label className="field">
                              <span className="field-label">
                                <input type="checkbox" checked={allowImages} disabled={busy}
                                  onChange={(event) => setAllowImages(event.target.checked)} />
                                此模型支持 PNG 和 JPEG 图片附件
                              </span>
                            </label>
                            <label className="field">
                              <span className="field-label">自动压缩阈值（令牌）</span>
                              <input className="input" type="number" min="1024" max="2000000"
                                value={compactThreshold} placeholder="留空关闭"
                                onChange={(event) => setCompactThreshold(event.target.value)}
                                disabled={busy} />
                              <span className="field-hint">仅在所选 Responses 端点支持自动压缩时开启。</span>
                            </label>
                          </>
                        ) : null}

                        <label className="field">
                          <span className="field-label">凭据读取来源</span>
                          <select
                            className="input"
                            aria-label="凭据读取来源"
                            value={credBackend}
                            onChange={(e) => setCredBackend(e.target.value)}
                            disabled={busy}
                          >
                            <option value="keychain">系统钥匙串（macOS Keychain，推荐安全存储）</option>
                            <option value="env">进程环境变量（开发调试用）</option>
                          </select>
                        </label>
                      </>
                    ) : null}

                    <div className="setting-card-foot">
                      <button
                        type="button"
                        className="btn btn-primary btn-sm"
                        onClick={() => void handleSaveModel()}
                        disabled={busy}
                      >
                        保存模型设置
                      </button>
                      <span className="pill pill-success">零前端驻留</span>
                    </div>
                  </div>

                  {api.saveModelProfile ? (
                    <section className="setting-card" aria-label="运行模型档案">
                      <div className="setting-title">运行模型档案</div>
                      <label className="field"><span className="field-label">档案名</span>
                        <input className="input" value={profileName} maxLength={32} disabled={busy}
                          placeholder="例如 Research" onChange={(e) => setProfileName(e.target.value)} />
                      </label>
                      <button type="button" className="chip-btn" disabled={busy || !profileName.trim()}
                        onClick={() => void updateProfiles()}>将上方配置存为档案</button>
                      {profileNames.map((name) => <p key={name}>{name} <button type="button"
                        className="link-btn" disabled={busy}
                        onClick={() => void updateProfiles(name)} aria-label={`删除档案 ${name}`}>删除</button></p>)}
                    </section>
                  ) : null}

                  <section className="setting-card" aria-label="系统钥匙串凭据管理">
                    <div className="setting-title">系统钥匙串（Keychain）API Key</div>
                    <p className="field-hint">
                      密钥只写入操作系统钥匙串，永不写入配置文件或事件日志，保存后界面绝不回显明文。
                    </p>
                    <div className="setting-cred-list">
                      {keychainItems.map((item) => (
                        <div key={`${item.backend}-${item.provider}`} className="setting-cred-row">
                          <span>
                            <strong>{item.provider}</strong>：
                            {item.implemented
                              ? item.configured
                                ? "已配置（安全存储）"
                                : "未配置"
                              : `当前平台不支持（${item.error ?? "未实现"}）`}
                          </span>
                          {item.writable && item.configured ? (
                            <button
                              type="button"
                              className="chip-btn"
                              onClick={() => void handleDeleteKey(item.provider)}
                              disabled={busy}
                            >
                              清除 {item.provider} 密钥
                            </button>
                          ) : null}
                        </div>
                      ))}
                    </div>

                    {keychainWritable ? (
                      <div className="setting-inline-form">
                        <select
                          className="input setting-provider-select"
                          aria-label="密钥厂商"
                          value={keyProvider}
                          onChange={(e) =>
                            setKeyProvider(e.target.value as "openai" | "anthropic")
                          }
                          disabled={busy}
                        >
                          <option value="openai">openai</option>
                          <option value="anthropic">anthropic</option>
                        </select>
                        <input
                          className="input setting-grow-input"
                          type="password"
                          aria-label="API Key 明文输入"
                          value={keySecret}
                          placeholder="粘贴 sk-... 后保存到系统钥匙串"
                          onChange={(e) => setKeySecret(e.target.value)}
                          disabled={busy}
                        />
                        <button
                          type="button"
                          className="btn btn-primary btn-sm"
                          onClick={() => void handleSaveKey()}
                          disabled={busy}
                        >
                          存入钥匙串
                        </button>
                      </div>
                    ) : null}

                    {envItems.length > 0 ? (
                      <p className="field-hint">
                        环境变量状态：
                        {envItems
                          .map(
                            (c) =>
                              `${c.provider}（${c.env_var ?? "-"}：${c.configured ? "已检测" : "未设置"}）`,
                          )
                          .join(" · ")}
                      </p>
                    ) : null}
                  </section>
                </>
              )}
            </>
          ) : null}

          {activeTab === "security" ? (
            <>
              <div className="settings-head">
                <div>
                  <h2>安全与执行授权</h2>
                  <p>授权始终在执行侧判定；模型输出与提示词不构成授权依据</p>
                </div>
                {closeButton}
              </div>

              <div className="setting-row">
                <div>
                  <div className="setting-title">新建对话默认执行模式</div>
                  <div className="setting-desc">
                    控制新建任务时输入框默认选中的运行策略（自动审批与完全访问待后端授权契约接入）
                  </div>
                </div>
                <select
                  className="ts-select"
                  aria-label="新建对话默认执行模式"
                  value={preferences.defaultPolicy.mode}
                  onChange={(event) =>
                    onUpdatePreferences?.({
                      defaultPolicy:
                        event.target.value === "pure_only"
                          ? PURE_ONLY_RUN_POLICY
                          : EFFECTS_RUN_POLICY,
                    })
                  }
                >
                  <option value="effects">每次询问（允许工具操作）</option>
                  <option value="pure_only">仅读取与对话（零写入副作用）</option>
                  <option value="auto" disabled>
                    自动审批（待后端授权契约接入）
                  </option>
                  <option value="full" disabled>
                    完全访问（待后端授权契约接入）
                  </option>
                </select>
              </div>

              <div className="setting-row">
                <div>
                  <div className="setting-title">工作区路径边界校验</div>
                  <div className="setting-desc">
                    所有路径均先解析符号链接与相对跳转后再核对绑定工作区；批准绑定具体动作与参数摘要，一次性消费
                  </div>
                </div>
                <span className="pill pill-success">执行侧强制</span>
              </div>

              <div className="setting-row">
                <div>
                  <div className="setting-title">进程隔离能力说明（如实披露）</div>
                  <div className="setting-desc">
                    工作区边界只约束工具可访问的路径；当前本地执行后端未启用物理进程沙箱，命令审批卡片会醒目提示隔离级别
                  </div>
                </div>
                <span className="pill pill-warn">无物理沙箱</span>
              </div>
            </>
          ) : null}

          {activeTab === "schedule" ? (
            <>
              <div className="settings-head">
                <div>
                  <h2>定时任务</h2>
                  <p>按时间、文件到达或本机事件触发任务。运行时仍需遵守工作区和审批设置。</p>
                </div>
                {closeButton}
              </div>

              {api === null ? (
                <p className="field-hint">尚未连接守护进程设置接口。</p>
              ) : (
                <>
                  <div className="setting-card">
                    <label className="field">
                      <span className="field-label">新建定时任务目标</span>
                      <input
                        className="input"
                        type="text"
                        value={schedGoal}
                        placeholder="例如：每日汇总收件目录里的日报并输出摘要"
                        onChange={(e) => setSchedGoal(e.target.value)}
                        disabled={busy}
                      />
                    </label>
                    <label className="field">
                      <span className="field-label">触发方式</span>
                      <select className="input" value={schedKind} disabled={busy}
                        onChange={(e) => {
                          const kind = SCHEDULE_KINDS.find((choice) => choice.value === e.target.value);
                          if (kind) setSchedKind(kind.value);
                        }}>
                        {SCHEDULE_KINDS.map(({ value, label }) => <option key={value} value={value}>{label}</option>)}
                      </select>
                    </label>
                    {schedKind === "calendar" ? (
                      <div className="setting-inline-form">
                        <label className="field">
                          <span className="field-label">当地时间</span>
                          <input className="input" type="time" value={schedTime}
                            onChange={(e) => setSchedTime(e.target.value)} disabled={busy} />
                        </label>
                        <label className="field">
                          <span className="field-label">时区</span>
                          <input className="input" placeholder="UTC+08:00" value={schedZone}
                            onChange={(e) => setSchedZone(e.target.value)} disabled={busy} />
                        </label>
                        <label className="field">
                          <span className="field-label">星期（周一 1，周日 7）</span>
                          <input className="input" value={schedDays}
                            onChange={(e) => setSchedDays(e.target.value)} disabled={busy} />
                        </label>
                      </div>
                    ) : null}
                    {schedKind === "file_arrival" ? (
                      <label className="field">
                        <span className="field-label">监听目录绝对路径（只读）</span>
                        <input className="input" value={schedDirectory}
                          onChange={(e) => setSchedDirectory(e.target.value)} disabled={busy} />
                        <span className="field-hint">只检查直接子文件。新建时已有的文件不会触发。</span>
                      </label>
                    ) : null}
                    {schedKind === "event" ? (
                      <p className="field-hint">本机脚本可运行 <code>tianshu schedule signal SCHEDULE_ID EVENT_ID</code>，同一事件只入队一次。</p>
                    ) : null}
                    <div className="setting-inline-form">
                      <input
                        className="input setting-grow-input"
                        type="text"
                        aria-label="定时任务工作目录"
                        value={schedWorkspace}
                        placeholder="可选：可写工作目录绝对路径（留空为纯对话任务）"
                        onChange={(e) => setSchedWorkspace(e.target.value)}
                        disabled={busy}
                      />
                      {schedKind === "interval" ? <input
                        className="input setting-interval-input"
                        type="number"
                        min={1}
                        aria-label="触发间隔秒数"
                        value={schedInterval}
                        onChange={(e) => setSchedInterval(e.target.value)}
                        disabled={busy}
                      /> : null}
                      <button
                        type="button"
                        className="btn btn-primary btn-sm"
                        onClick={() => void handleCreateSchedule()}
                        disabled={busy}
                      >
                        新建定时任务
                      </button>
                    </div>
                  </div>

                  <div className="setting-list">
                    {schedules.length === 0 ? (
                      <p className="field-hint">当前暂无定时任务。</p>
                    ) : (
                      schedules.map((item) => (
                        <div key={item.schedule_id} className="setting-list-item">
                          <div className="setting-list-head">
                            <strong>{item.goal}</strong>
                            <span className="pill">
                              {item.enabled ? "已启用" : "已停用"} · {scheduleLabel(item)}
                            </span>
                          </div>
                          {item.trigger.kind === "file_arrival" ? (
                            <p className="field-hint">监听：{item.trigger.directory}</p>
                          ) : null}
                          {item.trigger.kind === "event" ? (
                            <p className="field-hint">调度 ID：<code>{item.schedule_id}</code></p>
                          ) : null}
                          {item.last_error !== "" ? (
                            <p className="field-hint text-danger">上次触发异常：{item.last_error}</p>
                          ) : null}
                          <div className="setting-inline-actions">
                            <button
                              type="button"
                              className="chip-btn"
                              onClick={() => void handleToggleSchedule(item)}
                              disabled={busy}
                            >
                              {item.enabled ? "停用" : "启用"}
                            </button>
                            <button
                              type="button"
                              className="btn btn-danger btn-sm"
                              onClick={() => void handleRemoveSchedule(item.schedule_id)}
                              disabled={busy}
                            >
                              删除
                            </button>
                          </div>
                        </div>
                      ))
                    )}
                  </div>
                </>
              )}
            </>
          ) : null}

          {activeTab === "budget" ? (
            <>
              <div className="settings-head">
                <div>
                  <h2>预算账本</h2>
                  <p>父子任务共享原子预占与按实结算，未知用量与未知费用绝不按 0 混算</p>
                </div>
                {closeButton}
              </div>

              {api === null ? (
                <p className="field-hint">尚未连接守护进程设置接口。</p>
              ) : budget === null ? (
                <p className="field-hint">正在读取预算账本…</p>
              ) : (
                <>
                  <div className="setting-row">
                    <div>
                      <div className="setting-title">全局未决 / 待核对预占笔数</div>
                      <div className="setting-desc">
                        未知用量保持占位直至核清，不伪装为 0；单次运行的明细费用也可在会话右上角「运行洞察」查看
                      </div>
                    </div>
                    <span className="pill pill-info num">{budget.unsettled_count} 笔</span>
                  </div>

                  {budget.accounts.length === 0 ? (
                    <p className="field-hint">当前尚无账户级预算记录（首次运行预占后自动建立）。</p>
                  ) : (
                    <div className="setting-list">
                      {budget.accounts.map((acc) => (
                        <div key={acc.account_id} className="setting-list-item">
                          <div className="setting-list-head">
                            <strong>{acc.account_id}</strong>
                            <span className={`pill ${acc.overrun ? "pill-danger" : "pill-info"}`}>
                              口径：{acc.bound || "estimate"}
                              {acc.overrun ? " · 已超上界" : ""}
                              {acc.cancelled ? " · 已取消" : ""}
                            </span>
                          </div>
                          <p className="field-hint num">
                            已结算 Tokens：{formatInt(acc.settled["tokens"] ?? 0)} · 已结算金额：
                            {formatUsdExact(acc.settled["micros"] ?? 0)} · 预占中 Tokens：
                            {formatInt(acc.reserved["tokens"] ?? 0)} · 未知预占笔数：
                            {acc.unknown_reservations}
                          </p>
                        </div>
                      ))}
                    </div>
                  )}
                </>
              )}
            </>
          ) : null}

          {activeTab === "mcp" ? (
            <>
              <div className="settings-head">
                <div>
                  <h2>MCP 服务</h2>
                  <p>只读查看受控 stdio 和 HTTPS MCP 服务状态、准入工具与被拒工具诊断</p>
                </div>
                {closeButton}
              </div>

              {api === null ? (
                <p className="field-hint">尚未连接守护进程设置接口。</p>
              ) : mcp === null ? (
                <p className="field-hint">正在检查 MCP 配置…</p>
              ) : !mcp.configured ? (
                <div className="setting-row">
                  <div>
                    <div className="setting-title">外部 MCP 工具扩展</div>
                    <div className="setting-desc">{mcp.reason ?? "尚未配置 MCP server。"}</div>
                  </div>
                  <span className="pill">未配置</span>
                </div>
              ) : (
                <>
                  <p className="field-hint">
                    已配置 {mcp.servers.length} 个 MCP 服务，准入工具 {mcp.tools.length} 个，拒绝工具{" "}
                    {mcp.rejected_tools.length} 个。
                  </p>
                  <div className="setting-list">
                    {mcp.servers.map((srv) => (
                      <div key={srv.name} className="setting-list-item">
                        <div className="setting-list-head">
                          <strong>{srv.name}</strong>
                          <span className="pill pill-info">
                            {srv.status} · 工具 {srv.tools.length} 个
                          </span>
                        </div>
                        {srv.diagnostics.map((d, idx) => (
                          <p key={idx} className="field-hint">
                            诊断：{d}
                          </p>
                        ))}
                      </div>
                    ))}
                  </div>
                </>
              )}
            </>
          ) : null}

          {activeTab === "memory" ? (
            <>
              <div className="settings-head">
                <div>
                  <h2>记忆与上下文</h2>
                  <p>长期记忆是只追加事件日志的可重建投影，支持确认、忘记与彻底擦除</p>
                </div>
                {closeButton}
              </div>

              <div className="setting-row">
                <div>
                  <div className="setting-title">作用域隔离与候选确认</div>
                  <div className="setting-desc">
                    区分用户全局（user）、工作区（workspace）与任务（task）三级作用域；模型提议的候选记忆需你确认后生效
                  </div>
                </div>
                <span className="pill pill-success">可审计投影</span>
              </div>

              <div className="setting-row">
                <div>
                  <div className="setting-title">记忆库管理面板</div>
                  <div className="setting-desc">
                    检索词面与语义召回结果、核验来源事件凭据、确认候选记忆或执行忘记/擦除
                  </div>
                </div>
                <button
                  type="button"
                  className="chip-btn is-active"
                  onClick={() => {
                    onClose();
                    onOpenMemory?.();
                  }}
                >
                  打开记忆库
                </button>
              </div>
            </>
          ) : null}

          {activeTab === "diagnostics" ? (
            <>
              <div className="settings-head">
                <div>
                  <h2>诊断与数据服务</h2>
                  <p>守护进程健康水位、默认脱敏诊断包本地导出与在线一致性备份</p>
                </div>
                {closeButton}
              </div>

              <div className="setting-row">
                <div>
                  <div className="setting-title">本地守护进程（Agent Daemon）</div>
                  <div className="setting-desc">
                    {daemon.version !== null ? `版本 v${daemon.version}` : "版本未知"}
                    {daemon.started_at !== null
                      ? ` · 启动于 ${formatRelative(daemon.started_at, now)}`
                      : ""}
                    {` · ${streamStatusText(streamStatus, streamDetail)}`}
                  </div>
                </div>
                <span className={`pill ${isHealthy ? "pill-success" : "pill-danger"}`}>
                  {isHealthy ? "健康连接" : "连接中断"}
                </span>
              </div>

              <div className="setting-row">
                <div>
                  <div className="setting-title">事件日志同步水位与数据目录</div>
                  <div className="setting-desc mono">
                    已同步至 #{eventSeqHead}
                    {daemon.data_root !== null ? ` · ${daemon.data_root}` : ""}
                  </div>
                </div>
                <div className="setting-inline-actions">
                  <button
                    type="button"
                    className="chip-btn"
                    onClick={() => onRefresh?.()}
                    disabled={onRefresh === undefined}
                  >
                    立即同步
                  </button>
                  <button
                    type="button"
                    className="chip-btn is-active"
                    onClick={() => onReconnectStream?.()}
                    disabled={onReconnectStream === undefined}
                  >
                    重连事件流
                  </button>
                </div>
              </div>

              <div className="setting-card">
                <div className="setting-title">默认脱敏诊断包导出（仅限本地）</div>
                <p className="field-hint">
                  诊断包只在本地写出 .zip 文件，<strong>绝不自动联网上传</strong>。默认自动对凭据、本机路径与任务目标打码，仅保留事件结构、时序与错误码。
                </p>
                {api === null ? (
                  <p className="field-hint">尚未连接守护进程设置接口。</p>
                ) : diagManifest === null ? (
                  <p className="field-hint">正在生成诊断包内容预览清单…</p>
                ) : (
                  <>
                    <div className="setting-diag-list">
                      {diagManifest.sections.map((sec) => {
                        const checked = diagSelected[sec.section] !== false;
                        return (
                          <label key={sec.section} className="setting-diag-row">
                            <input
                              type="checkbox"
                              checked={checked}
                              onChange={(e) =>
                                setDiagSelected((prev) => ({
                                  ...prev,
                                  [sec.section]: e.target.checked,
                                }))
                              }
                              disabled={busy}
                            />
                            <span>
                              <strong>{sec.section}</strong>（{sec.record_count} 条，约{" "}
                              {formatInt(sec.estimated_bytes)} B）— {sec.description}
                            </span>
                          </label>
                        );
                      })}
                    </div>

                    <div className="setting-inline-form">
                      <input
                        className="input setting-grow-input"
                        type="text"
                        aria-label="诊断包本地导出目录"
                        value={diagOutDir}
                        placeholder="输入本地导出目录绝对路径，例如 ~/Desktop"
                        onChange={(e) => setDiagOutDir(e.target.value)}
                        disabled={busy}
                      />
                      <button
                        type="button"
                        className="btn btn-primary btn-sm"
                        onClick={() => void handleExportDiagnostics()}
                        disabled={busy}
                      >
                        导出脱敏诊断包到本地
                      </button>
                    </div>
                  </>
                )}
              </div>

              <div className="setting-row setting-row-stack">
                <div>
                  <div className="setting-title">在线一致性备份与校验 CLI 命令</div>
                  <div className="setting-desc">
                    事件日志永不删除或覆写；可在终端执行在线快照备份、清单 SHA-256 核验与保留期审计
                  </div>
                </div>
                <pre className="settings-code-block mono">
                  {`tianshu backup create <输出目录>\ntianshu backup verify <备份包路径>\ntianshu data retention-audit`}
                </pre>
              </div>
            </>
          ) : null}

          {activeTab === "appearance" ? (
            <>
              <div className="settings-head">
                <div>
                  <h2>外观与伴侣动效</h2>
                  <p>定制界面浅深主题与主页动态伴侣的交互表现（纯内存态，不写浏览器存储）</p>
                </div>
                {closeButton}
              </div>

              <div className="setting-row">
                <div>
                  <div className="setting-title">界面外观主题</div>
                  <div className="setting-desc">实时切换清透浅色、深邃暗色或跟随操作系统外观</div>
                </div>
                <div className="setting-inline-actions">
                  <button
                    type="button"
                    className={`chip-btn${preferences.theme === "light" ? " is-active" : ""}`}
                    data-theme-choice="light"
                    onClick={() => onUpdatePreferences?.({ theme: "light" })}
                  >
                    <IconSun size={13} />
                    <span>浅色模式</span>
                  </button>
                  <button
                    type="button"
                    className={`chip-btn${preferences.theme === "dark" ? " is-active" : ""}`}
                    data-theme-choice="dark"
                    onClick={() => onUpdatePreferences?.({ theme: "dark" })}
                  >
                    <IconMoon size={13} />
                    <span>深色模式</span>
                  </button>
                  <button
                    type="button"
                    className={`chip-btn${preferences.theme === "system" ? " is-active" : ""}`}
                    data-theme-choice="system"
                    onClick={() => onUpdatePreferences?.({ theme: "system" })}
                  >
                    <span>跟随系统</span>
                  </button>
                </div>
              </div>

              <div className="setting-row">
                <div>
                  <div className="setting-title">在欢迎主页展示动态伴侣</div>
                  <div className="setting-desc">新建对话欢迎页与运行洞察面板展示立体矢量伴侣形象</div>
                </div>
                <button
                  type="button"
                  role="switch"
                  aria-checked={preferences.enablePet}
                  aria-label="在欢迎主页展示动态伴侣"
                  data-testid="switch-enable-pet"
                  className={`ts-switch${preferences.enablePet ? " is-on" : ""}`}
                  onClick={() =>
                    onUpdatePreferences?.({ enablePet: !preferences.enablePet })
                  }
                >
                  <span className="ts-switch-knob" />
                </button>
              </div>

              <div className="setting-row">
                <div>
                  <div className="setting-title">鼠标 2.5D 眼神跟随视差</div>
                  <div className="setting-desc">
                    光标掠过欢迎页伴侣时平滑带动面罩、内凹倒角与发光眼神微转向
                  </div>
                </div>
                <button
                  type="button"
                  role="switch"
                  aria-checked={preferences.enableParallax}
                  aria-label="鼠标 2.5D 眼神跟随视差"
                  data-testid="switch-pet-parallax"
                  className={`ts-switch${preferences.enableParallax ? " is-on" : ""}`}
                  onClick={() =>
                    onUpdatePreferences?.({
                      enableParallax: !preferences.enableParallax,
                    })
                  }
                >
                  <span className="ts-switch-knob" />
                </button>
              </div>

              <div className="setting-row">
                <div>
                  <div className="setting-title">减少持续动态效果</div>
                  <div className="setting-desc">
                    暂停伴侣持续悬浮、挥手与流光动画，保持安静静态展示
                  </div>
                </div>
                <button
                  type="button"
                  role="switch"
                  aria-checked={preferences.reduceMotion}
                  aria-label="减少持续动态效果"
                  data-testid="switch-reduce-motion"
                  className={`ts-switch${preferences.reduceMotion ? " is-on" : ""}`}
                  onClick={() =>
                    onUpdatePreferences?.({ reduceMotion: !preferences.reduceMotion })
                  }
                >
                  <span className="ts-switch-knob" />
                </button>
              </div>

              <div className="setting-row setting-pose-preview-row">
                <div className="setting-pose-left">
                  <WelcomePet
                    status={preferences.previewPose}
                    showPedestal={false}
                    parallaxEnabled={false}
                    reducedMotion={preferences.reduceMotion}
                    size={72}
                  />
                  <div>
                    <div className="setting-title">伴侣七态实时预览</div>
                    <div className="setting-desc">点击右侧状态胶囊预览全部表情姿态</div>
                  </div>
                </div>
                <div className="setting-pose-pills">
                  {POSE_OPTIONS.map((item) => (
                    <button
                      key={item.key}
                      type="button"
                      className={`chip-btn${preferences.previewPose === item.key ? " is-active" : ""}`}
                      onClick={() => onUpdatePreferences?.({ previewPose: item.key })}
                    >
                      {item.label}
                    </button>
                  ))}
                </div>
              </div>
            </>
          ) : null}
        </div>
      </div>
    </div>
  );
}
