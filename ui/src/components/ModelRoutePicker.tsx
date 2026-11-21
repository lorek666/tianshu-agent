import type { ModelProfileInput } from "../api/settings";
import type { RunModelSelection } from "../api/types";

export function ModelRoutePicker({ profiles, selection, onChange, disabled }: {
  profiles: Record<string, ModelProfileInput>;
  selection: RunModelSelection;
  onChange: (selection: RunModelSelection) => void;
  disabled: boolean;
}) {
  const names = Object.keys(profiles);
  if (!names.length) return null;
  const primary = selection.model_profile ?? "";
  const fallbacks = selection.fallback_profiles ?? [];
  return (
    <details className="composer-model-route">
      <summary>为本次运行选模型{primary ? ` · ${primary}` : ""}</summary>
      <label className="field">
        <span className="field-label">首选档案</span>
        <select className="input" disabled={disabled} value={primary} onChange={(event) => {
          onChange(event.target.value ? { model_profile: event.target.value } : {});
        }}>
          <option value="">当前默认模型</option>
          {names.map((name) => <option key={name} value={name}>{name}</option>)}
        </select>
      </label>
      {primary ? Array.from({ length: Math.min(3, names.length - 1) }, (_, index) => {
        if (index > fallbacks.length) return null;
        const selected = fallbacks[index] ?? "";
        return (
          <label className="field" key={index}>
            <span className="field-label">备用 {index + 1}</span>
            <select className="input" disabled={disabled} value={selected} onChange={(event) => {
              const next = fallbacks.slice(0, index);
              if (event.target.value) next.push(event.target.value);
              onChange({ model_profile: primary, ...(next.length ? { fallback_profiles: next } : {}) });
            }}>
              <option value="">无</option>
              {names.filter((name) => name !== primary &&
                (name === selected || !fallbacks.includes(name)))
                .map((name) => <option key={name} value={name}>{name}</option>)}
            </select>
          </label>
        );
      }) : null}
      {primary ? <p className="field-hint">备用档案只在发送请求前用于本地预检。</p> : null}
    </details>
  );
}
