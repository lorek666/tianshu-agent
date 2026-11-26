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

  it("控制命令不接受附带正文，技能命令不会被当成普通目标", () => {
    for (const command of ["compact", "execute"]) {
      expect(parseComposerCommand(`/${command}`)).toEqual({ kind: command });
      expect(parseComposerCommand(`/${command} 额外参数`).kind).toBe("error");
    }
    expect(parseComposerCommand("/tmp/documents 请总结").kind).toBe("message");
    expect(parseComposerCommand("/unknown")).toEqual({
      kind: "skill", skill: "unknown", text: "",
    });
  });

  it("支持打开技能菜单、显式调用和快捷调用，内置命令优先", () => {
    for (const value of ["/", "/skills", "/skill"]) {
      expect(parseComposerCommand(value)).toEqual({ kind: "skills" });
    }
    expect(parseComposerCommand("/skill review@0123456789ab 检查改动\n不要修改")).toEqual({
      kind: "skill", skill: "review@0123456789ab", text: "检查改动\n不要修改",
    });
    expect(parseComposerCommand("/review 检查改动")).toEqual({
      kind: "skill", skill: "review", text: "检查改动",
    });
    expect(parseComposerCommand("/plan 先分析")).toEqual({ kind: "plan", text: "先分析" });
  });
});
