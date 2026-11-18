import { useState } from "react";
import type {
  ApprovalView,
  DaemonView,
  RunView,
  TaskView,
  WorkspaceMode,
} from "../api/types";
import type { StreamStatus } from "../api/stream";
import { formatRelative } from "../lib/format";
import {
  isOpenApproval,
  runStateLabel,
  runStateTone,
  taskGoalSummary,
} from "../lib/humanize";
import { streamStatusText } from "./AppHeader";
import {
  IconBrain,
  IconFolder,
  IconMark,
  IconMoon,
  IconPlus,
  IconSettings,
  IconSidebar,
  IconSun,
} from "./Icons";

export interface ProjectWorkspace {
  root_path: string;
  mode: WorkspaceMode;
  name: string;
}

export interface SidebarProps {
  tasks: TaskView[];
  runs: RunView[];
  approvals: ApprovalView[];
  projects: ProjectWorkspace[];
  selectedTaskId: string | null;
  composingNewTask: boolean;
  activeProjectPath: string | null;
  collapsed: boolean;
  effectiveTheme: "light" | "dark";
  daemon: DaemonView;
  streamStatus: StreamStatus;
  streamDetail: string | null;
  connectionLost: boolean;
  demo: boolean;
  now: number;
  onToggleCollapse: () => void;
  onNewFreeTask: () => void;
  onSelectProject: (project: ProjectWorkspace) => void;
  onAddProject: (rootPath: string) => void;
  onSelectTask: (taskId: string) => void;
  onOpenMemory: () => void;
  onToggleTheme: () => void;
  onOpenSettings: () => void;
}

export function basenameOfPath(rawPath: string): string {
  const trimmed = rawPath.replace(/[\\/]+$/, "");
  if (trimmed === "") return rawPath;
  const parts = trimmed.split(/[\\/]/).filter(Boolean);
  return parts[parts.length - 1] ?? rawPath;
}

