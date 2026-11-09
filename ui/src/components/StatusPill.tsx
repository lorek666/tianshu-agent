import type { ReactNode } from "react";

export type PillTone = "neutral" | "info" | "action" | "success" | "warn" | "danger" | "approval";

export interface StatusPillProps {
  tone?: PillTone;
  children: ReactNode;
  dot?: boolean;
  title?: string;
}

/** 状态小胶囊。颜色只做辅助，文字始终是人话。 */
export function StatusPill({ tone = "neutral", children, dot = false, title }: StatusPillProps) {
  return (
    <span className={`pill pill-${tone}`} title={title}>
      {dot ? <span className="pill-dot" aria-hidden="true" /> : null}
      {children}
    </span>
  );
}

export function EmptyState({
  title,
  hint,
  icon,
  action,
}: {
  title: string;
  hint?: string;
  icon?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="empty-state">
      {icon !== undefined ? <div className="empty-icon">{icon}</div> : null}
      <div className="empty-title">{title}</div>
      {hint !== undefined && hint !== "" ? <div className="empty-hint">{hint}</div> : null}
      {action !== undefined ? <div className="empty-action">{action}</div> : null}
    </div>
  );
}

export function Spinner({ label }: { label?: string }) {
  return (
    <span className="spinner-row">
      <span className="spinner" aria-hidden="true" />
      {label !== undefined ? <span>{label}</span> : null}
    </span>
  );
}