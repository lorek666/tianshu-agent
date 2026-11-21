/** 控制命令在送达 task.revise 之前解析，不把命令文本写进任务目标。 */
export type ComposerCommand =
  | { kind: "message"; text: string }
  | { kind: "plan"; text: string }
  | { kind: "compact" }
  | { kind: "execute" }
  | { kind: "error"; text: string };

export function parseComposerCommand(value: string): ComposerCommand {
  const text = value.trim();
  const match = /^\/(\S+)(?:\s+([\s\S]*))?$/.exec(text);
  if (match === null) return { kind: "message", text };
  const command = match[1];
  const argument = (match[2] ?? "").trim();
  if (command === "plan") return { kind: "plan", text: argument };
  if (command === "compact" || command === "execute") {
    return argument === ""
      ? { kind: command }
      : { kind: "error", text: `/${command} 不接受正文，请单独发送该命令。` };
  }
  return { kind: "error", text: "未知命令。可使用 /plan、/compact 或 /execute。" };
}
