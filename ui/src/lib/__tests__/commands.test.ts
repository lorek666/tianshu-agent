import { describe, expect, it } from "vitest";
import { parseComposerCommand } from "../commands";

describe("输入区命令", () => {
  it("区分普通正文、切换计划与带正文的计划请求", () => {
    expect(parseComposerCommand("普通要求")).toEqual({ kind: "message", text: "普通要求" });
    expect(parseComposerCommand(" /plan ")).toEqual({ kind: "plan", text: "" });
    expect(parseComposerCommand("/plan 先读代码\n再出方案")).toEqual({
      kind: "plan", text: "先读代码\n再出方案",
    });
  });

  it("控制命令不接受附带正文，绝对路径仍是普通正文", () => {
    for (const command of ["compact", "execute"]) {
      expect(parseComposerCommand(`/${command}`)).toEqual({ kind: command });
      expect(parseComposerCommand(`/${command} 额外参数`).kind).toBe("error");
    }
    expect(parseComposerCommand("/tmp/documents 请总结").kind).toBe("message");
  });
});
