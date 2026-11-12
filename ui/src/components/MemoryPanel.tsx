/**
 * 记忆面板（G2 记忆阶段 M1）：**纯展示**——状态由 `MemoryController` 给，动作由
 * 回调给。这样测试可以用 `renderToStaticMarkup` 直接喂状态（本仓库 UI 测试不接
 * jsdom 点击），也让"渲染"与"取数"的边界清楚。
 *
 * 三条不许简化的渲染纪律：
 * 1. 正文**纯文本**渲染（React 默认转义）。记忆内容来自模型/用户输入，绝不能当
 *    HTML 塞进 DOM。
 * 2. `stale` 三态：后端省略 = 判不了，不显示"新鲜"。
 * 3. 能力标注照实显示（`semantic=false` 时把 `degraded` 原因给出来），空结果时把
 *    可见作用域一并给出来——否则人分不清"没这条"与"没覆盖到"。
 */

import type { MemoryScopeChoice, MemoryView } from "../api/types";
import type { MemoryPanelState } from "../lib/memoryController";
import {
  memoryCapabilityText,
  memoryEvidenceText,
  memoryLayerLabel,
  memoryOriginLabel,
  memoryScoreText,
  memoryScopesText,
  memoryStaleText,
  memoryStatusLabel,
  memorySummaryText,
} from "../lib/memoryView";

export const MEMORY_KIND_OPTIONS = [
  { value: "preference", label: "偏好（你怎么要求我做事）" },
  { value: "fact", label: "事实（关于你或你的项目）" },
  { value: "decision", label: "决定（已经定下来的）" },
  { value: "procedure", label: "流程（固定做法）" },
] as const;

export interface MemoryPanelCallbacks {
  onSearch: (query: string) => void;
  onRefresh: () => void;
  onScopeChange: (scope: MemoryScopeChoice) => void;
  onOpenContent: (item: MemoryView) => void;
  onConfirm: (item: MemoryView) => void;
  onForget: (item: MemoryView) => void;
  onErase: (item: MemoryView) => void;
  onRemember: (kind: string, content: string) => void;
}

export interface MemoryPanelViewProps extends MemoryPanelCallbacks {
  state: MemoryPanelState;
}

function scopeValue(scope: MemoryScopeChoice): string {
  return scope.scope === "user" ? "user" : `${scope.scope}:${scope.scope_id ?? ""}`;
}

function parseScopeValue(raw: string): MemoryScopeChoice {
  if (raw === "user") return { scope: "user", scope_id: null };
  const [scope, ...rest] = raw.split(":");
  const scopeId = rest.join(":") === "" ? null : rest.join(":");
  if (scope === "task" || scope === "workspace") return { scope, scope_id: scopeId };
  return { scope: "user", scope_id: null };
}

/** 一条记忆的"凭什么"与动作。正文不在这里展开（`memory.get` 才取）。 */
export function MemoryRow({
  item,
  busy,
  onOpenContent,
  onConfirm,
  onForget,
  onErase,
}: {
  item: MemoryView;
  busy: string | null;
  onOpenContent: (item: MemoryView) => void;
  onConfirm: (item: MemoryView) => void;
  onForget: (item: MemoryView) => void;
  onErase: (item: MemoryView) => void;
}) {
  const staleText = memoryStaleText(item);
  const scoreText = memoryScoreText(item);
  const writeBusy =
    busy === `confirm:${item.memory_id}` ||
    busy === `forget:${item.memory_id}` ||
    busy === `erase:${item.memory_id}`;
  const canConfirm = item.status === "candidate";
  const canForget = item.status === "candidate" || item.status === "active";
  const canErase = item.erased_at === null && item.status !== "superseded";
  return (
    <li className="memory-row" data-memory-id={item.memory_id}>
      <div className="memory-row-head">
        <strong>{memoryStatusLabel(item.status)}</strong>
        <span className="memory-meta">
          {memoryOriginLabel(item.origin)} · {memoryLayerLabel(item.layer)} · r{item.revision}
        </span>
      </div>
      <p className="memory-scope">
        {item.scope}
        {item.scope_id !== null ? ` · ${item.scope_id}` : ""}
      </p>
      <p className="memory-summary">{memorySummaryText(item.content)}</p>
      <p className="memory-evidence">{memoryEvidenceText(item)}</p>
      {scoreText !== null ? <p className="memory-score">{scoreText}</p> : null}
      {staleText !== null ? (
        <p className={item.stale === true ? "memory-stale" : "memory-fresh"}>{staleText}</p>
      ) : null}
      <div className="memory-actions">
        <button
          type="button"
          className="btn btn-ghost btn-sm"
          onClick={() => onOpenContent(item)}
          disabled={writeBusy}
        >
          看正文
        </button>
        {canConfirm ? (
          <button
            type="button"
            className="btn btn-primary btn-sm"
            onClick={() => onConfirm(item)}
            disabled={writeBusy}
          >
            确认并启用
          </button>
        ) : null}
        {canForget ? (
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            onClick={() => onForget(item)}
            disabled={writeBusy}
          >
            忘记
          </button>
        ) : null}
        {canErase ? (
          <button
            type="button"
            className="btn btn-danger btn-sm"
            onClick={() => onErase(item)}
            disabled={writeBusy}
          >
            删除正文副本
          </button>
        ) : null}
      </div>
    </li>
  );
}

