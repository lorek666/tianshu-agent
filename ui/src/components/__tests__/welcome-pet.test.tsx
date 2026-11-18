// @vitest-environment jsdom

import { act } from "react";
import { createRoot } from "react-dom/client";
import type { Root } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ApprovalView, StateView } from "../../api/types";
import type { EventStreamCallbacks, HostClient, RpcParams } from "../../api/host";

const mocked = vi.hoisted(() => ({
  host: null as unknown,
}));

vi.mock("../../api/host", () => ({
  createRealHostClient: () => mocked.host,
}));

vi.mock("../../api/token", () => ({
  isDemoRequested: () => false,
  readHostToken: () => "test-token",
}));

import { App } from "../../App";
import { WelcomePet } from "../WelcomePet";
import type { WelcomePetStatus } from "../WelcomePet";

function emptyStateView(): StateView {
  return {
    tasks: [],
    runs: [],
    approvals: [] as ApprovalView[],
    event_seq_head: 1,
    daemon: {
      version: "0.1.0",
      started_at: 1,
      data_root: "/tmp/data",
    },
  };
}

class EmptyFakeHost implements HostClient {
  readonly mode = "host" as const;
  readonly state: StateView;
  failGetState = false;
  streamCallbacks: EventStreamCallbacks | null = null;
  private streamAbort: (() => void) | null = null;

  readonly rpc = vi.fn(async (_method: string, _params: RpcParams = {}) => ({})) as unknown as HostClient["rpc"];

  constructor(initial: StateView = emptyStateView()) {
    this.state = structuredClone(initial);
  }

  async getState(): Promise<StateView> {
    if (this.failGetState) {
      throw new Error("daemon offline");
    }
    return structuredClone(this.state);
  }

  async openEvents(
    _sinceSeq: number,
    callbacks: EventStreamCallbacks,
    signal: AbortSignal,
  ): Promise<void> {
    this.streamCallbacks = callbacks;
    callbacks.onOpen?.();
    return new Promise<void>((resolve) => {
      this.streamAbort = resolve;
      if (signal.aborted) {
        resolve();
        return;
      }
      signal.addEventListener("abort", () => resolve(), { once: true });
    });
  }

  failAllReconnects(): void {
    this.openEvents = async () => {
      throw new Error("stream broken");
    };
    this.streamAbort?.();
  }
}

let root: Root | null = null;
let container: HTMLDivElement | null = null;

beforeEach(() => {
  (
    globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }
  ).IS_REACT_ACT_ENVIRONMENT = true;
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});

afterEach(async () => {
  if (root !== null) {
    await act(async () => root?.unmount());
  }
  container?.remove();
  root = null;
  container = null;
  mocked.host = null;
  vi.clearAllMocks();
});

async function settle(): Promise<void> {
  await act(async () => {
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
  });
}

async function waitFor(condition: () => boolean): Promise<void> {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    if (condition()) return;
    await settle();
  }
  throw new Error("等待欢迎页条件超时");
}

