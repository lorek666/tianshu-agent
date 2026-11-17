import { useEffect, useRef, useState } from "react";
import type { KeyboardEvent } from "react";
import {
  EFFECTS_RUN_POLICY,
  PURE_ONLY_RUN_POLICY,
} from "../api/types";
import type {
  CreateTaskInput,
  RunExecutionPolicyV1,
  WorkspaceMode,
  WorkspaceView,
} from "../api/types";
import type { StreamStatus } from "../api/stream";
import { workspaceModeLabel } from "../lib/humanize";
import { streamStatusText } from "./AppHeader";
import {
  IconCheck,
  IconChevronDown,
  IconFolder,
  IconPlus,
  IconRefresh,
  IconSend,
  IconShield,
  IconX,
} from "./Icons";
import { ModelStatusControl } from "./ModelStatusControl";
import type { ProjectWorkspace } from "./Sidebar";
import { basenameOfPath } from "./Sidebar";
import { WelcomePet } from "./WelcomePet";
import type { WelcomePetStatus } from "./WelcomePet";

export interface WelcomeComposerProps {
  activeProject: ProjectWorkspace | null;
  projects: ProjectWorkspace[];
  defaultPolicy: RunExecutionPolicyV1;
  enablePet: boolean;
  enableParallax: boolean;
  reduceMotion: boolean;
  previewPose: WelcomePetStatus;
  connectionLost: boolean;
  streamStatus: StreamStatus;
  streamDetail: string | null;
  openApprovalsCount: number;
  busy: boolean;
  onJumpToPendingApproval: () => void;
  onRefresh: () => void;
  onOpenModelSettings: () => void;
  onSubmit: (input: CreateTaskInput) => Promise<void>;
}

interface WorkspaceDraftRow {
  root_path: string;
  mode: WorkspaceMode;
}

type OpenMenu = null | "workspace" | "perm";

