import type { DaemonView } from "../api/types";
import type { StreamStatus } from "../api/stream";
import { formatRelative } from "../lib/format";
import { IconMark, IconRefresh } from "./Icons";

export interface AppHeaderProps {
  daemon: DaemonView;
  streamStatus: StreamStatus;
  streamDetail: string | null;
  demo: boolean;
  connectionLost: boolean;
  now: number;
  onRefresh: () => void;
}

export function streamStatusText(status: StreamStatus, detail: string | null): string {
  switch (status) {
    case "live":
      return "事件流已连接";
    case "connecting":
      return "正在连接事件流…";
    case "reconnecting":
      return detail ?? "事件流断开，正在重连…";
    case "polling":
      return "事件流不可用，仅轮询兜底";
    case "stopped":
      return "事件流已停止";
    default:
      return "事件流状态未知";
  }
}

export function AppHeader({
  daemon,
  streamStatus,
  streamDetail,
  demo,
  connectionLost,
  now,
  onRefresh,
}: AppHeaderProps) {
  const versionText = daemon.version !== null ? `v${daemon.version}` : "版本未知";
  const startedText =
    daemon.started_at !== null ? `已运行 ${formatRelative(daemon.started_at, now).replace("前", "")}` : null;
  const live = !connectionLost && (streamStatus === "live" || streamStatus === "connecting" || streamStatus === "reconnecting");

  return (
    <header className="app-header">
      <div className="brand">
        <span className="brand-mark">
          <IconMark size={21} />
        </span>
        <span className="brand-name">天枢</span>
        <span className="brand-sub">桌面 Agent</span>
      </div>

      <div className="header-status">
        {demo ? (
          <span className="demo-badge" title="当前页面使用内置示例数据，不连接真实守护进程">
            演示模式
          </span>
        ) : null}
        <span className={`conn-chip${live ? " is-live" : " is-down"}`} title={streamStatusText(streamStatus, streamDetail)}>
          <span className="conn-dot" aria-hidden="true" />
          {connectionLost ? "连接中断" : streamStatus === "live" ? "已连接" : streamStatusText(streamStatus, streamDetail)}
        </span>
        <span className="daemon-meta" title={daemon.data_root ?? undefined}>
          守护进程 {versionText}
          {startedText !== null ? ` · ${startedText}` : ""}
        </span>
        <button type="button" className="icon-btn" onClick={onRefresh} title="刷新任务与运行状态" aria-label="刷新">
          <IconRefresh size={15} />
        </button>
      </div>
    </header>
  );
}