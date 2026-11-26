import { cloneElement, useEffect, useId, useRef, useState } from "react";
import type { AriaAttributes, KeyboardEvent, ReactElement } from "react";
import type { SkillCatalog, SkillsApi, SkillView } from "../api/skills";

interface Props {
  value: string;
  onChange: (value: string) => void;
  workspaceRoots: string[];
  api: SkillsApi | null;
  children: ReactElement<AriaAttributes & { role?: string }>;
}

function queryFor(value: string): string | null {
  if (value === "/skill" || value === "/skills") return "";
  if (value.startsWith("/skill ")) {
    const rest = value.slice(7);
    return /\s/.test(rest) ? null : rest;
  }
  return /^\/[^\s]*$/.test(value) ? value.slice(1) : null;
}

const BUILTINS = [
  { name: "goal", description: "建立目标与验收条件，每轮手动推进" },
  { name: "plan", description: "先生成计划，确认后再执行" },
  { name: "compact", description: "压缩当前任务的上下文" },
  { name: "execute", description: "确认执行已有计划" },
];

/** 两个输入框共用；捕获菜单按键后再决定是否交给原输入框发送逻辑。 */
export function SkillCommandMenu({ value, onChange, workspaceRoots, api, children }: Props) {
  const menuId = useId();
  const menuRef = useRef<HTMLDivElement>(null);
  const [dismissed, setDismissed] = useState<string | null>(null);
  const [index, setIndex] = useState(0);
  const [result, setResult] = useState<{
    key: string; api: SkillsApi; catalog: SkillCatalog | null; error: string | null;
  } | null>(null);
  const rootsKey = JSON.stringify(workspaceRoots);
  const query = queryFor(value);
  const open = query !== null && dismissed !== value;
  const current = result?.key === rootsKey && result.api === api ? result : null;
  const catalog = current?.catalog ?? null;
  const loading = open && api !== null && current === null;

  useEffect(() => {
    setIndex(0);
    setDismissed(null);
  }, [value, rootsKey]);

  useEffect(() => {
    if (!open || api === null) return;
    let cancelled = false;
    setResult(null);
    void api.list(JSON.parse(rootsKey) as string[]).then(
      (next) => {
        if (!cancelled) setResult({ key: rootsKey, api, catalog: next, error: null });
      },
      (error: unknown) => {
        if (!cancelled) setResult({
          key: rootsKey, api, catalog: null,
          error: error instanceof Error ? error.message : "无法读取技能列表",
        });
      },
    );
    return () => { cancelled = true; };
  }, [open, api, rootsKey]);

  const filter = (query ?? "").toLocaleLowerCase();
  const builtins = BUILTINS.filter((entry) => `${entry.name} ${entry.description}`.includes(filter));
  const skills = (catalog?.skills ?? []).filter(
    (entry) => `${entry.name} ${entry.description}`.toLocaleLowerCase().includes(filter),
  );
  const options: { key: string; value: string; name: string; description: string; skill?: SkillView }[] = [
    ...builtins.map((entry) => ({
      key: entry.name, value: `/${entry.name} `, ...entry,
    })),
    ...skills.map((entry) => ({
      key: entry.id, value: `/skill ${entry.id} `, name: entry.name,
      description: entry.description, skill: entry,
    })),
  ];
  const activeIndex = Math.min(index, Math.max(0, options.length - 1));

  useEffect(() => {
    menuRef.current?.querySelector<HTMLElement>("[aria-selected='true']")
      ?.scrollIntoView?.({ block: "nearest" });
  }, [activeIndex, open]);

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (!open || event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return;
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      setDismissed(value);
    } else if (options.length > 0 && !event.shiftKey
        && ["ArrowDown", "ArrowUp", "Enter", "Tab"].includes(event.key)) {
      event.preventDefault();
      event.stopPropagation();
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        setIndex((activeIndex + (event.key === "ArrowDown" ? 1 : -1) + options.length)
          % options.length);
      } else {
        onChange(options[activeIndex]!.value);
      }
    }
  };

  return <div className="skill-composer" onKeyDownCapture={onKeyDown}
    onBlurCapture={(event) => {
      if (!event.currentTarget.contains(event.relatedTarget)) setDismissed(value);
    }}>
    {cloneElement(children, {
      role: "combobox",
      "aria-autocomplete": "list",
      "aria-expanded": open,
      "aria-controls": open ? menuId : undefined,
      "aria-activedescendant": open && options.length > 0
        ? `${menuId}-${activeIndex}` : undefined,
    })}
    {open ? <div className="skill-command-menu" ref={menuRef}>
      <div className="skill-command-heading">命令与本地技能</div>
      {loading ? <p role="status">正在读取技能…</p> : null}
      {current?.error ? <p role="status">{current.error}；可关闭后重新输入 / 重试。</p> : null}
      <div id={menuId} role="listbox" aria-label="命令与技能">
        {options.map((option, position) => <button
          key={option.key} id={`${menuId}-${position}`} type="button" role="option"
          aria-selected={position === activeIndex} tabIndex={-1}
          data-skill-id={option.skill?.id}
          className="skill-command-option"
          onMouseDown={(event) => event.preventDefault()}
          onMouseEnter={() => setIndex(position)}
          onClick={() => onChange(option.value)}>
          <strong>/{option.name}</strong>
          <span>{option.description}</span>
          {option.skill ? <small title={option.skill.path}>{option.skill.path}</small> : null}
        </button>)}
      </div>
      {!loading && skills.length === 0 ? <p>未发现匹配技能。请将 SKILL.md 放入
        ~/.agents/skills/名称/ 或工作区的 .agents/skills/名称/。</p> : null}
      {catalog && catalog.issues.length > 0 ? <details>
        <summary>有 {catalog.issues.length} 项技能读取问题</summary>
        {catalog.issues.map((issue, position) => <p key={position}>
          {issue.path}：{issue.message}
        </p>)}
      </details> : null}
      <p className="skill-command-hint">选择后发送，技能全文将作为任务输入发送给当前模型。</p>
    </div> : null}
  </div>;
}