export function WelcomeComposer({
  activeProject,
  projects,
  defaultPolicy,
  enablePet,
  enableParallax,
  reduceMotion,
  previewPose,
  connectionLost,
  streamStatus,
  streamDetail,
  openApprovalsCount,
  busy,
  onJumpToPendingApproval,
  onRefresh,
  onOpenModelSettings,
  onSubmit,
}: WelcomeComposerProps) {
  const [goal, setGoal] = useState("");
  const [rows, setRows] = useState<WorkspaceDraftRow[]>(() =>
    activeProject !== null
      ? [{ root_path: activeProject.root_path, mode: activeProject.mode }]
      : [{ root_path: "", mode: "writable" }],
  );
  const [executionPolicy, setExecutionPolicy] =
    useState<RunExecutionPolicyV1>(defaultPolicy);
  const [openMenu, setOpenMenu] = useState<OpenMenu>(null);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const toolbarRef = useRef<HTMLDivElement | null>(null);

  // 切换「新建非项目任务」与「项目内新建任务」时同步默认工作区：
  // 项目上下文自动带入项目工作区；非项目默认留空。
  useEffect(() => {
    if (activeProject !== null) {
      setRows([{ root_path: activeProject.root_path, mode: activeProject.mode }]);
    } else {
      setRows([{ root_path: "", mode: "writable" }]);
    }
    setOpenMenu(null);
    setError(null);
  }, [activeProject]);

  useEffect(() => {
    setExecutionPolicy(defaultPolicy);
  }, [defaultPolicy]);

  useEffect(() => {
    if (openMenu === null) return;
    const handlePointerDown = (event: MouseEvent) => {
      if (
        toolbarRef.current !== null &&
        event.target instanceof Node &&
        !toolbarRef.current.contains(event.target)
      ) {
        setOpenMenu(null);
      }
    };
    const handleEsc = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape") setOpenMenu(null);
    };
    window.addEventListener("mousedown", handlePointerDown);
    window.addEventListener("keydown", handleEsc);
    return () => {
      window.removeEventListener("mousedown", handlePointerDown);
      window.removeEventListener("keydown", handleEsc);
    };
  }, [openMenu]);

  const pending = busy || submitting;
  const connectionAlert = connectionLost || streamStatus === "disconnected";
  const isLive =
    !connectionLost &&
    (streamStatus === "live" ||
      streamStatus === "connecting" ||
      streamStatus === "reconnecting" ||
      streamStatus === "polling");

  const petStatus: WelcomePetStatus = connectionAlert
    ? "error"
    : pending
      ? "thinking"
      : previewPose;

  const committedWorkspaces: WorkspaceView[] = rows
    .map((row) => ({ root_path: row.root_path.trim(), mode: row.mode }))
    .filter((row) => row.root_path !== "");

  const updateRow = (index: number, patch: Partial<WorkspaceDraftRow>) => {
    setRows((previous) =>
      previous.map((item, idx) => (idx === index ? { ...item, ...patch } : item)),
    );
  };

  const removeRow = (index: number) => {
    setRows((previous) => {
      if (previous.length <= 1) return [{ root_path: "", mode: "writable" }];
      return previous.filter((_, idx) => idx !== index);
    });
  };

  const clearWorkspaces = () => {
    setRows([{ root_path: "", mode: "writable" }]);
    setOpenMenu(null);
  };

  const selectProjectWorkspace = (project: ProjectWorkspace) => {
    setRows([{ root_path: project.root_path, mode: project.mode }]);
    setOpenMenu(null);
  };

  const handleSubmit = async (): Promise<void> => {
    if (pending) return;
    const trimmedGoal = goal.trim();
    if (trimmedGoal === "") {
      setError("请先描述你想让天枢完成的任务或对话内容。");
      return;
    }
    setError(null);
    setOpenMenu(null);
    setSubmitting(true);
    try {
      await onSubmit({
        goal: trimmedGoal,
        workspaces: committedWorkspaces,
        execution_policy: executionPolicy,
      });
      setGoal("");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "创建任务失败，请重试。");
    } finally {
      setSubmitting(false);
    }
  };

  const handleTextareaKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      void handleSubmit();
    }
  };

  const wsLabel =
    committedWorkspaces.length === 0
      ? "选择工作区（可留空）"
      : committedWorkspaces.length === 1
        ? `${committedWorkspaces[0]!.root_path}${
            committedWorkspaces[0]!.mode === "readonly" ? " · 只读" : ""
          }`
        : `${basenameOfPath(committedWorkspaces[0]!.root_path)} 等 ${committedWorkspaces.length} 个目录`;

  const permLabel =
    executionPolicy.mode === "pure_only" ? "仅读取与对话" : "每次询问";

  return (
    <div className="canvas-main">
      <header className="canvas-header">
        <div className="canvas-breadcrumb">
          {activeProject !== null ? (
            <>
              <span className="breadcrumb-label">项目：{activeProject.name}</span>
              <span className="breadcrumb-sep">·</span>
              <span className="breadcrumb-path mono" title={activeProject.root_path}>
                {activeProject.root_path}
              </span>
            </>
          ) : (
            <span className="breadcrumb-label">新建对话</span>
          )}
        </div>

        <div className="canvas-header-actions">
          {openApprovalsCount > 0 ? (
            <button
              type="button"
              className="chip-btn is-warn"
              onClick={onJumpToPendingApproval}
            >
              <span>{openApprovalsCount} 项操作待批准 · 前往处理</span>
            </button>
          ) : null}

          <span
            className={`conn-chip${isLive ? " is-live" : " is-down"}`}
            title={streamStatusText(streamStatus, streamDetail)}
          >
            <span className="conn-dot" aria-hidden="true" />
            {connectionLost
              ? "连接中断"
              : streamStatus === "live"
                ? "守护进程在线"
                : streamStatusText(streamStatus, streamDetail)}
          </span>

          <button
            type="button"
            className="icon-btn"
            onClick={onRefresh}
            title="刷新任务与运行状态"
            aria-label="刷新"
          >
            <IconRefresh size={15} />
          </button>
        </div>
      </header>

      <section className="welcome-view center-empty">
        {enablePet ? (
          <WelcomePet
            status={petStatus}
            interactive
            parallaxEnabled={enableParallax}
            reducedMotion={reduceMotion}
            size={224}
          />
        ) : null}

        <h1 className="welcome-title">欢迎使用天枢</h1>

        <form
          className="composer-shell"
          onSubmit={(event) => {
            event.preventDefault();
            void handleSubmit();
          }}
        >
          <textarea
            className="composer-textarea"
            data-testid="composer-textarea"
            aria-label="任务目标"
            rows={2}
            value={goal}
            placeholder="描述你想让天枢完成的任务…"
            onChange={(event) => setGoal(event.target.value)}
            onKeyDown={handleTextareaKeyDown}
            disabled={pending}
          />

          <div className="composer-toolbar" ref={toolbarRef}>
            <div className="composer-left-controls">
              {/* 1. 工作区选择与留空控件 */}
              <div className="dropdown-anchor">
                <span className="ws-chip-group">
                  <button
                    type="button"
                    className={`chip-btn${committedWorkspaces.length > 0 ? " is-active" : ""}`}
                    data-testid="composer-ws-btn"
                    disabled={pending}
                    aria-expanded={openMenu === "workspace"}
                    onClick={() =>
                      setOpenMenu((prev) => (prev === "workspace" ? null : "workspace"))
                    }
                  >
                    <IconFolder size={13} />
                    <span className="chip-truncate" title={wsLabel}>
                      {wsLabel}
                    </span>
                    {committedWorkspaces.length === 0 ? <IconChevronDown size={11} /> : null}
                  </button>
                  {committedWorkspaces.length > 0 ? (
                    <button
                      type="button"
                      className="chip-clear-btn"
                      aria-label="清空工作区"
                      title="清空工作区（保持留空）"
                      disabled={pending}
                      onClick={clearWorkspaces}
                    >
                      <IconX size={11} />
                    </button>
                  ) : null}
                </span>

                {openMenu === "workspace" ? (
                  <div
                    className="popover-menu ws-popover"
                    data-testid="ws-popover"
                    role="dialog"
                    aria-label="选择工作区目录"
                  >
                    <div className="popover-header">绑定工作区目录（可留空）</div>

                    <button
                      type="button"
                      className={`popover-item${committedWorkspaces.length === 0 ? " is-selected" : ""}`}
                      onClick={clearWorkspaces}
                    >
                      <div className="popover-item-main">
                        <div className="popover-item-title">不绑定工作区（留空）</div>
                        <div className="popover-item-desc">
                          适合自由问答与分析；不授予任何本地目录读写权限
                        </div>
                      </div>
                      {committedWorkspaces.length === 0 ? (
                        <span className="popover-check">
                          <IconCheck size={14} />
                        </span>
                      ) : null}
                    </button>

                    {projects.map((project) => {
                      const selected =
                        committedWorkspaces.length === 1 &&
                        committedWorkspaces[0]?.root_path === project.root_path;
                      return (
                        <button
                          key={project.root_path}
                          type="button"
                          className={`popover-item${selected ? " is-selected" : ""}`}
                          onClick={() => selectProjectWorkspace(project)}
                        >
                          <div className="popover-item-main">
                            <div className="popover-item-title">
                              {project.name}（{workspaceModeLabel(project.mode)}）
                            </div>
                            <div className="popover-item-desc mono">{project.root_path}</div>
                          </div>
                          {selected ? (
                            <span className="popover-check">
                              <IconCheck size={14} />
                            </span>
                          ) : null}
                        </button>
                      );
                    })}

                    <div className="popover-divider" />

                    <div className="ws-custom-editor">
                      {rows.map((row, index) => (
                        <div className="workspace-row" key={index}>
                          <input
                            className="ts-input mono ws-row-input"
                            value={row.root_path}
                            placeholder="输入绝对路径，或留空"
                            aria-label={`第 ${index + 1} 个目录路径`}
                            onChange={(event) =>
                              updateRow(index, { root_path: event.target.value })
                            }
                            disabled={pending}
                          />
                          <div
                            className="segmented"
                            role="group"
                            aria-label={`第 ${index + 1} 个目录权限`}
                          >
                            <button
                              type="button"
                              className={`segmented-item${row.mode === "readonly" ? " is-active" : ""}`}
                              onClick={() => updateRow(index, { mode: "readonly" })}
                              disabled={pending}
                            >
                              只读
                            </button>
                            <button
                              type="button"
                              className={`segmented-item${row.mode === "writable" ? " is-active" : ""}`}
                              onClick={() => updateRow(index, { mode: "writable" })}
                              disabled={pending}
                            >
                              可写
                            </button>
                          </div>
                          {rows.length > 1 ? (
                            <button
                              type="button"
                              className="icon-btn"
                              aria-label="移除这个目录"
                              onClick={() => removeRow(index)}
                              disabled={pending}
                            >
                              <IconX size={13} />
                            </button>
                          ) : null}
                        </div>
                      ))}

                      <div className="ws-editor-foot">
                        <button
                          type="button"
                          className="link-btn"
                          onClick={() =>
                            setRows((prev) => [...prev, { root_path: "", mode: "writable" }])
                          }
                          disabled={pending}
                        >
                          <IconPlus size={12} />
                          再加一个目录
                        </button>
                        <button
                          type="button"
                          className="btn btn-primary btn-sm"
                          data-testid="apply-ws-btn"
                          onClick={() => setOpenMenu(null)}
                        >
                          确定
                        </button>
                      </div>
                    </div>
                  </div>
                ) : null}
              </div>

              {/* 2. 权限与执行模式下拉菜单 */}
              <div className="dropdown-anchor">
                <button
                  type="button"
                  className={`chip-btn${executionPolicy.mode === "effects" ? " is-active" : ""}`}
                  data-testid="composer-perm-btn"
                  disabled={pending}
                  aria-expanded={openMenu === "perm"}
                  onClick={() => setOpenMenu((prev) => (prev === "perm" ? null : "perm"))}
                >
                  <IconShield size={13} />
                  <span>{permLabel}</span>
                  <IconChevronDown size={11} />
                </button>

                {openMenu === "perm" ? (
                  <div
                    className="popover-menu perm-popover"
                    data-testid="perm-popover"
                    role="menu"
                    aria-label="执行权限选项"
                  >
                    <div className="popover-header">执行与工具授权模式</div>

                    <button
                      type="button"
                      role="menuitemradio"
                      aria-checked={executionPolicy.mode === "effects"}
                      className={`popover-item${executionPolicy.mode === "effects" ? " is-selected" : ""}`}
                      data-perm-option="ask"
                      onClick={() => {
                        setExecutionPolicy(EFFECTS_RUN_POLICY);
                        setOpenMenu(null);
                      }}
                    >
                      <div className="popover-item-main">
                        <div className="popover-item-title">每次询问</div>
                        <div className="popover-item-desc">
                          允许工具操作；涉及覆盖、删除或访问目录外路径前单独请求你批准
                        </div>
                      </div>
                      {executionPolicy.mode === "effects" ? (
                        <span className="popover-check">
                          <IconCheck size={14} />
                        </span>
                      ) : null}
                    </button>

                    <button
                      type="button"
                      role="menuitemradio"
                      aria-checked={false}
                      className="popover-item is-disabled"
                      data-perm-option="auto"
                      disabled
                    >
                      <div className="popover-item-main">
                        <div className="popover-item-title">
                          <span>自动审批</span>
                          <span className="popover-tag">待后端授权契约接入</span>
                        </div>
                        <div className="popover-item-desc">
                          自动放行已绑定工作区内的常规修改（当前保持执行侧逐次确认）
                        </div>
                      </div>
                    </button>

                    <button
                      type="button"
                      role="menuitemradio"
                      aria-checked={false}
                      className="popover-item is-disabled"
                      data-perm-option="full"
                      disabled
                    >
                      <div className="popover-item-main">
                        <div className="popover-item-title">
                          <span>完全访问</span>
                          <span className="popover-tag">待后端授权契约接入</span>
                        </div>
                        <div className="popover-item-desc">
                          连续读写与执行不中断询问（当前后端契约未开放，不伪造放行）
                        </div>
                      </div>
                    </button>

                    <div className="popover-divider" />

                    <button
                      type="button"
                      role="menuitemradio"
                      aria-checked={executionPolicy.mode === "pure_only"}
                      className={`popover-item${executionPolicy.mode === "pure_only" ? " is-selected" : ""}`}
                      data-perm-option="pure_only"
                      onClick={() => {
                        setExecutionPolicy(PURE_ONLY_RUN_POLICY);
                        setOpenMenu(null);
                      }}
                    >
                      <div className="popover-item-main">
                        <div className="popover-item-title">仅读取与对话</div>
                        <div className="popover-item-desc">
                          本次运行不写文件、不执行有副作用的工具（磁盘空间不足时仍可运行）
                        </div>
                      </div>
                      {executionPolicy.mode === "pure_only" ? (
                        <span className="popover-check">
                          <IconCheck size={14} />
                        </span>
                      ) : null}
                    </button>
                  </div>
                ) : null}
              </div>
            </div>

            {/* 右侧：模型切换器紧邻在蓝色发送按钮正左侧 */}
            <div className="composer-right-controls">
              <ModelStatusControl
                testId="composer-model-btn"
                disabled={pending}
                onOpenModelSettings={onOpenModelSettings}
              />

              <button
                type="submit"
                className="send-btn"
                data-testid="composer-send-btn"
                disabled={pending}
                aria-label="发送"
                title={pending ? "正在创建并启动…" : "发送并开始运行"}
              >
                <IconSend size={16} />
              </button>
            </div>
          </div>

          {error !== null ? (
            <div className="form-error composer-error" role="alert">
              {error}
            </div>
          ) : null}
        </form>
      </section>
    </div>
  );
}
