/**
 * 记忆面板的状态机（M1）：把"调 RPC + 处理错误 + 不乐观更新"从组件里拆出来。
 *
 * 为什么单独一层：界面里最容易骗人的地方是**写操作之后自己猜新状态**。记忆有
 * revision，猜了就会和库不一致。这里的纪律是——写完就重新拉一遍，拿库的说法。
 *
 * 冲突（`conflict` = revision 过期）**不自动重试**：自动重放会把"你看到的版本"与
 * "库里现在的版本"混成一次悄悄的成功；正确动作是刷新列表，让人重新确认。
 */

import type { MemoryApi } from "../api/memory";
import type { MemoryScopeChoice, MemorySearchResult, MemoryView } from "../api/types";

export type MemoryPanelPhase = "idle" | "loading" | "ready" | "error";

export interface MemoryPanelState {
  phase: MemoryPanelPhase;
  /** 列表或检索结果（检索时同时带上能力标注）。 */
  items: MemoryView[];
  result: MemorySearchResult | null;
  query: string;
  scope: MemoryScopeChoice;
  /** 面板顶部的人话提示（成功/冲突/降级说明）。 */
  notice: string | null;
  error: string | null;
  /** 正在进行的写操作（按钮禁用，防重复提交）。 */
  busy: string | null;
}

const USER_SCOPE: MemoryScopeChoice = { scope: "user", scope_id: null };

function initialState(scope: MemoryScopeChoice = USER_SCOPE): MemoryPanelState {
  return {
    phase: "idle",
    items: [],
    result: null,
    query: "",
    scope,
    notice: null,
    error: null,
    busy: null,
  };
}

function messageOf(error: unknown): { text: string; conflict: boolean } {
  const code =
    typeof error === "object" && error !== null && "code" in error
      ? String((error as { code: unknown }).code)
      : "";
  const raw = error instanceof Error ? error.message : String(error);
  if (code === "conflict") {
    return {
      text: "这条记忆已经被改过（你看到的 revision 过期了）。已刷新列表，请重新确认。",
      conflict: true,
    };
  }
  if (code === "not_found") return { text: "这条记忆不存在或不可见。", conflict: false };
  if (code === "bad_params") return { text: `请求参数不对：${raw}`, conflict: false };
  if (code === "unreachable" || code === "timeout") {
    return { text: "连不上守护进程，记忆操作没有执行。", conflict: false };
  }
  return { text: raw === "" ? "记忆操作失败。" : raw, conflict: false };
}

export class MemoryController {
  private state: MemoryPanelState;

  constructor(
    private readonly api: MemoryApi,
    private readonly onChange: (state: MemoryPanelState) => void,
    scope: MemoryScopeChoice = USER_SCOPE,
  ) {
    this.state = initialState(scope);
  }

  get current(): MemoryPanelState {
    return this.state;
  }

  private emit(patch: Partial<MemoryPanelState>): void {
    this.state = { ...this.state, ...patch };
    this.onChange(this.state);
  }

  /** 换可见作用域：直接重拉列表（不是把已有结果过滤一遍——那会骗人）。 */
  async setScope(scope: MemoryScopeChoice): Promise<void> {
    this.emit({ scope, query: "", result: null });
    await this.load();
  }

  async load(): Promise<void> {
    this.emit({ phase: "loading", error: null });
    try {
      const list = await this.api.list(this.state.scope, null);
      this.emit({
        phase: "ready",
        items: list.items,
        result: null,
        notice: null,
        error: null,
      });
    } catch (error) {
      this.emit({ phase: "error", error: messageOf(error).text });
    }
  }

  async search(query: string): Promise<void> {
    const trimmed = query.trim();
    if (trimmed === "") {
      await this.load();
      return;
    }
    this.emit({ phase: "loading", error: null, query: trimmed });
    try {
      const result = await this.api.search(trimmed, this.state.scope);
      this.emit({
        phase: "ready",
        items: result.items,
        result,
        notice: null,
        error: null,
      });
    } catch (error) {
      this.emit({ phase: "error", error: messageOf(error).text });
    }
  }

  /** 写操作统一走这里：**不乐观更新**，成功后重新拉一遍列表。 */
  private async write(
    label: string,
    action: () => Promise<unknown>,
    success: string,
  ): Promise<void> {
    this.emit({ busy: label, error: null, notice: null });
    try {
      await action();
      const list = await this.api.list(this.state.scope, null);
      this.emit({
        phase: "ready",
        items: list.items,
        result: null,
        busy: null,
        notice: success,
        error: null,
      });
    } catch (error) {
      const message = messageOf(error);
      if (message.conflict) {
        // 冲突：刷新 + 提示；**不**自动重试（自动重放会悄悄替你确认一个你没看过的版本）。
        try {
          const list = await this.api.list(this.state.scope, null);
          this.emit({
            phase: "ready",
            items: list.items,
            result: null,
            busy: null,
            notice: message.text,
            error: null,
          });
          return;
        } catch {
          // 连刷新都失败：保留原错误，不装作刷新成功。
        }
      }
      this.emit({ phase: "error", busy: null, error: message.text });
    }
  }

  confirm(item: MemoryView): Promise<void> {
    return this.write(
      `confirm:${item.memory_id}`,
      () => this.api.confirm(item.memory_id, item.revision),
      "已确认：这条记忆会参与召回。",
    );
  }

  forget(item: MemoryView): Promise<void> {
    return this.write(
      `forget:${item.memory_id}`,
      () => this.api.forget(item.memory_id, item.revision),
      "已忘记：不再召回，事件日志里的记录仍在。",
    );
  }

  erase(item: MemoryView): Promise<void> {
    return this.write(
      `erase:${item.memory_id}`,
      () => this.api.erase(item.memory_id, item.revision),
      "已删除正文副本与索引；事件日志里的读快照仍保留（不是物理抹除整库）。",
    );
  }

  remember(kind: string, content: string): Promise<void> {
    return this.write(
      "remember",
      () => this.api.remember(kind, content),
      "已记住：写入即生效（人自己就是签发者）。",
    );
  }

  /** 点开一条才取正文（列表刻意不带正文）。 */
  async openContent(memoryId: string): Promise<MemoryView | null> {
    try {
      return await this.api.get(memoryId);
    } catch (error) {
      this.emit({ error: messageOf(error).text });
      return null;
    }
  }
}
