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

function messageOf(error: unknown, writing = false): { text: string; conflict: boolean } {
  const code =
    typeof error === "object" && error !== null && "code" in error
      ? String((error as { code: unknown }).code)
      : "";
  const raw = error instanceof Error ? error.message : String(error);
  if (code === "conflict") {
    return {
      text: "这条记忆已经被改过（你看到的 revision 过期了）。",
      conflict: true,
    };
  }
  if (code === "not_found") return { text: "这条记忆不存在或不可见。", conflict: false };
  if (code === "bad_params") return { text: `请求参数不对：${raw}`, conflict: false };
  if (code === "unreachable" || code === "timeout") {
    return {
      text: writing
        ? "与守护进程的连接中断，无法确认记忆操作结果。请先刷新核对，再决定是否重试。"
        : "与守护进程的连接中断，请刷新重试。",
      conflict: false,
    };
  }
  return { text: raw === "" ? "记忆操作失败。" : raw, conflict: false };
}

export class MemoryController {
  private state: MemoryPanelState;
  private viewGeneration = 0;

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
    this.emit({ scope, query: "", items: [], result: null });
    await this.load();
  }

  async load(): Promise<void> {
    const generation = ++this.viewGeneration;
    this.emit({ phase: "loading", error: null, query: "" });
    try {
      const list = await this.api.list(this.state.scope, null);
      if (generation !== this.viewGeneration) return;
      this.emit({
        phase: "ready",
        items: list.items,
        result: null,
        notice: null,
        error: null,
      });
    } catch (error) {
      if (generation !== this.viewGeneration) return;
      this.emit({ phase: "error", error: messageOf(error).text });
    }
  }

  async search(query: string): Promise<void> {
    const trimmed = query.trim();
    if (trimmed === "") {
      await this.load();
      return;
    }
    const generation = ++this.viewGeneration;
    this.emit({ phase: "loading", error: null, query: trimmed });
    try {
      const result = await this.api.search(trimmed, this.state.scope);
      if (generation !== this.viewGeneration) return;
      this.emit({
        phase: "ready",
        items: result.items,
        result,
        notice: null,
        error: null,
      });
    } catch (error) {
      if (generation !== this.viewGeneration) return;
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
    let notice = success;
    let conflict = false;
    try {
      await action();
    } catch (error) {
      const message = messageOf(error, true);
      if (!message.conflict) {
        this.emit({ phase: "error", busy: null, error: message.text });
        return;
      }
      // revision 冲突只刷新，不自动重放写请求。
      notice = message.text;
      conflict = true;
    }

    // 写操作的结果已经确定；接下来的刷新是另一条请求，不能改写这个事实。
    // 与 load/search 共用代次，晚到的刷新不得覆盖用户新选择的作用域或查询。
    const generation = ++this.viewGeneration;
    const { scope, query } = this.state;
    try {
      const result = query ? await this.api.search(query, scope) : null;
      const list = result ?? await this.api.list(scope, null);
      if (generation === this.viewGeneration) {
        this.emit({
          phase: "ready", items: list.items, result, busy: null,
          notice: conflict ? `${notice}已刷新列表，请重新确认。` : notice,
          error: null,
        });
      } else {
        this.emit({ busy: null, notice });
      }
    } catch (error) {
      if (generation === this.viewGeneration) {
        this.emit({
          phase: "error", busy: null, notice: conflict ? null : notice,
          error: `${conflict ? notice : ""}列表刷新失败：${messageOf(error).text}`,
        });
      } else {
        this.emit({ busy: null, notice });
      }
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
