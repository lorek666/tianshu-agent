import { useEffect, useRef } from "react";
import type { MouseEvent, ReactNode } from "react";
import { IconX } from "./Icons";

export interface ModalProps {
  title: string;
  subtitle?: string;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
  wide?: boolean;
  /** 请求进行中时禁止点遮罩/Esc 关闭，避免请求结果无处安放。 */
  closeDisabled?: boolean;
}

export function Modal({ title, subtitle, onClose, children, footer, wide = false, closeDisabled = false }: ModalProps) {
  const cardRef = useRef<HTMLDivElement>(null);
  const previousFocus = useRef<HTMLElement | null>(null);

  useEffect(() => {
    previousFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    cardRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !closeDisabled) onClose();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      previousFocus.current?.focus();
    };
  }, [onClose, closeDisabled]);

  const handleBackdrop = (event: MouseEvent<HTMLDivElement>) => {
    if (event.target === event.currentTarget && !closeDisabled) onClose();
  };

  return (
    <div className="modal-backdrop" onMouseDown={handleBackdrop}>
      <div
        className={`modal-card${wide ? " modal-wide" : ""}`}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        tabIndex={-1}
        ref={cardRef}
      >
        <div className="modal-head">
          <div className="modal-heading">
            <h2>{title}</h2>
            {subtitle !== undefined && subtitle !== "" ? <p>{subtitle}</p> : null}
          </div>
          <button type="button" className="icon-btn" onClick={onClose} disabled={closeDisabled} aria-label="关闭">
            <IconX />
          </button>
        </div>
        <div className="modal-body">{children}</div>
        {footer !== undefined ? <div className="modal-foot">{footer}</div> : null}
      </div>
    </div>
  );
}