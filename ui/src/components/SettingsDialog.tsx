import { useEffect, useState } from "react";
import {
  EFFECTS_RUN_POLICY,
  PURE_ONLY_RUN_POLICY,
} from "../api/types";
import type { DaemonView, RunExecutionPolicyV1 } from "../api/types";
import type { StreamStatus } from "../api/stream";
import { formatRelative } from "../lib/format";
import { streamStatusText } from "./AppHeader";
import {
  IconBrain,
  IconChart,
  IconMoon,
  IconRefresh,
  IconShield,
  IconSparkle,
  IconSun,
} from "./Icons";
import { WelcomePet } from "./WelcomePet";
import type { WelcomePetStatus } from "./WelcomePet";

export type ThemeChoice = "light" | "dark" | "system";

export type SettingsTab =
  | "models"
  | "security"
  | "budget"
  | "memory"
  | "data"
  | "appearance";

export interface UiPreferences {
  theme: ThemeChoice;
  enablePet: boolean;
  enableParallax: boolean;
  reduceMotion: boolean;
  defaultPolicy: RunExecutionPolicyV1;
  previewPose: WelcomePetStatus;
}

export interface SettingsDialogProps {
  open: boolean;
  initialTab?: SettingsTab;
  preferences: UiPreferences;
  daemon: DaemonView;
  streamStatus: StreamStatus;
  streamDetail: string | null;
  connectionLost: boolean;
  eventSeqHead: number;
  now: number;
  onUpdatePreferences: (patch: Partial<UiPreferences>) => void;
  onOpenMemory: () => void;
  onRefresh: () => void;
  onReconnectStream: () => void;
  onClose: () => void;
}

const POSE_OPTIONS: ReadonlyArray<{ key: WelcomePetStatus; label: string }> = [
  { key: "welcome", label: "欢迎" },
  { key: "idle", label: "待命" },
  { key: "thinking", label: "思考" },
  { key: "executing", label: "执行" },
  { key: "done", label: "完成" },
  { key: "error", label: "异常" },
  { key: "sleeping", label: "休眠" },
];

