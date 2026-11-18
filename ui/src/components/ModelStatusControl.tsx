import { useEffect, useRef, useState } from "react";
import { humanizeHostError } from "../api/errors";
import type { ModelConfigView, SettingsApi, SettingsView } from "../api/settings";
import { IconChevronDown, IconSettings, IconSparkle } from "./Icons";

export interface ModelStatusControlProps {
  testId: string;
  model?: ModelConfigView | null;
  settingsApi?: SettingsApi | null;
  disabled?: boolean;
  onModelUpdated?: (settings: SettingsView) => void;
  onOpenModelSettings: () => void;
}

function conciseModelLabel(model: ModelConfigView | null | undefined): string {
  if (model === null || model === undefined) return "模型配置";
  if (model.mode === "scripted") return "演示脚本模型";
  const name = model.model_name.trim();
  if (name !== "") return name;
  return model.mode;
}

const MODE_OPTIONS = [
  { key: "scripted", label: "离线演示" },
  { key: "openai", label: "OpenAI 兼容" },
  { key: "anthropic", label: "Anthropic" },
] as const;

/**
 * 发送按钮正左侧的模型快速切换入口。
 *
 * 对接控制面真实 `settings.get` / `model.configure` RPC：支持不重启守护进程
 * 热切换模型协议与 Model ID，并提供系统钥匙串凭据与完整端点设置直达入口；
 * 绝不保存或回显任何 API Key 明文。
 */
export function ModelStatusControl({
  testId,
  model = null,
  settingsApi = null,
  disabled = false,
  onModelUpdated,
  onOpenModelSettings,
}: ModelStatusControlProps) {
  const [open, setOpen] = useState(false);
  const [draftMode, setDraftMode] = useState(model?.mode ?? "scripted");
  const [draftModelName, setDraftModelName] = useState(model?.model_name ?? "");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const anchorRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    setDraftMode(model?.mode ?? "scripted");
    setDraftModelName(model?.model_name ?? "");
    setError(null);
  }, [open, model?.mode, model?.model_name]);

  useEffect(() => {
    if (!open) return;
    const handlePointerDown = (event: MouseEvent) => {
      if (
        anchorRef.current !== null &&
        event.target instanceof Node &&
        !anchorRef.current.contains(event.target)
      ) {
        setOpen(false);
      }
    };
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    window.addEventListener("mousedown", handlePointerDown);
    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("mousedown", handlePointerDown);
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [open]);

  const handleSelectMode = (nextMode: string): void => {
    setDraftMode(nextMode);
    setError(null);
    if (nextMode === "openai" && draftModelName.trim() === "") {
      setDraftModelName("gpt-4o-mini");
    } else if (nextMode === "anthropic" && draftModelName.trim() === "") {
      setDraftModelName("claude-sonnet-4-5");
    }
  };

  const handleApply = async (): Promise<void> => {
    if (settingsApi === null) {
      setError("守护进程设置接口尚未就绪。");
      return;
    }
    const trimmedName = draftModelName.trim();
    if (draftMode === "anthropic" && trimmedName === "") {
      setError("Anthropic 模式必须填写模型名称（例如 claude-sonnet-4-5）。");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const updated = await settingsApi.configureModel({
        mode: draftMode,
        model_name: draftMode === "scripted" ? "" : trimmedName,
        base_url: model?.base_url ?? "",
        credential_backend: model?.credential_backend ?? "keychain",
      });
      onModelUpdated?.(updated);
      setOpen(false);
    } catch (caught) {
      setError(humanizeHostError(caught));
    } finally {
      setSaving(false);
    }
  };

  const currentLabel = conciseModelLabel(model);

  return (
    <div className="dropdown-anchor" ref={anchorRef}>
      <button
        type="button"
        className={`chip-btn model-pill${open ? " is-active" : ""}`}
        data-testid={testId}
        disabled={disabled}
        aria-expanded={open}
        aria-haspopup="dialog"
        title={model ? `当前模型：${model.summary}` : "切换当前运行模型"}
        onClick={() => setOpen((previous) => !previous)}
      >
        <IconSparkle size={13} />
        <span>{currentLabel}</span>
        <IconChevronDown size={11} />
      </button>

      {open ? (
        <div
          className="popover-menu align-right model-popover"
          role="dialog"
          aria-label="切换运行模型"
        >
          <div className="popover-header">
            <span>切换运行模型</span>
            <span className="pill pill-info">
              {model?.mode === "scripted" ? "离线" : "在线"}
            </span>
          </div>

          <div className="model-mode-pills" role="group" aria-label="模型协议快速选择">
            {MODE_OPTIONS.map((option) => (
              <button
                key={option.key}
                type="button"
                className={`chip-btn${draftMode === option.key ? " is-active" : ""}`}
                data-model-mode={option.key}
                onClick={() => handleSelectMode(option.key)}
                disabled={saving}
              >
                {option.label}
              </button>
            ))}
          </div>

          {draftMode === "scripted" ? (
            <p className="model-popover-desc">
              使用内置离线演示脚本模型（不联网、不消耗外部 API 额度），立即热切换生效。
            </p>
          ) : (
            <div className="model-quick-fields">
              <label className="ws-field-label" htmlFor={`${testId}-model-id`}>
                模型 ID
              </label>
              <input
                id={`${testId}-model-id`}
                className="input"
                type="text"
                aria-label="快速切换模型 ID"
                data-testid={`${testId}-model-input`}
                value={draftModelName}
                placeholder={
                  draftMode === "anthropic" ? "例如 claude-sonnet-4-5" : "默认 gpt-4o-mini"
                }
                onChange={(event) => setDraftModelName(event.target.value)}
                disabled={saving}
              />
              {model !== null && model.base_url.trim() !== "" ? (
                <div className="popover-footnote mono">端点：{model.base_url}</div>
              ) : null}
            </div>
          )}

          {error !== null ? (
            <div className="form-error model-popover-error" role="alert">
              {error}
            </div>
          ) : null}

          <div className="model-popover-actions">
            <button
              type="button"
              className="btn btn-primary btn-sm"
              data-testid="quick-model-apply-btn"
              onClick={() => void handleApply()}
              disabled={saving || settingsApi === null}
            >
              {saving ? "切换中…" : "应用模型"}
            </button>
          </div>

          <div className="popover-divider" />

          <button
            type="button"
            className="popover-action-btn"
            onClick={() => {
              setOpen(false);
              onOpenModelSettings();
            }}
          >
            <IconSettings size={14} />
            <span>管理 API Key 与高级端点设置…</span>
          </button>
        </div>
      ) : null}
    </div>
  );
}
