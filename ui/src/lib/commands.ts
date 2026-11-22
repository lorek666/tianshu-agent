/** 控制命令在送达 task.revise 之前解析，不把命令文本写进任务目标。 */
export type ComposerCommand =
  | { kind: "message"; text: string }
  | { kind: "plan"; text: string }
  | { kind: "compact" }
  | { kind: "execute" }
  | { kind: "skills" }
  | { kind: "skill"; skill: string; text: string }
  | { kind: "error"; text: string };

export function parseComposerCommand(value: string): ComposerCommand {
  const text = value.trim();
  if (text === "/") return { kind: "skills" };
  const match = /^\/(\S+)(?:\s+([\s\S]*))?$/.exec(text);
  if (match === null) return { kind: "message", text };
  const command = match[1]!;
  const argument = (match[2] ?? "").trim();
  if (command === "skills") {
    return argument === ""
      ? { kind: "skills" }
      : { kind: "error", text: "/skills 不接受正文，请选择技能后发送。" };
  }
  if (command === "skill") {
    if (argument === "") return { kind: "skills" };
    const selected = /^(\S+)(?:\s+([\s\S]*))?$/.exec(argument)!;
    return { kind: "skill", skill: selected[1]!, text: (selected[2] ?? "").trim() };
  }
  if (command === "plan") return { kind: "plan", text: argument };
  if (command === "compact" || command === "execute") {
    return argument === ""
      ? { kind: command }
      : { kind: "error", text: `/${command} 不接受正文，请单独发送该命令。` };
  }
  if (command.includes("/")) return { kind: "message", text };
  // 名称由后端技能目录验证；失败绝不退化成普通任务目标。
  return { kind: "skill", skill: command!, text: argument };
}
