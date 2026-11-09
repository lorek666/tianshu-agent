import { IconAlert, IconInfo, IconMark } from "./Icons";
import { Spinner } from "./StatusPill";

export interface ConnectionScreenProps {
  kind: "booting" | "no-token" | "unreachable";
  message?: string | null;
  onRetry: () => void;
  /** 自动重试进行中：显示文案，但不把整页切回加载态（避免闪屏）。 */
  retrying?: boolean;
}

/**
 * Host 没起来 / 页面不是 Host 加载的 / 首屏请求超时 —— 三种情况都给明确的
 * 中文解释和可操作项。绝不停在一个转不完的圈里。
 */
export function ConnectionScreen({ kind, message, onRetry, retrying = false }: ConnectionScreenProps) {
  return (
    <div className="connection-screen">
      <div className="connection-card">
        <div className="connection-mark">
          <IconMark size={30} />
          <span className="connection-brand">天枢</span>
        </div>

        {kind === "booting" ? (
          <>
            <h1>正在连接守护进程…</h1>
            <p className="connection-text">正在向本机的天枢守护进程读取任务状态。</p>
            <div className="connection-status">
              <Spinner label="等待响应" />
            </div>
            <p className="connection-hint">通常不到一秒。若超过 8 秒没有响应，这里会给出错误提示。</p>
          </>
        ) : kind === "no-token" ? (
          <>
            <div className="connection-icon danger">
              <IconAlert size={22} />
            </div>
            <h1>无法连接守护进程</h1>
            <p className="connection-text">
              这个页面应该由天枢的桌面宿主加载。直接打开页面时读不到访问令牌，所以不能连接本机守护进程。
            </p>
            <div className="connection-actions">
              <button type="button" className="btn btn-primary" onClick={onRetry}>
                重新检查
              </button>
              <a className="btn btn-ghost" href="?demo=1">
                用演示数据预览界面
              </a>
            </div>
            <p className="connection-hint">
              启动方式：运行 <code>tianshu host</code>，从它给出的本机地址打开界面。
            </p>
          </>
        ) : (
          <>
            <div className="connection-icon danger">
              <IconAlert size={22} />
            </div>
            <h1>守护进程没有响应</h1>
            <p className="connection-text">
              界面连不上本机守护进程。它可能还没有启动，或者刚刚重启、端口已经换了。
            </p>
            {message !== null && message !== undefined && message !== "" ? (
              <pre className="connection-error">{message}</pre>
            ) : null}
            <div className="connection-actions">
              <button type="button" className="btn btn-primary" onClick={onRetry} disabled={retrying}>
                {retrying ? "正在重试…" : "重试连接"}
              </button>
              <a className="btn btn-ghost" href="?demo=1">
                用演示数据预览界面
              </a>
            </div>
            <p className="connection-hint">
              界面每 5 秒会自动重试一次；启动守护进程后，连接恢复时这里会自动进入主界面。
            </p>
          </>
        )}

        <div className="connection-foot">
          <IconInfo size={13} />
          <span>界面不会把访问令牌写进 URL、localStorage 或日志。</span>
        </div>
      </div>
    </div>
  );
}