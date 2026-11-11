import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import type { ApprovalView } from "../../api/types";
import { ApprovalCard } from "../ApprovalPanel";

function approval(overrides: Partial<ApprovalView> = {}): ApprovalView {
  return {
    approval_id: "appr_1",
    run_id: "run_1",
    task_id: "task_1",
    action: "c4.exec",
    resources: [],
    reason: "执行命令需要你确认",
    rule_id: "c4.exec",
    isolation: null,
    state: "pending",
    created_at: 1,
    expires_at: 10_000,
    ...overrides,
  };
}

function render(value: ApprovalView): string {
  return renderToStaticMarkup(
    <ApprovalCard
      approval={value}
      task={null}
      now={2}
      busy={null}
      onDecide={() => undefined}
    />,
  );
}

describe("审批卡片：批准之前就把隔离真实情况说出来", () => {
  it("没有隔离视图时不渲染隔离提示（非 exec 动作）", () => {
    const html = render(approval());
    expect(html).not.toContain("受信任执行");
    expect(html).not.toContain("隔离");
  });

  it("受信任执行在审批卡片里可见", () => {
    const html = render(
      approval({
        isolation: {
          backend: "local-posix",
          declared: "none",
          measured: null,
          level: "none",
          unverified: true,
          contradicted: false,
          boundaries: { files_authority: "unknown" },
          enforced_boundaries: [],
        },
      }),
    );
    expect(html).toContain("受信任执行");
    expect(html).toContain("未隔离");
  });

  it("自述与实测冲突时，卡片显示两类级别而不是只说未隔离", () => {
    const html = render(
      approval({
        isolation: {
          backend: "fake",
          declared: "app_container",
          measured: "none",
          level: "none",
          unverified: false,
          contradicted: true,
          boundaries: {},
          enforced_boundaries: [],
        },
      }),
    );
    expect(html).toContain("自述");
    expect(html).toContain("实测");
    expect(html).not.toContain("受信任执行");
  });
});
