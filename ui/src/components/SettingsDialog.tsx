/**
 * 首日设置与运行管理对话框：模型与系统钥匙串凭据、定时任务、账户预算账本、MCP 状态。
 *
 * 安全红线：
 * - 密钥输入框使用 password 类型，提交成功后立即清空本地状态，绝不回显任何已存密钥字符；
 * - 当后端返回 `unsupported`（例如非 macOS 钥匙串或只读环境变量后端）时如实展示原因，不静默降级。
 */

import { useCallback, useEffect, useState } from "react";

import { humanizeHostError } from "../api/errors";
import type {
  BudgetOverviewView,
  CredentialStatusView,
  DiagnosticManifestView,
  McpOverviewView,
  ScheduleView,
  SettingsApi,
} from "../api/settings";
import { formatInt, formatUsdExact } from "../lib/format";
import { Modal } from "./Modal";

type TabKey = "model" | "schedule" | "budget" | "mcp" | "diagnostics";

export interface SettingsDialogProps {
  open: boolean;
  api: SettingsApi;
  onClose: () => void;
  onSettingsSaved?: (summary: string) => void;
}

export function SettingsDialog({
  open,
  api,
  onClose,
  onSettingsSaved,
}: SettingsDialogProps) {
  const [tab, setTab] = useState<TabKey>("model");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  // Model & Credentials state
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

  // Budget & MCP & Diagnostics state
  const [budget, setBudget] = useState<BudgetOverviewView | null>(null);
  const [mcp, setMcp] = useState<McpOverviewView | null>(null);
  const [diagManifest, setDiagManifest] = useState<DiagnosticManifestView | null>(null);
  const [diagSelected, setDiagSelected] = useState<Record<string, boolean>>({});
  const [diagOutDir, setDiagOutDir] = useState("");

  const loadTab = useCallback(
    async (target: TabKey) => {
      setBusy(true);
      setError(null);
      try {
        if (target === "model") {
          const s = await api.getSettings();
          setMode(s.model.mode);
          setModelName(s.model.model_name);
          setBaseUrl(s.model.base_url);
          setCredBackend(s.model.credential_backend);
          setCredentials(s.credentials);
        } else if (target === "schedule") {
          setSchedules(await api.listSchedules());
        } else if (target === "budget") {
          setBudget(await api.getBudgetOverview());
        } else if (target === "mcp") {
          setMcp(await api.getMcpOverview());
        } else {
          const manifest = await api.previewDiagnostics();
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
        setError(humanizeHostError(caught));
      } finally {
        setBusy(false);
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
    void loadTab(tab);
  }, [open, tab, loadTab]);

  if (!open) return null;

  const handleSaveModel = async (): Promise<void> => {
    if (mode === "anthropic" && modelName.trim() === "") {
      setError("Anthropic 模式必须显式填写模型名称（例如 claude-sonnet-4-5）。");
      return;
    }
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const updated = await api.configureModel({
        mode,
        model_name: modelName.trim(),
        base_url: baseUrl.trim(),
        credential_backend: credBackend,
      });
      setMode(updated.model.mode);
      setModelName(updated.model.model_name);
      setBaseUrl(updated.model.base_url);
      setCredBackend(updated.model.credential_backend);
      setCredentials(updated.credentials);
      setNotice(`已保存并切换当前模型：${updated.model.summary}`);
      onSettingsSaved?.(updated.model.summary);
    } catch (caught) {
      setError(humanizeHostError(caught));
    } finally {
      setBusy(false);
    }
  };

  const handleSaveKey = async (): Promise<void> => {
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
    const interval = Number.parseInt(schedInterval, 10);
    if (schedGoal.trim() === "") {
      setError("请填写定时任务的目标描述。");
      return;
    }
    if (!Number.isFinite(interval) || interval < 1) {
      setError("触发间隔必须是大于等于 1 的整数秒。");
      return;
    }
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await api.createSchedule({
        goal: schedGoal.trim(),
        workspace: schedWorkspace.trim(),
        interval_seconds: interval,
      });
      setSchedGoal("");
      setSchedWorkspace("");
      setSchedules(await api.listSchedules());
      setNotice("定时任务已创建，守护进程到期会自动创建任务并入队运行。");
    } catch (caught) {
      setError(humanizeHostError(caught));
    } finally {
      setBusy(false);
    }
  };

  const keychainItems = credentials.filter((c) => c.backend === "keychain");
  const envItems = credentials.filter((c) => c.backend === "env");
  const keychainWritable = keychainItems.some((c) => c.writable);

  return (
    <Modal
      title="设置与运行管理"
      subtitle="配置真实模型与系统钥匙串凭据，管理定时任务，并查看账户级预算账本与 MCP 服务状态。"
      onClose={onClose}
      closeDisabled={busy}
      wide
    >
      <div className="memory-toolbar" role="tablist" aria-label="设置分类" style={{ marginBottom: 12 }}>
        {(
          [
            ["model", "模型与密钥"],
            ["schedule", "定时任务"],
            ["budget", "预算账本"],
            ["mcp", "MCP 服务"],
            ["diagnostics", "诊断包导出"],
          ] as const
        ).map(([key, label]) => (
          <button
            key={key}
            type="button"
            role="tab"
            aria-selected={tab === key}
            className={tab === key ? "btn btn-primary btn-sm" : "btn btn-ghost btn-sm"}
            onClick={() => {
              setNotice(null);
              setError(null);
              setTab(key);
            }}
            disabled={busy}
          >
            {label}
          </button>
        ))}
      </div>

      {error !== null ? (
        <p className="banner banner-danger" role="alert">
          {error}
        </p>
      ) : null}
      {notice !== null ? (
        <p className="banner" role="status">
          {notice}
        </p>
      ) : null}

      {tab === "model" ? (
        <div className="form">
          <div className="field">
            <span className="field-label">模型协议</span>
            <select
              className="input"
              aria-label="模型协议"
              value={mode}
              onChange={(e) => setMode(e.target.value)}
              disabled={busy}
            >
              <option value="scripted">离线演示脚本模型（不联网、不计费）</option>
              <option value="openai">OpenAI 兼容协议（OpenAI / DeepSeek / Moonshot / 本地服务）</option>
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
                  placeholder={mode === "anthropic" ? "必填，例如 claude-sonnet-4-5" : "默认 gpt-4o-mini"}
                  onChange={(e) => setModelName(e.target.value)}
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
                  onChange={(e) => setBaseUrl(e.target.value)}
                  disabled={busy}
                />
              </label>

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

          <div>
            <button
              type="button"
              className="btn btn-primary btn-sm"
              onClick={() => void handleSaveModel()}
              disabled={busy}
            >
              保存模型设置
            </button>
          </div>

          <hr style={{ border: 0, borderTop: "1px solid var(--border, #e5e7eb)", margin: "12px 0" }} />

          <section aria-label="系统钥匙串凭据管理">
            <h4 style={{ margin: "0 0 6px" }}>系统钥匙串（Keychain）API Key</h4>
            <p className="field-hint" style={{ marginBottom: 8 }}>
              密钥只写入操作系统钥匙串，永不写入配置文件或事件日志，保存后界面绝不回显明文。
            </p>
            {keychainItems.map((item) => (
              <div
                key={`${item.backend}-${item.provider}`}
                style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}
              >
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
                    className="btn btn-ghost btn-sm"
                    onClick={() => void handleDeleteKey(item.provider)}
                    disabled={busy}
                  >
                    清除 {item.provider} 密钥
                  </button>
                ) : null}
              </div>
            ))}

            {keychainWritable ? (
              <div style={{ display: "flex", gap: 8, marginTop: 8, flexWrap: "wrap" }}>
                <select
                  className="input"
                  style={{ width: 140 }}
                  aria-label="密钥厂商"
                  value={keyProvider}
                  onChange={(e) => setKeyProvider(e.target.value as "openai" | "anthropic")}
                  disabled={busy}
                >
                  <option value="openai">openai</option>
                  <option value="anthropic">anthropic</option>
                </select>
                <input
                  className="input"
                  style={{ flex: 1, minWidth: 220 }}
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
              <p className="field-hint" style={{ marginTop: 8 }}>
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
        </div>
      ) : null}

      {tab === "schedule" ? (
        <div className="form">
          <div style={{ display: "grid", gap: 8 }}>
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
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
              <input
                className="input"
                style={{ flex: 1, minWidth: 200 }}
                type="text"
                aria-label="定时任务工作目录"
                value={schedWorkspace}
                placeholder="可选：可写工作目录绝对路径（留空为纯对话任务）"
                onChange={(e) => setSchedWorkspace(e.target.value)}
                disabled={busy}
              />
              <input
                className="input"
                style={{ width: 140 }}
                type="number"
                min={1}
                aria-label="触发间隔秒数"
                value={schedInterval}
                onChange={(e) => setSchedInterval(e.target.value)}
                disabled={busy}
              />
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

          <div style={{ marginTop: 12 }}>
            {schedules.length === 0 ? (
              <p className="field-hint">当前暂无定时任务。</p>
            ) : (
              schedules.map((item) => (
                <div
                  key={item.schedule_id}
                  style={{
                    padding: "8px 10px",
                    border: "1px solid var(--border, #e5e7eb)",
                    borderRadius: 6,
                    marginBottom: 8,
                  }}
                >
                  <div style={{ display: "flex", justifyContent: "space-between", gap: 8 }}>
                    <strong>{item.goal}</strong>
                    <span>{item.enabled ? "已启用" : "已停用"} · 每 {item.interval_seconds}s</span>
                  </div>
                  {item.last_error !== "" ? (
                    <p className="field-hint" style={{ color: "var(--danger, #b91c1c)" }}>
                      上次触发异常：{item.last_error}
                    </p>
                  ) : null}
                  <div style={{ display: "flex", gap: 8, marginTop: 6 }}>
                    <button
                      type="button"
                      className="btn btn-ghost btn-sm"
                      onClick={() => {
                        void api
                          .setScheduleEnabled(item.schedule_id, !item.enabled)
                          .then(() => api.listSchedules())
                          .then(setSchedules)
                          .catch((c) => setError(humanizeHostError(c)));
                      }}
                      disabled={busy}
                    >
                      {item.enabled ? "停用" : "启用"}
                    </button>
                    <button
                      type="button"
                      className="btn btn-danger btn-sm"
                      onClick={() => {
                        void api
                          .removeSchedule(item.schedule_id)
                          .then(() => api.listSchedules())
                          .then(setSchedules)
                          .catch((c) => setError(humanizeHostError(c)));
                      }}
                      disabled={busy}
                    >
                      删除
                    </button>
                  </div>
                </div>
              ))
            )}
          </div>
        </div>
      ) : null}

      {tab === "budget" ? (
        <div>
          {budget === null ? (
            <p className="field-hint">正在读取预算账本…</p>
          ) : (
            <>
              <p className="field-hint">
                全局未决/待核对预占笔数：<strong>{budget.unsettled_count}</strong>（未知用量不伪装为 0，保持占位直至核清）
              </p>
              {budget.accounts.length === 0 ? (
                <p className="field-hint">当前尚无账户级预算记录（首次运行预占后自动建立）。</p>
              ) : (
                budget.accounts.map((acc) => (
                  <div
                    key={acc.account_id}
                    style={{
                      padding: "8px 10px",
                      border: "1px solid var(--border, #e5e7eb)",
                      borderRadius: 6,
                      marginBottom: 8,
                    }}
                  >
                    <div>
                      <strong>{acc.account_id}</strong>（口径：{acc.bound || "estimate"}
                      {acc.overrun ? " · 已超上界" : ""}
                      {acc.cancelled ? " · 已取消" : ""}）
                    </div>
                    <p className="field-hint" style={{ margin: "4px 0 0" }}>
                      已结算 Tokens：{formatInt(acc.settled["tokens"] ?? 0)} · 已结算金额：
                      {formatUsdExact(acc.settled["micros"] ?? 0)} · 预占中 Tokens：
                      {formatInt(acc.reserved["tokens"] ?? 0)} · 未知预占笔数：
                      {acc.unknown_reservations}
                    </p>
                  </div>
                ))
              )}
            </>
          )}
        </div>
      ) : null}

      {tab === "mcp" ? (
        <div>
          {mcp === null ? (
            <p className="field-hint">正在检查 MCP 配置…</p>
          ) : !mcp.configured ? (
            <p className="field-hint">{mcp.reason ?? "尚未配置 MCP server。"}</p>
          ) : (
            <>
              <p className="field-hint">
                已配置 {mcp.servers.length} 个 MCP 服务，准入工具 {mcp.tools.length} 个，拒绝工具{" "}
                {mcp.rejected_tools.length} 个。
              </p>
              {mcp.servers.map((srv) => (
                <div
                  key={srv.name}
                  style={{
                    padding: "8px 10px",
                    border: "1px solid var(--border, #e5e7eb)",
                    borderRadius: 6,
                    marginBottom: 8,
                  }}
                >
                  <strong>{srv.name}</strong> · 状态：{srv.status} · 工具数：{srv.tools.length}
                  {srv.diagnostics.map((d, idx) => (
                    <p key={idx} className="field-hint">
                      诊断：{d}
                    </p>
                  ))}
                </div>
              ))}
            </>
          )}
        </div>
      ) : null}

      {tab === "diagnostics" ? (
        <div className="form">
          <p className="field-hint">
            诊断包只在本地写出 .zip 文件，<strong>绝不自动联网上传</strong>。默认自动对凭据、本机路径与任务目标打码，仅保留事件结构、时序与错误码。
          </p>
          {diagManifest === null ? (
            <p className="field-hint">正在生成诊断包内容预览清单…</p>
          ) : (
            <>
              <div>
                {diagManifest.sections.map((sec) => {
                  const checked = diagSelected[sec.section] !== false;
                  return (
                    <label
                      key={sec.section}
                      style={{
                        display: "flex",
                        alignItems: "flex-start",
                        gap: 8,
                        padding: "6px 0",
                      }}
                    >
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

              <div style={{ display: "flex", gap: 8, marginTop: 8, flexWrap: "wrap" }}>
                <input
                  className="input"
                  style={{ flex: 1, minWidth: 220 }}
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
                  onClick={() => {
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
                    void api
                      .exportDiagnostics(diagOutDir.trim(), chosen)
                      .then((res) => {
                        setDiagManifest(res.manifest);
                        setNotice(`已导出默认脱敏诊断包至本地：${res.archive_path}`);
                      })
                      .catch((c) => setError(humanizeHostError(c)))
                      .finally(() => setBusy(false));
                  }}
                  disabled={busy}
                >
                  导出脱敏诊断包到本地
                </button>
              </div>
            </>
          )}
        </div>
      ) : null}
    </Modal>
  );
}
