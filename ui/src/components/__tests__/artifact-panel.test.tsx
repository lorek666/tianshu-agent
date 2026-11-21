// @vitest-environment jsdom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";
import type { HostClient } from "../../api/host";
import { ArtifactPanel } from "../ArtifactPanel";

it("previews the original text by Run and Receipt, and clears it when Run changes", async () => {
  const artifact = {
    artifact_id: "rcpt_abcd1234abcd",
    path: "report.md",
    bytes: 25,
    sha256: "a".repeat(64),
    tool_id: "create_report",
  };
  const rpc = vi.fn(async (method: string, params?: Record<string, unknown>) => {
    if (method === "artifact.list") {
      return { artifacts: params?.run_id === "run_1" ? [artifact] : [] };
    }
    if (method === "artifact.preview") {
      return { artifact: { ...artifact, text: "<script>bad()</script>", truncated: true } };
    }
    throw new Error(`Unexpected method ${method}`);
  });
  const host = {
    mode: "host",
    rpc: rpc as HostClient["rpc"],
    getState: vi.fn(),
    openEvents: vi.fn(),
  } satisfies HostClient;
  const container = document.createElement("div");
  const root = createRoot(container);
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
  try {
    await act(async () => root.render(<ArtifactPanel host={host} runId="run_1" revision={1} />));
    await act(async () => container.querySelector("button")?.click());
    expect(rpc).toHaveBeenCalledWith("artifact.list", { run_id: "run_1" });
    const file = Array.from(container.querySelectorAll("button")).find(
      (button) => button.textContent?.includes("report.md"),
    );
    expect(file).toBeDefined();
    await act(async () => file?.click());
    expect(rpc).toHaveBeenCalledWith("artifact.preview", {
      run_id: "run_1", artifact_id: artifact.artifact_id,
    });
    expect(container.querySelector("pre")?.textContent).toBe("<script>bad()</script>");
    expect(container.querySelector("script")).toBeNull();
    expect(container.textContent).toContain("预览已截断");

    await act(async () => root.render(<ArtifactPanel host={host} runId="run_2" revision={2} />));
    expect(container.querySelector("pre")).toBeNull();
    expect(container.textContent).not.toContain("report.md");
  } finally {
    await act(async () => root.unmount());
  }
});