export function SettingsDialog({
  open,
  initialTab = "models",
  preferences,
  daemon,
  streamStatus,
  streamDetail,
  connectionLost,
  eventSeqHead,
  now,
  onUpdatePreferences,
  onOpenMemory,
  onRefresh,
  onReconnectStream,
  onClose,
}: SettingsDialogProps) {
  const [activeTab, setActiveTab] = useState<SettingsTab>(initialTab);

  useEffect(() => {
    if (open) setActiveTab(initialTab);
  }, [open, initialTab]);

  useEffect(() => {
    if (!open) return;
    const handleEsc = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handleEsc);
    return () => window.removeEventListener("keydown", handleEsc);
  }, [open, onClose]);

  if (!open) return null;

  const isHealthy =
    !connectionLost &&
    (streamStatus === "live" ||
      streamStatus === "connecting" ||
      streamStatus === "reconnecting" ||
      streamStatus === "polling");

  const closeButton = (
    <button
      type="button"
      className="chip-btn"
      data-testid="close-settings-btn"
      onClick={onClose}
    >
      完成
    </button>
  );

  return (
    <div
      className="modal-mask"
      role="dialog"
      aria-modal="true"
      aria-label="系统设置"
      onClick={onClose}
    >
      <div
        className="settings-dialog"
        onClick={(event) => event.stopPropagation()}
      >
        <nav className="settings-nav" aria-label="设置分类">
          <div className="settings-nav-title">设置中心</div>

          <button
            type="button"
            className={`settings-tab-btn${activeTab === "models" ? " is-active" : ""}`}
            data-settings-tab="models"
            onClick={() => setActiveTab("models")}
          >
            <IconSparkle size={15} />
            <span>模型与服务商</span>
          </button>

          <button
            type="button"
            className={`settings-tab-btn${activeTab === "security" ? " is-active" : ""}`}
            data-settings-tab="security"
            onClick={() => setActiveTab("security")}
          >
            <IconShield size={15} />
            <span>安全与执行授权</span>
          </button>

          <button
            type="button"
            className={`settings-tab-btn${activeTab === "budget" ? " is-active" : ""}`}
            data-settings-tab="budget"
            onClick={() => setActiveTab("budget")}
          >
            <IconChart size={15} />
            <span>预算与限额护栏</span>
          </button>

          <button
            type="button"
            className={`settings-tab-btn${activeTab === "memory" ? " is-active" : ""}`}
            data-settings-tab="memory"
            onClick={() => setActiveTab("memory")}
          >
            <IconBrain size={15} />
            <span>记忆与上下文</span>
          </button>

          <button
            type="button"
            className={`settings-tab-btn${activeTab === "data" ? " is-active" : ""}`}
            data-settings-tab="data"
            onClick={() => setActiveTab("data")}
          >
            <IconRefresh size={15} />
            <span>数据备份与服务</span>
          </button>

          <button
            type="button"
            className={`settings-tab-btn${activeTab === "appearance" ? " is-active" : ""}`}
            data-settings-tab="appearance"
            onClick={() => setActiveTab("appearance")}
          >
            <IconSun size={15} />
            <span>外观与伴侣动效</span>
          </button>
        </nav>

        <div className="settings-body">
          {activeTab === "models" ? (
            <>
              <div className="settings-head">
                <div>
                  <h2>模型与服务商</h2>
                  <p>查看本机守护进程的模型托管架构与连接凭据安全边界</p>
                </div>
                {closeButton}
              </div>

              <div className="setting-row">
                <div>
                  <div className="setting-title">当前模型路由方式</div>
                  <div className="setting-desc">
                    由本机守护进程（Agent Daemon）在启动时加载原生模型适配器（OpenAI / Anthropic / 自定义兼容端点）
                  </div>
                </div>
                <span className="pill pill-info">守护进程托管</span>
              </div>

              <div className="setting-row">
                <div>
                  <div className="setting-title">API 密钥安全隔离</div>
                  <div className="setting-desc">
                    真实模型密钥仅由守护进程从运行环境读取，绝不进入浏览器存储，也绝不写入只追加事件日志
                  </div>
                </div>
                <span className="pill pill-success">零前端驻留</span>
              </div>

              <div className="setting-row setting-row-stack">
                <div>
                  <div className="setting-title">启动环境变量参考</div>
                  <div className="setting-desc">
                    配置环境变量后重启守护进程即可切换服务商或自定义推理网关；运行时动态切模 RPC 待后续接入
                  </div>
                </div>
                <pre className="settings-code-block mono">
                  {`OPENAI_API_KEY / OPENAI_BASE_URL\nANTHROPIC_API_KEY`}
                </pre>
              </div>
            </>
          ) : null}

          {activeTab === "security" ? (
            <>
              <div className="settings-head">
                <div>
                  <h2>安全与执行授权</h2>
                  <p>授权始终在执行侧判定；模型输出与提示词不构成授权依据</p>
                </div>
                {closeButton}
              </div>

              <div className="setting-row">
                <div>
                  <div className="setting-title">新建对话默认执行模式</div>
                  <div className="setting-desc">
                    控制新建任务时输入框左下角默认选中的运行策略（自动审批与完全访问待后端授权契约接入）
                  </div>
                </div>
                <select
                  className="ts-select"
                  aria-label="新建对话默认执行模式"
                  value={preferences.defaultPolicy.mode}
                  onChange={(event) =>
                    onUpdatePreferences({
                      defaultPolicy:
                        event.target.value === "pure_only"
                          ? PURE_ONLY_RUN_POLICY
                          : EFFECTS_RUN_POLICY,
                    })
                  }
                >
                  <option value="effects">每次询问（允许工具操作）</option>
                  <option value="pure_only">仅读取与对话（零写入副作用）</option>
                  <option value="auto" disabled>
                    自动审批（待后端授权契约接入）
                  </option>
                  <option value="full" disabled>
                    完全访问（待后端授权契约接入）
                  </option>
                </select>
              </div>

              <div className="setting-row">
                <div>
                  <div className="setting-title">工作区路径边界校验</div>
                  <div className="setting-desc">
                    所有路径均先解析符号链接与相对跳转后再核对绑定工作区；批准绑定具体动作与参数摘要，一次性消费
                  </div>
                </div>
                <span className="pill pill-success">执行侧强制</span>
              </div>

              <div className="setting-row">
                <div>
                  <div className="setting-title">进程隔离能力说明（如实披露）</div>
                  <div className="setting-desc">
                    工作区边界只约束工具可访问的路径；当前本地执行后端未启用物理进程沙箱，命令审批卡片会醒目提示隔离级别
                  </div>
                </div>
                <span className="pill pill-warn">无物理沙箱</span>
              </div>
            </>
          ) : null}

          {activeTab === "budget" ? (
            <>
              <div className="settings-head">
                <div>
                  <h2>预算与限额护栏</h2>
                  <p>防止长任务与子任务失控消耗，未知用量与未知费用绝不按 0 混算</p>
                </div>
                {closeButton}
              </div>

              <div className="setting-row">
                <div>
                  <div className="setting-title">父子任务共享预占账本</div>
                  <div className="setting-desc">
                    每次模型请求前先原子预占额度，完成后按实际账单结算；子任务额度从父运行预算中统一扣减
                  </div>
                </div>
                <span className="pill pill-success">已启用</span>
              </div>

              <div className="setting-row">
                <div>
                  <div className="setting-title">单次运行用量与成本审阅</div>
                  <div className="setting-desc">
                    在任意会话右上角打开「运行洞察」即可查看该次运行的轮次上限、Token 明细、已知/未知成本及超限拒绝记录
                  </div>
                </div>
                <span className="pill pill-info">运行洞察可见</span>
              </div>

              <div className="setting-row">
                <div>
                  <div className="setting-title">账户级在线限额调整</div>
                  <div className="setting-desc">
                    控制面目前尚未开放账户级账本只读与修改 RPC，界面不展示虚构的全局余额滑块
                  </div>
                </div>
                <span className="pill">待控制面接入</span>
              </div>
            </>
          ) : null}

          {activeTab === "memory" ? (
            <>
              <div className="settings-head">
                <div>
                  <h2>记忆与上下文</h2>
                  <p>长期记忆是只追加事件日志的可重建投影，支持确认、忘记与彻底擦除</p>
                </div>
                {closeButton}
              </div>

              <div className="setting-row">
                <div>
                  <div className="setting-title">作用域隔离与候选确认</div>
                  <div className="setting-desc">
                    区分用户全局（user）、工作区（workspace）与任务（task）三级作用域；模型提议的候选记忆需你确认后生效
                  </div>
                </div>
                <span className="pill pill-success">可审计投影</span>
              </div>

              <div className="setting-row">
                <div>
                  <div className="setting-title">记忆库管理面板</div>
                  <div className="setting-desc">
                    检索词面与语义召回结果、核验来源事件凭据、确认候选记忆或执行忘记/擦除
                  </div>
                </div>
                <button
                  type="button"
                  className="chip-btn is-active"
                  onClick={() => {
                    onClose();
                    onOpenMemory();
                  }}
                >
                  打开记忆库
                </button>
              </div>
            </>
          ) : null}

          {activeTab === "data" ? (
            <>
              <div className="settings-head">
                <div>
                  <h2>数据备份与服务</h2>
                  <p>本地守护进程运行状态、事件同步水位与在线一致性备份</p>
                </div>
                {closeButton}
              </div>

              <div className="setting-row">
                <div>
                  <div className="setting-title">本地守护进程（Agent Daemon）</div>
                  <div className="setting-desc">
                    {daemon.version !== null ? `版本 v${daemon.version}` : "版本未知"}
                    {daemon.started_at !== null
                      ? ` · 启动于 ${formatRelative(daemon.started_at, now)}`
                      : ""}
                    {` · ${streamStatusText(streamStatus, streamDetail)}`}
                  </div>
                </div>
                <span className={`pill ${isHealthy ? "pill-success" : "pill-danger"}`}>
                  {isHealthy ? "健康连接" : "连接中断"}
                </span>
              </div>

              <div className="setting-row">
                <div>
                  <div className="setting-title">事件日志同步水位与数据目录</div>
                  <div className="setting-desc mono">
                    已同步至 #{eventSeqHead}
                    {daemon.data_root !== null ? ` · ${daemon.data_root}` : ""}
                  </div>
                </div>
                <div className="setting-inline-actions">
                  <button type="button" className="chip-btn" onClick={onRefresh}>
                    立即同步
                  </button>
                  <button
                    type="button"
                    className="chip-btn is-active"
                    onClick={onReconnectStream}
                  >
                    重连事件流
                  </button>
                </div>
              </div>

              <div className="setting-row setting-row-stack">
                <div>
                  <div className="setting-title">在线一致性备份与校验命令</div>
                  <div className="setting-desc">
                    在终端可执行在线一致性快照备份、SHA-256 清单核验与保留期只读审计（事件日志永不删除或覆写）
                  </div>
                </div>
                <pre className="settings-code-block mono">
                  {`tianshu backup create <输出目录>\ntianshu backup verify <备份包路径>\ntianshu retention status`}
                </pre>
              </div>
            </>
          ) : null}

          {activeTab === "appearance" ? (
            <>
              <div className="settings-head">
                <div>
                  <h2>外观与伴侣动效</h2>
                  <p>定制界面浅深主题与主页动态伴侣的交互表现（纯内存态，不写浏览器存储）</p>
                </div>
                {closeButton}
              </div>

              <div className="setting-row">
                <div>
                  <div className="setting-title">界面外观主题</div>
                  <div className="setting-desc">
                    实时切换清透浅色、深邃暗色或跟随操作系统外观
                  </div>
                </div>
                <div className="setting-inline-actions">
                  <button
                    type="button"
                    className={`chip-btn${preferences.theme === "light" ? " is-active" : ""}`}
                    data-theme-choice="light"
                    onClick={() => onUpdatePreferences({ theme: "light" })}
                  >
                    <IconSun size={13} />
                    <span>浅色模式</span>
                  </button>
                  <button
                    type="button"
                    className={`chip-btn${preferences.theme === "dark" ? " is-active" : ""}`}
                    data-theme-choice="dark"
                    onClick={() => onUpdatePreferences({ theme: "dark" })}
                  >
                    <IconMoon size={13} />
                    <span>深色模式</span>
                  </button>
                  <button
                    type="button"
                    className={`chip-btn${preferences.theme === "system" ? " is-active" : ""}`}
                    data-theme-choice="system"
                    onClick={() => onUpdatePreferences({ theme: "system" })}
                  >
                    <span>跟随系统</span>
                  </button>
                </div>
              </div>

              <div className="setting-row">
                <div>
                  <div className="setting-title">在欢迎主页展示动态伴侣</div>
                  <div className="setting-desc">
                    新建对话欢迎页与运行洞察面板展示立体矢量伴侣形象
                  </div>
                </div>
                <button
                  type="button"
                  role="switch"
                  aria-checked={preferences.enablePet}
                  aria-label="在欢迎主页展示动态伴侣"
                  data-testid="switch-enable-pet"
                  className={`ts-switch${preferences.enablePet ? " is-on" : ""}`}
                  onClick={() =>
                    onUpdatePreferences({ enablePet: !preferences.enablePet })
                  }
                >
                  <span className="ts-switch-knob" />
                </button>
              </div>

              <div className="setting-row">
                <div>
                  <div className="setting-title">鼠标 2.5D 眼神跟随视差</div>
                  <div className="setting-desc">
                    光标掠过欢迎页伴侣时平滑带动面罩、内凹倒角与发光眼神微转向
                  </div>
                </div>
                <button
                  type="button"
                  role="switch"
                  aria-checked={preferences.enableParallax}
                  aria-label="鼠标 2.5D 眼神跟随视差"
                  data-testid="switch-pet-parallax"
                  className={`ts-switch${preferences.enableParallax ? " is-on" : ""}`}
                  onClick={() =>
                    onUpdatePreferences({
                      enableParallax: !preferences.enableParallax,
                    })
                  }
                >
                  <span className="ts-switch-knob" />
                </button>
              </div>

              <div className="setting-row">
                <div>
                  <div className="setting-title">减少持续动态效果</div>
                  <div className="setting-desc">
                    暂停伴侣持续悬浮、挥手与流光动画，保持安静静态展示
                  </div>
                </div>
                <button
                  type="button"
                  role="switch"
                  aria-checked={preferences.reduceMotion}
                  aria-label="减少持续动态效果"
                  data-testid="switch-reduce-motion"
                  className={`ts-switch${preferences.reduceMotion ? " is-on" : ""}`}
                  onClick={() =>
                    onUpdatePreferences({ reduceMotion: !preferences.reduceMotion })
                  }
                >
                  <span className="ts-switch-knob" />
                </button>
              </div>

              <div className="setting-row setting-pose-preview-row">
                <div className="setting-pose-left">
                  <WelcomePet
                    status={preferences.previewPose}
                    showPedestal={false}
                    parallaxEnabled={false}
                    reducedMotion={preferences.reduceMotion}
                    size={72}
                  />
                  <div>
                    <div className="setting-title">伴侣七态实时预览</div>
                    <div className="setting-desc">点击右侧状态胶囊预览全部表情姿态</div>
                  </div>
                </div>
                <div className="setting-pose-pills">
                  {POSE_OPTIONS.map((item) => (
                    <button
                      key={item.key}
                      type="button"
                      className={`chip-btn${preferences.previewPose === item.key ? " is-active" : ""}`}
                      onClick={() => onUpdatePreferences({ previewPose: item.key })}
                    >
                      {item.label}
                    </button>
                  ))}
                </div>
              </div>
            </>
          ) : null}
        </div>
      </div>
    </div>
  );
}