export function MemoryPanelView({
  state,
  onSearch,
  onRefresh,
  onScopeChange,
  onOpenContent,
  onConfirm,
  onForget,
  onErase,
  onRemember,
}: MemoryPanelViewProps) {
  const capability = state.result === null ? null : memoryCapabilityText(state.result);
  const scopes = state.result === null ? null : memoryScopesText(state.result.scopes);
  return (
    <div className="memory-panel">
      <div className="memory-toolbar">
        <label className="field">
          <span className="field-label">可见作用域</span>
          <select
            className="input"
            defaultValue={scopeValue(state.scope)}
            onChange={(event) => onScopeChange(parseScopeValue(event.target.value))}
          >
            <option value="user">我自己的记忆（user）</option>
            <option value="task:">某个任务（task）</option>
            <option value="workspace:">某个目录（workspace）</option>
          </select>
        </label>
        {state.scope.scope !== "user" ? (
          <label className="field">
            <span className="field-label">
              {state.scope.scope === "task" ? "任务 id" : "目录绝对路径"}
            </span>
            <input
              className="input"
              defaultValue={state.scope.scope_id ?? ""}
              onBlur={(event) =>
                onScopeChange({ ...state.scope, scope_id: event.target.value.trim() || null })
              }
            />
          </label>
        ) : null}
        <label className="field memory-search-field">
          <span className="field-label">检索</span>
          <input
            className="input"
            placeholder="例如：周报 偏好"
            defaultValue={state.query}
            onKeyDown={(event) => {
              if (event.key === "Enter") onSearch(event.currentTarget.value);
            }}
          />
        </label>
        <button type="button" className="btn btn-primary btn-sm" onClick={onRefresh}>
          刷新
        </button>
      </div>

      <p className="field-hint">
        这里看到的就是 Agent 能召回的（默认只有你自己的记忆）；「候选」要你确认才参与召回。
      </p>

      {state.notice !== null ? (
        <p className="banner banner-warn" role="status">
          {state.notice}
        </p>
      ) : null}
      {state.error !== null ? (
        <p className="banner banner-danger" role="alert">
          {state.error}
        </p>
      ) : null}

      {capability !== null ? <p className="field-hint memory-capability">{capability}</p> : null}
      {scopes !== null ? <p className="field-hint memory-scopes">{scopes}</p> : null}

      {state.phase === "loading" ? <p className="field-hint">正在读取记忆…</p> : null}

      {state.phase !== "loading" && state.items.length === 0 ? (
        <p className="empty-hint">
          {state.result === null
            ? "这个作用域里还没有记忆。你可以直接写一条记住，或者让 Agent 先提议。"
            : "没有命中。空结果不等于“库里没有”——看上面的能力标注（可能只有词面召回）。"}
        </p>
      ) : null}

      {state.items.length > 0 ? (
        <ul className="memory-list">
          {state.items.map((item) => (
            <MemoryRow
              key={item.memory_id}
              item={item}
              busy={state.busy}
              onOpenContent={onOpenContent}
              onConfirm={onConfirm}
              onForget={onForget}
              onErase={onErase}
            />
          ))}
        </ul>
      ) : null}

      <details className="memory-remember">
        <summary>亲手记住一条</summary>
        <p className="field-hint">
          你写的记忆直接生效（人就是签发者）。删除正文副本仍要单独确认，且事件日志里的读快照会保留。
        </p>
        <label className="field">
          <span className="field-label">类别</span>
          <select className="input" defaultValue="preference" id="memory-kind">
            {MEMORY_KIND_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span className="field-label">内容</span>
          <textarea className="textarea" id="memory-content" rows={3} placeholder="例如：以后周报都用中文写，先给结论。" />
        </label>
        <button
          type="button"
          className="btn btn-primary btn-sm"
          disabled={state.busy === "remember"}
          onClick={() => {
            const kind = document.getElementById("memory-kind");
            const content = document.getElementById("memory-content");
            const kindValue = kind instanceof HTMLSelectElement ? kind.value : "preference";
            const contentValue = content instanceof HTMLTextAreaElement ? content.value : "";
            onRemember(kindValue, contentValue);
          }}
        >
          记住
        </button>
      </details>
    </div>
  );
}
