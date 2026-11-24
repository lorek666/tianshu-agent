import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import type { ApprovalView } from "../../api/types";
import { ApprovalCard } from "../ApprovalPanel";

function approval(overrides: Partial<ApprovalView> = {}): ApprovalView {
  return {
    approval_id: "appr_1",
    run_id: "run_1",
    task_id: "task_1",
    action: "c4.delete",
    resources: [],
    reason: "删除文件需要你确认",
    rule_id: "c4.delete",
    isolation: null,
    command: null,
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
        action: "exec",
        rule_id: "c4.exec",
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
        action: "exec",
        rule_id: "c4.exec",
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

describe("审批卡片：完整命令详情", () => {
  const localIsolation = {
    backend: "local-posix",
    declared: "none" as const,
    measured: null,
    level: "none" as const,
    unverified: true,
    contradicted: false,
    boundaries: {},
    enforced_boundaries: [],
  };

  it("逐项展示完整 argv、实际 cwd、后端和隔离级别", () => {
    const html = render(approval({
      action: "exec",
      rule_id: "c4.exec",
      isolation: localIsolation,
      command: {
        argv: ["/usr/bin/python3", "-c", "print('secret')", "", "line1\nline2"],
        cwd: "/Volumes/Work/project/nested directory",
        backend: "local",
      },
    }));
    expect(html).toContain("/usr/bin/python3");
    expect(html).toContain("-c");
    expect(html).toContain("print(&#x27;secret&#x27;)");
    expect(html).toContain("&quot;&quot;");
    expect(html).toContain("line1\\nline2");
    expect(html).toContain("/Volumes/Work/project/nested directory");
    expect(html).toContain("执行后端");
    expect(html).toContain("local");
    expect(html).toContain("隔离级别");
    expect(html).toContain("未隔离");
    expect(html).toContain("受信任执行");
    expect(html).toContain("共 5 项 · 完整显示");
    expect(html).not.toContain("缺少完整命令详情");
  });

  it("长参数完整保留，不截断也不加省略号", () => {
    const tail = `tail-${"x".repeat(2_000)}`;
    const cwd = `/tmp/${"long-directory-".repeat(100)}project`;
    const html = render(approval({
      action: "exec",
      rule_id: "c4.exec",
      command: { argv: ["/bin/echo", tail], cwd, backend: "local" },
    }));
    expect(html).toContain(tail);
    expect(html).toContain(cwd);
    expect(html).not.toContain("…");
  });

  it("argv、cwd、后端里的 HTML 都按纯文本转义，不能注入标签", () => {
    const attack = '<img src=x onerror="window.__approvalInjected=1">';
    const html = render(approval({
      action: "exec",
      rule_id: "c4.exec",
      command: {
        argv: ["/bin/echo", attack],
        cwd: `/tmp/${attack}`,
        backend: `local-${attack}`,
      },
    }));
    expect(html).not.toContain(attack);
    expect(html).not.toContain("<img");
    expect(html).toContain("&lt;img");
  });

  it("旧 exec 审批或畸形详情明确告警，不伪造 argv/cwd/后端", () => {
    for (const command of [
      null,
      { argv: [], cwd: "", backend: "" },
      { argv: ["/bin/echo"], cwd: "", backend: "local" },
    ]) {
      const html = render(approval({ action: "exec", rule_id: "c4.exec", command }));
      expect(html).toContain("缺少完整命令详情");
      expect(html).toContain("请拒绝并让 Agent 重新发起");
      expect(html).not.toContain("/bin/echo");
    }
  });
});
