import { useEffect, useRef, useState } from "react";
import { IconChevronDown, IconSettings, IconSparkle } from "./Icons";

export interface ModelStatusControlProps {
  testId: string;
  disabled?: boolean;
  onOpenModelSettings: () => void;
}

/**
 * 发送按钮正左侧的模型切换入口。
 *
 * 诚实原则（PRD 红线与文档对账纪律）：当前守护进程的模型适配器由启动时的本机配置
 * 托管，控制面尚未暴露动态切换模型的 RPC，因此浮层明确展示托管状态与设置说明入口，
 * 不伪造未生效的模型切换。
 */
export function ModelStatusControl({
  testId,
  disabled = false,
  onOpenModelSettings,
}: ModelStatusControlProps) {
  const [open, setOpen] = useState(false);
  const anchorRef = useRef<HTMLDivElement | null>(null);

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

  return (
    <div className="dropdown-anchor" ref={anchorRef}>
      <button
        type="button"
        className={`chip-btn model-pill${open ? " is-active" : ""}`}
        data-testid={testId}
        disabled={disabled}
        aria-expanded={open}
        aria-haspopup="dialog"
        title="查看守护进程托管模型状态"
        onClick={() => setOpen((previous) => !previous)}
      >
        <IconSparkle size={13} />
        <span>守护进程托管模型</span>
        <IconChevronDown size={11} />
      </button>

      {open ? (
        <div
          className="popover-menu align-right model-popover"
          role="dialog"
          aria-label="推理模型托管说明"
        >
          <div className="popover-header">推理模型（守护进程托管）</div>
          <div className="model-popover-card">
            <div className="model-popover-title">
              <span>✦ 本机守护进程当前配置模型</span>
              <span className="pill pill-info">生效中</span>
            </div>
            <p className="model-popover-desc">
              模型适配器由守护进程启动环境变量托管，界面不保存 API 密钥，也不会伪造未接入后端的在线切换。
            </p>
          </div>
          <p className="popover-footnote">
            会话级实时切换模型待后端控制面 RPC 接入后开放。
          </p>
          <button
            type="button"
            className="popover-action-btn"
            onClick={() => {
              setOpen(false);
              onOpenModelSettings();
            }}
          >
            <IconSettings size={14} />
            <span>打开「模型与服务商」查看配置方式</span>
          </button>
        </div>
      ) : null}
    </div>
  );
}