export function Sidebar({
  tasks,
  runs,
  approvals,
  projects,
  selectedTaskId,
  composingNewTask,
  activeProjectPath,
  collapsed,
  effectiveTheme,
  daemon,
  streamStatus,
  streamDetail,
  connectionLost,
  demo,
  now,
  onToggleCollapse,
  onNewFreeTask,
  onSelectProject,
  onAddProject,
  onSelectTask,
  onOpenMemory,
  onToggleTheme,
  onOpenSettings,
}: SidebarProps) {
  const [addingProject, setAddingProject] = useState(false);
  const [newProjectPath, setNewProjectPath] = useState("");

  const isHealthy =
    !connectionLost &&
    (streamStatus === "live" ||
      streamStatus === "connecting" ||
      streamStatus === "reconnecting" ||
      streamStatus === "polling");

  const daemonTooltip = [
    daemon.version !== null ? `守护进程 v${daemon.version}` : "本地守护进程",
    connectionLost ? "连接中断" : streamStatusText(streamStatus, streamDetail),
    demo ? "演示模式" : null,
  ]
    .filter(Boolean)
    .join(" · ");

  const submitNewProject = () => {
    const trimmed = newProjectPath.trim();
    if (trimmed === "") {
      setAddingProject(false);
      return;
    }
    onAddProject(trimmed);
    setNewProjectPath("");
    setAddingProject(false);
  };

  return (
    <aside className={`ts-sidebar${collapsed ? " is-collapsed" : ""}`} aria-label="主导航侧栏">
      <div className="brand-bar">
        <button
          type="button"
          className="brand-id"
          onClick={onNewFreeTask}
          title={daemonTooltip}
        >
          <span className="brand-logo">
            <IconMark size={16} />
          </span>
          <span className="brand-title-group hide-when-collapsed">
            <span className="brand-title-text">天枢</span>
            <span
              className={`brand-health-dot ${isHealthy ? "is-ok" : "is-down"}`}
              aria-label={isHealthy ? "守护进程已连接" : "守护进程连接异常"}
            />
          </span>
        </button>

        <button
          type="button"
          className="icon-btn sidebar-collapse-btn"
          onClick={onToggleCollapse}
          aria-label={collapsed ? "展开侧栏" : "收起侧栏"}
          title={collapsed ? "展开侧栏" : "收起侧栏"}
        >
          <IconSidebar size={16} />
        </button>
      </div>

      {/* 自由新建任务（非项目：默认不带工作区，可自选或留空；按钮纯文本保持「新建任务」） */}
      <button
        type="button"
        className={`new-chat-btn${composingNewTask && activeProjectPath === null ? " is-active" : ""}`}
        data-testid="new-free-task-btn"
        onClick={onNewFreeTask}
        title="新建任务（非项目自由对话，工作区可自选或留空）"
      >
        <IconPlus size={15} />
        <span className="hide-when-collapsed">新建任务</span>
      </button>

      {/* 项目工作区（点击进入新建任务时自动填入该项目路径） */}
      <div className="nav-section-label hide-when-collapsed">
        <span>项目工作区</span>
        <button
          type="button"
          className="section-mini-btn"
          aria-label="添加项目工作区"
          title="添加常用项目目录"
          onClick={() => setAddingProject((previous) => !previous)}
        >
          <IconPlus size={12} />
        </button>
      </div>

      {addingProject && !collapsed ? (
        <form
          className="sidebar-add-project hide-when-collapsed"
          onSubmit={(event) => {
            event.preventDefault();
            submitNewProject();
          }}
        >
          <input
            className="ts-input sidebar-project-input mono"
            placeholder="输入项目绝对路径…"
            value={newProjectPath}
            autoFocus
            onChange={(event) => setNewProjectPath(event.target.value)}
          />
          <button type="submit" className="chip-btn is-active">
            添加
          </button>
        </form>
      ) : null}

      <div className="project-pill-list hide-when-collapsed">
        {projects.length === 0 ? (
          <div className="sidebar-empty-hint">
            绑定过目录的任务会自动归入项目工作区
          </div>
        ) : (
          projects.map((project) => {
            const isActive =
              selectedTaskId === null && activeProjectPath === project.root_path;
            return (
              <button
                key={project.root_path}
                type="button"
                className={`project-row${isActive ? " is-active" : ""}`}
                data-project-path={project.root_path}
                onClick={() => onSelectProject(project)}
                title={`在「${project.name}」项目下新建任务（自动填入 ${project.root_path}）`}
              >
                <span className="project-row-main">
                  <IconFolder size={14} />
                  <span className="project-row-name">{project.name}</span>
                </span>
                <span className="project-row-plus" aria-hidden="true">
                  +
                </span>
              </button>
            );
          })
        )}
      </div>

      <div className="nav-section-label hide-when-collapsed">
        <span>最近会话</span>
        {tasks.length > 0 ? <span className="nav-count num">{tasks.length}</span> : null}
      </div>

      <div className="task-nav-list" role="list" aria-label="最近会话">
        {tasks.length === 0 ? (
          <div className="sidebar-empty-hint hide-when-collapsed">
            暂无历史会话，在右侧直接发起即可
          </div>
        ) : (
          tasks.map((task) => {
            const pendingCount = approvals.filter(
              (approval) =>
                approval.task_id === task.task_id && isOpenApproval(approval.state),
            ).length;
            const activeRun = task.active_run_id
              ? runs.find((run) => run.run_id === task.active_run_id) ?? null
              : null;
            const updatedAt = task.updated_at ?? task.created_at;
            const firstWs = task.workspaces[0];
            const wsShort =
              firstWs !== undefined ? basenameOfPath(firstWs.root_path) : "无工作区";
            const isSelected = !composingNewTask && selectedTaskId === task.task_id;
            const dotTone =
              pendingCount > 0 ? "approval" : runStateTone(activeRun?.state ?? task.status);

            return (
              <button
                key={task.task_id}
                type="button"
                role="listitem"
                className={`task-nav-item${isSelected ? " is-active" : ""}`}
                onClick={() => onSelectTask(task.task_id)}
                aria-current={isSelected ? "true" : undefined}
                title={task.goal}
              >
                <span className={`status-dot tone-${dotTone}`} aria-hidden="true" />
                <span className="task-nav-body hide-when-collapsed">
                  <span className="task-nav-title">{taskGoalSummary(task, 54)}</span>
                  <span className="task-nav-meta">
                    <span className="task-nav-sub">
                      {wsShort} · {formatRelative(updatedAt, now)}
                    </span>
                    {pendingCount > 0 ? (
                      <span className="task-approval-hint">{pendingCount} 待批准</span>
                    ) : activeRun !== null ? (
                      <span className="task-run-hint">{runStateLabel(activeRun.state)}</span>
                    ) : null}
                  </span>
                </span>
              </button>
            );
          })
        )}
      </div>

      <div className="sidebar-dock">
        <button
          type="button"
          className="dock-btn"
          onClick={onOpenMemory}
          title="查看与管理记忆库"
        >
          <span className="dock-btn-main">
            <IconBrain size={16} />
            <span className="hide-when-collapsed">记忆库</span>
          </span>
        </button>

        <button
          type="button"
          className="dock-btn"
          onClick={onToggleTheme}
          title="切换浅色 / 深色外观"
        >
          <span className="dock-btn-main">
            {effectiveTheme === "light" ? <IconMoon size={16} /> : <IconSun size={16} />}
            <span className="hide-when-collapsed">
              {effectiveTheme === "light" ? "深色模式" : "浅色模式"}
            </span>
          </span>
        </button>

        <button
          type="button"
          className="dock-btn"
          data-testid="open-settings-btn"
          onClick={onOpenSettings}
          title="打开系统设置"
        >
          <span className="dock-btn-main">
            <IconSettings size={16} />
            <span className="hide-when-collapsed">设置</span>
          </span>
        </button>
      </div>
    </aside>
  );
}