describe("主界面中央空状态欢迎页与动态宠物", () => {
  it("空任务列表时在中央欢迎页展示动态宠物、欢迎使用天枢标题与一体化输入框，且不含冗余问候或 Nova 字样", async () => {
    const host = new EmptyFakeHost();
    mocked.host = host;
    await act(async () => root?.render(<App />));
    await waitFor(() => container?.querySelector(".center-empty") !== null);

    const center = container?.querySelector(".center-empty");
    expect(center).not.toBeNull();

    const pet = center?.querySelector('[data-testid="welcome-pet"]');
    const welcomeHeading = center?.querySelector(".welcome-title");
    const composer = center?.querySelector(".composer-shell");
    expect(pet).not.toBeNull();
    expect(welcomeHeading?.textContent?.trim()).toBe("欢迎使用天枢");
    expect(composer).not.toBeNull();

    // 宠物位于欢迎文案上方（DOM 顺序在标题之前）
    const relation = pet!.compareDocumentPosition(welcomeHeading!);
    expect(relation & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

    // 严格遵守命名要求与精简要求：界面与属性均不出现 Nova / NOVA 字样，不含「你好，欢迎回来」与「回车发送」
    expect(container?.innerHTML).not.toMatch(/nova/i);
    expect(container?.textContent).not.toContain("你好，欢迎回来");
    expect(container?.textContent).not.toContain("回车发送");
  });

  it("七种姿态渲染各自的立体特征部件，且多实例同屏无 SVG defs ID 冲突", () => {
    const statuses: WelcomePetStatus[] = [
      "welcome",
      "idle",
      "thinking",
      "executing",
      "done",
      "error",
      "sleeping",
    ];
    const html = renderToStaticMarkup(
      <div>
        {statuses.map((st) => (
          <WelcomePet key={st} status={st} />
        ))}
      </div>,
    );

    expect(html).not.toMatch(/nova/i);
    expect(html).toContain('data-pet-part="pedestal"');
    expect(html).toContain('data-pet-part="orbital-ribbon"');
    expect(html).toContain('data-pet-part="holo-panels"');
    expect(html).toContain('data-pet-part="hud-screens"');
    expect(html).toContain('data-pet-part="sparkles"');
    expect(html).toContain('data-pet-part="warning-sign"');
    expect(html).toContain('data-pet-part="sleep-zzz"');

    const ids = [...html.matchAll(/\sid="([^"]+)"/g)].map((match) => match[1]);
    expect(ids.length).toBeGreaterThan(30);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("支持鼠标 2.5D 眼神视差追踪与点击切换互动表情", async () => {
    await act(async () => root?.render(<WelcomePet interactive />));
    const stage = container?.querySelector<HTMLElement>('[data-testid="welcome-pet"]');
    expect(stage).not.toBeNull();

    stage!.getBoundingClientRect = () =>
      ({
        left: 0,
        top: 0,
        width: 280,
        height: 280,
        right: 280,
        bottom: 280,
        x: 0,
        y: 0,
        toJSON: () => ({}),
      }) as DOMRect;

    const eyesBefore = stage?.querySelector(".tp-pet-eyes")?.getAttribute("transform");
    await act(async () => {
      stage?.dispatchEvent(
        new MouseEvent("mousemove", { bubbles: true, clientX: 260, clientY: 40 }),
      );
    });
    const eyesMoved = stage?.querySelector(".tp-pet-eyes")?.getAttribute("transform");
    expect(eyesMoved).not.toBe(eyesBefore);

    await act(async () => {
      stage?.dispatchEvent(
        new MouseEvent("mouseout", { bubbles: true, relatedTarget: document.body }),
      );
    });
    expect(stage?.querySelector(".tp-pet-eyes")?.getAttribute("transform")).toBe(eyesBefore);

    // 点击宠物本体可循环互动姿态（欢迎 -> 待命 -> 思考 -> 执行 -> 完成 -> 休眠）
    const initialLabel = stage?.querySelector("svg")?.getAttribute("aria-label");
    await act(async () => {
      stage?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    const nextLabel = stage?.querySelector("svg")?.getAttribute("aria-label");
    expect(nextLabel).not.toBe(initialLabel);
  });

  it("与守护进程连接中断时欢迎页宠物自动切为异常提醒态，恢复连接后回归欢迎态", async () => {
    const host = new EmptyFakeHost();
    mocked.host = host;
    await act(async () => root?.render(<App />));
    await waitFor(() => container?.querySelector('[data-testid="welcome-pet"]') !== null);

    expect(container?.querySelector('[data-pet-part="warning-sign"]')).toBeNull();

    // 连续两次刷新失败触发 connectionLost
    host.failGetState = true;
    const refreshBtn = container?.querySelector<HTMLButtonElement>('button[aria-label="刷新"]');
    expect(refreshBtn).not.toBeNull();
    await act(async () => {
      refreshBtn?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    await settle();
    await act(async () => {
      refreshBtn?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    await waitFor(() => container?.querySelector('[data-pet-part="warning-sign"]') !== null);

    // 守护进程恢复后点击刷新，自动回归欢迎姿态
    host.failGetState = false;
    await act(async () => {
      refreshBtn?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    await waitFor(() => container?.querySelector('[data-pet-part="warning-sign"]') === null);
    expect(container?.querySelector('[data-pet-part="orbital-ribbon"]')).not.toBeNull();
  });
});
