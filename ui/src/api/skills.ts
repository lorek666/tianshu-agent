import type { HostClient } from "./host";
import type { ComposerCommand } from "../lib/commands";
import { asRecord } from "../lib/values";
import { RPC } from "./types";

export interface SkillView {
  id: string;
  name: string;
  description: string;
  path: string;
  sha256: string;
}

export interface SkillCatalog {
  format_version: 1;
  skills: SkillView[];
  issues: { path: string; message: string }[];
}

export interface PreparedSkill {
  format_version: 1;
  skill: SkillView;
  goal: string;
}

export interface SkillsApi {
  list(workspaceRoots: string[]): Promise<SkillCatalog>;
  prepare(skill: string, input: string, workspaceRoots: string[]): Promise<PreparedSkill>;
}

function skillView(raw: unknown): SkillView {
  const value = asRecord(raw);
  if (["id", "name", "description", "path", "sha256"].some(
    (key) => typeof value[key] !== "string" || value[key] === "",
  )) throw new Error("技能描述格式无效，请更新天枢后重试");
  return {
    id: value.id as string, name: value.name as string,
    description: value.description as string, path: value.path as string,
    sha256: value.sha256 as string,
  };
}

function versioned(raw: unknown): Record<string, unknown> {
  const value = asRecord(raw);
  if (value.format_version !== 1) throw new Error("当前连接不支持技能 V1，请更新天枢后重试");
  return value;
}

export function createSkillsApi(host: HostClient): SkillsApi {
  return {
    async list(workspaceRoots) {
      const raw = versioned(await host.rpc(RPC.skillList, { workspace_roots: workspaceRoots }));
      if (!Array.isArray(raw.skills) || !Array.isArray(raw.issues)) {
        throw new Error("技能列表格式无效");
      }
      return {
        format_version: 1,
        skills: raw.skills.map(skillView),
        issues: raw.issues.map((item: unknown) => {
          const issue = asRecord(item);
          if (typeof issue.path !== "string" || typeof issue.message !== "string") {
            throw new Error("技能读取问题格式无效");
          }
          return { path: issue.path, message: issue.message };
        }),
      };
    },
    async prepare(skill, input, workspaceRoots) {
      const raw = versioned(await host.rpc(RPC.skillPrepare, {
        workspace_roots: workspaceRoots, skill, input,
      }));
      if (typeof raw.goal !== "string" || raw.goal.trim() === "") {
        throw new Error("技能准备结果缺少目标正文");
      }
      return { format_version: 1, skill: skillView(raw.skill), goal: raw.goal };
    },
  };
}

export async function prepareComposerText(
  command: ComposerCommand, api: SkillsApi | null, workspaceRoots: string[],
): Promise<string> {
  if (command.kind === "message" || command.kind === "plan" || command.kind === "goal") {
    return command.text;
  }
  if (command.kind !== "skill") throw new Error("请先选择命令或技能");
  if (api === null) throw new Error("当前连接不支持技能，请更新天枢后重试");
  return (await api.prepare(command.skill, command.text, workspaceRoots)).goal;
}
