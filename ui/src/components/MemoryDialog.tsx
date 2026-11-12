/**
 * 记忆对话框（G2 记忆阶段 M1）：把 `MemoryApi` + `MemoryController` 接到 `Modal` 上。
 *
 * 取数与状态在 `MemoryController`（可单测），这里只负责：打开时加载、把回调转给
 * 控制器、两个本地交互（看正文 / 删除前二次确认）。**删除永远要二次确认**：
 * `memory.erase` 会删正文副本与索引，不是"停召回"（那是 `memory.forget`）。
 */

import { useEffect, useMemo, useState } from "react";

import type { MemoryApi } from "../api/memory";
import type { MemoryView } from "../api/types";
import { MemoryController } from "../lib/memoryController";
import type { MemoryPanelState } from "../lib/memoryController";
import { memoryStatusLabel } from "../lib/memoryView";
import { MemoryPanelView } from "./MemoryPanel";
import { Modal } from "./Modal";

const INITIAL: MemoryPanelState = {
  phase: "idle",
  items: [],
  result: null,
  query: "",
  scope: { scope: "user", scope_id: null },
  notice: null,
  error: null,
  busy: null,
};

export interface MemoryDialogProps {
  open: boolean;
  api: MemoryApi;
  onClose: () => void;
}

export function MemoryDialog({ open, api, onClose }: MemoryDialogProps) {
  const [state, setState] = useState<MemoryPanelState>(INITIAL);
  const [detail, setDetail] = useState<MemoryView | null>(null);
  const [eraseTarget, setEraseTarget] = useState<MemoryView | null>(null);

  const controller = useMemo(
    () => (open ? new MemoryController(api, setState) : null),
    [open, api],
  );

  useEffect(() => {
    if (controller === null) return;
    void controller.load();
  }, [controller]);

  // 关掉时清掉本地交互状态：下次打开不该看到上次的正文/确认框。
  useEffect(() => {
    if (!open) {
      setDetail(null);
      setEraseTarget(null);
    }
  }, [open]);

  if (!open || controller === null) return null;

  return (
    <Modal
      title="记忆"
      subtitle="Agent 能召回的长期内容。候选要你确认才生效；忘记只是停止召回，删除会去掉正文副本。"
      onClose={onClose}
      closeDisabled={state.busy !== null}
      wide
    >
      <MemoryPanelView
        state={state}
        onSearch={(query) => void controller.search(query)}
        onRefresh={() => void controller.load()}
        onScopeChange={(scope) => {
          setDetail(null);
          void controller.setScope(scope);
        }}
        onOpenContent={(item) => {
          void controller.openContent(item.memory_id).then((full) => setDetail(full ?? item));
        }}
        onConfirm={(item) => void controller.confirm(item)}
        onForget={(item) => void controller.forget(item)}
        onErase={(item) => setEraseTarget(item)}
        onRemember={(kind, content) => {
          if (content.trim() === "") {
            setState((previous) => ({ ...previous, error: "内容不能为空。" }));
            return;
          }
          void controller.remember(kind, content);
        }}
      />

      {detail !== null ? (
        <section className="memory-detail" aria-label="记忆正文">
          <h3>正文（纯文本，原样显示）</h3>
          <p className="field-hint">
            {memoryStatusLabel(detail.status)} · {detail.scope}
            {detail.scope_id !== null ? ` · ${detail.scope_id}` : ""} · r{detail.revision}
          </p>
          <pre className="memory-content">
            {detail.content_state === "unavailable"
              ? "正文不可用：这条记忆的正文快照已不在（不会被替换成库里的最新版本）。"
              : (detail.content ?? "（正文为空）")}
          </pre>
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => setDetail(null)}>
            收起
          </button>
        </section>
      ) : null}

      {eraseTarget !== null ? (
        <section className="memory-confirm" aria-label="确认删除记忆正文">
          <p className="banner banner-danger" role="alert">
            要删除这条记忆的正文副本与检索索引吗？事件日志里的读快照仍会保留——这不是「物理抹除整库」。
          </p>
          <pre className="memory-content">{eraseTarget.content ?? "（正文为空）"}</pre>
          <div className="memory-actions">
            <button
              type="button"
              className="btn btn-danger btn-sm"
              onClick={() => {
                const target = eraseTarget;
                setEraseTarget(null);
                void controller.erase(target);
              }}
            >
              确认删除
            </button>
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              onClick={() => setEraseTarget(null)}
            >
              取消
            </button>
          </div>
        </section>
      ) : null}
    </Modal>
  );
}
