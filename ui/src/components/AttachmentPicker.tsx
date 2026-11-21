import { useRef, useState } from "react";
import { checkAttachmentFiles, TEXT_ATTACHMENT_ACCEPT } from "../lib/attachments";
import { IconPlus, IconX } from "./Icons";

interface Props {
  files: File[];
  onChange: (files: File[]) => void;
  disabled: boolean;
  allowImages?: boolean;
}

export function AttachmentPicker({ files, onChange, disabled, allowImages = false }: Props) {
  const input = useRef<HTMLInputElement>(null);
  const [error, setError] = useState<string | null>(null);
  return (
    <div className="composer-attachments">
      <input
        type="file"
        hidden
        multiple
        ref={input}
        disabled={disabled}
        aria-label="选择要随本次运行发送的附件"
        accept={TEXT_ATTACHMENT_ACCEPT + (allowImages ? ",.png,.jpg,.jpeg,image/png,image/jpeg" : "")}
        onChange={(event) => {
          const picked = Array.from(event.currentTarget.files ?? []);
          event.currentTarget.value = "";
          if (!picked.length) return;
          const combined = [...files, ...picked];
          try {
            checkAttachmentFiles(combined, allowImages);
            onChange(combined);
            setError(null);
          } catch (caught) {
            setError(caught instanceof Error ? caught.message : "无法添加附件。");
          }
        }}
      />
      <button type="button" className="chip-btn" disabled={disabled} onClick={() => input.current?.click()}>
        <IconPlus size={12} /> 附件
      </button>
      {files.map((file, index) => (
        <span key={`${file.name}:${index}`} className="goal-ws-tag">
          <span className="chip-truncate" title={file.name}>{file.name}</span>
          <button type="button" className="chip-clear-btn" disabled={disabled}
            aria-label={`移除附件 ${file.name}`} onClick={() => {
              onChange(files.filter((_, item) => item !== index));
              setError(null);
            }}>
            <IconX size={11} />
          </button>
        </span>
      ))}
      {files.length > 0 ? <p className="field-hint">附件会随运行发送给所选模型。</p> : null}
      {error !== null ? <p className="form-error" role="alert">{error}</p> : null}
    </div>
  );
}
