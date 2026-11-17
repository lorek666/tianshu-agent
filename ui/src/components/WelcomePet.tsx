import { useId, useState } from "react";
import type { KeyboardEvent, MouseEvent } from "react";
import { IconPlus } from "./Icons";

export type WelcomePetStatus =
  | "welcome"
  | "idle"
  | "thinking"
  | "executing"
  | "done"
  | "error"
  | "sleeping";

export interface WelcomePetProps {
  /** 当前展示姿态；缺省为首屏欢迎姿态。 */
  status?: WelcomePetStatus;
  /** 是否开启鼠标眼神跟随与点击切换互动姿态。 */
  interactive?: boolean;
  /** 是否渲染底部水晶悬浮台。 */
  showPedestal?: boolean;
  /** 舞台宽高像素尺寸。 */
  size?: number;
}

const INTERACTIVE_CYCLE: readonly WelcomePetStatus[] = [
  "welcome",
  "idle",
  "thinking",
  "executing",
  "done",
  "sleeping",
];

const STATUS_LABELS: Record<WelcomePetStatus, string> = {
  welcome: "天枢吉祥物：热情欢迎",
  idle: "天枢吉祥物：安静待命",
  thinking: "天枢吉祥物：正在思考",
  executing: "天枢吉祥物：正在执行",
  done: "天枢吉祥物：顺利完成",
  error: "天枢吉祥物：需要协助",
  sleeping: "天枢吉祥物：小憩休眠",
};

interface ArmPose {
  x: number;
  y: number;
  rot: number;
  waveClass: string;
}

interface PoseSpec {
  bodyTilt: number;
  headTilt: number;
  headTurn: number;
  leftArm: ArmPose;
  rightArm: ArmPose;
  floatClass: string;
}

function poseForStatus(status: WelcomePetStatus): PoseSpec {
  switch (status) {
    case "welcome":
      return {
        bodyTilt: -10,
        headTilt: -10,
        headTurn: -0.42,
        leftArm: { x: -36, y: -14, rot: 10, waveClass: "tp-pet-wave-l" },
        rightArm: { x: 35, y: -10, rot: 26, waveClass: "" },
        floatClass: "tp-pet-float-hero",
      };
    case "idle":
      return {
        bodyTilt: -4,
        headTilt: -5,
        headTurn: 0.15,
        leftArm: { x: -35, y: -10, rot: -34, waveClass: "" },
        rightArm: { x: 35, y: -15, rot: -22, waveClass: "tp-pet-wave-r" },
        floatClass: "tp-pet-float-gentle",
      };
    case "thinking":
      return {
        bodyTilt: 2,
        headTilt: 4,
        headTurn: 0.22,
        leftArm: { x: -35, y: -10, rot: -34, waveClass: "" },
        rightArm: { x: 35, y: -10, rot: 30, waveClass: "" },
        floatClass: "tp-pet-float-gentle",
      };
    case "executing":
      return {
        bodyTilt: 0,
        headTilt: -2,
        headTurn: 0.12,
        leftArm: { x: -35, y: -11, rot: -28, waveClass: "" },
        rightArm: { x: 35, y: -11, rot: 28, waveClass: "" },
        floatClass: "tp-pet-float-gentle",
      };
    case "done":
      return {
        bodyTilt: -5,
        headTilt: -6,
        headTurn: 0.1,
        leftArm: { x: -38, y: -18, rot: 24, waveClass: "tp-pet-wave-l" },
        rightArm: { x: 38, y: -18, rot: -28, waveClass: "tp-pet-wave-r" },
        floatClass: "tp-pet-cheer",
      };
    case "error":
      return {
        bodyTilt: 5,
        headTilt: -7,
        headTurn: 0.18,
        leftArm: { x: -35, y: -9, rot: -36, waveClass: "" },
        rightArm: { x: 35, y: -9, rot: 34, waveClass: "" },
        floatClass: "tp-pet-error",
      };
    case "sleeping":
      return {
        bodyTilt: 0,
        headTilt: 14,
        headTurn: 0,
        leftArm: { x: -35, y: -10, rot: -32, waveClass: "" },
        rightArm: { x: 35, y: -10, rot: 32, waveClass: "" },
        floatClass: "tp-pet-sleep",
      };
  }
}

/**
 * 纯本地 2.5D 高光矢量动态吉祥物组件（零外部运行时依赖、无外链资源）。
 * 所有定位 <g transform> 与 CSS 关键帧动画分层嵌套，避免 CSS transform 覆盖 SVG 坐标。
 */
export function WelcomePet({
  status = "welcome",
  interactive = false,
  showPedestal = true,
  size = 268,
}: WelcomePetProps) {
  const rawId = useId();
  const uid = `tp-${rawId.replace(/[^a-zA-Z0-9_-]/g, "")}`;

  const [cycleIdx, setCycleIdx] = useState(0);
  const [gaze, setGaze] = useState<{ x: number; y: number }>({ x: 0, y: 0 });

  // 外部传入非默认异常/运行状态时优先忠实反映，默认欢迎态下允许点击切换互动表情。
  const activeStatus: WelcomePetStatus =
    status !== "welcome" ? status : interactive ? INTERACTIVE_CYCLE[cycleIdx] ?? "welcome" : "welcome";

  const isError = activeStatus === "error";
  const isSleep = activeStatus === "sleeping";
  const isWelcome = activeStatus === "welcome";
  const isDone = activeStatus === "done";
  const isThink = activeStatus === "thinking";
  const isExec = activeStatus === "executing";
  const isIdle = activeStatus === "idle";

  const neonMain = isError ? "#ff9b26" : "#24ccff";
  const neonCore = isError ? "#fff2db" : "#dcfbff";
  const neonDeep = isError ? "#d95b08" : "#1659e6";
  const eyeNeon = "#22ccff";
  const eyeHot = "#c4f9ff";

  const pose = poseForStatus(activeStatus);
  const vx = pose.headTurn * 11 + gaze.x * 4.2;
  const bx = pose.headTurn * 9 + gaze.x * 3.2;
  const ex = pose.headTurn * 5 + gaze.x * 8.0;
  const vy = gaze.y * 2.6;
  const ey = 2 + gaze.y * 5.2;

  const leftEarRx = Math.max(7.5, 11.5 + pose.headTurn * 5.5);
  const rightEarRx = Math.max(7.5, 11.5 - pose.headTurn * 5.5);
  const leftRingRx = Math.max(4.8, 7.8 + pose.headTurn * 4.2);
  const rightRingRx = Math.max(4.8, 7.8 - pose.headTurn * 4.2);

  const handleMouseMove = (event: MouseEvent<HTMLDivElement>) => {
    if (!interactive) return;
    const rect = event.currentTarget.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return;
    const nx = ((event.clientX - rect.left) / rect.width) * 2 - 1;
    const ny = ((event.clientY - rect.top) / rect.height) * 2 - 1;
    setGaze({
      x: Math.max(-1, Math.min(1, nx)),
      y: Math.max(-1, Math.min(1, ny)),
    });
  };

  const handleMouseLeave = () => {
    if (!interactive) return;
    setGaze({ x: 0, y: 0 });
  };

  const handleCycle = () => {
    if (!interactive || status !== "welcome") return;
    setCycleIdx((prev) => (prev + 1) % INTERACTIVE_CYCLE.length);
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (!interactive) return;
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      handleCycle();
    }
  };

  const renderEyes = () => {
    if (isWelcome || isDone) {
      return (
        <g className="tp-pet-blink">
          <path
            d="M -45 8 C -41 -13, -13 -13, -9 8 C -9 12, -15 13, -18 8 C -21 1, -33 1, -36 8 C -39 13, -45 12, -45 8 Z"
            fill={`url(#${uid}-eye-fill)`}
            filter={`url(#${uid}-eye-glow)`}
          />
          <path
            d="M 9 8 C 13 -13, 41 -13, 45 8 C 45 12, 39 13, 36 8 C 33 1, 21 1, 18 8 C 15 13, 9 12, 9 8 Z"
            fill={`url(#${uid}-eye-fill)`}
            filter={`url(#${uid}-eye-glow)`}
          />
        </g>
      );
    }
    if (isIdle || isSleep) {
      return (
        <g className="tp-pet-pulse">
          <path
            d="M -42 -1 C -35 11, -17 11, -10 -1"
            fill="none"
            stroke={eyeNeon}
            strokeWidth="5.2"
            strokeLinecap="round"
            filter={`url(#${uid}-eye-glow)`}
          />
          <path
            d="M -41 -0.5 C -35 9.5, -17 9.5, -11 -0.5"
            fill="none"
            stroke={eyeHot}
            strokeWidth="2.2"
            strokeLinecap="round"
          />
          <path
            d="M 10 -1 C 17 11, 35 11, 42 -1"
            fill="none"
            stroke={eyeNeon}
            strokeWidth="5.2"
            strokeLinecap="round"
            filter={`url(#${uid}-eye-glow)`}
          />
          <path
            d="M 11 -0.5 C 17 9.5, 35 9.5, 41 -0.5"
            fill="none"
            stroke={eyeHot}
            strokeWidth="2.2"
            strokeLinecap="round"
          />
        </g>
      );
    }
    if (isThink) {
      return (
        <g className="tp-pet-pulse">
          <path
            d="M -39 -9 L -16 1.5 L -38 11"
            fill="none"
            stroke={eyeNeon}
            strokeWidth="5.8"
            strokeLinecap="round"
            strokeLinejoin="round"
            filter={`url(#${uid}-eye-glow)`}
          />
          <path
            d="M -38 -8.5 L -17 1.5 L -37 10.5"
            fill="none"
            stroke={eyeHot}
            strokeWidth="2.4"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
          <path
            d="M 39 -9 L 16 1.5 L 38 11"
            fill="none"
            stroke={eyeNeon}
            strokeWidth="5.8"
            strokeLinecap="round"
            strokeLinejoin="round"
            filter={`url(#${uid}-eye-glow)`}
          />
          <path
            d="M 38 -8.5 L 17 1.5 L 37 10.5"
            fill="none"
            stroke={eyeHot}
            strokeWidth="2.4"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </g>
      );
    }
    if (isExec) {
      return (
        <g className="tp-pet-blink">
          <path
            d="M -41 -1 C -35 10, -18 10, -12 -2"
            fill="none"
            stroke={eyeNeon}
            strokeWidth="5.6"
            strokeLinecap="round"
            filter={`url(#${uid}-eye-glow)`}
          />
          <path
            d="M -40 -0.5 C -35 8.5, -18 8.5, -13 -1.5"
            fill="none"
            stroke={eyeHot}
            strokeWidth="2.5"
            strokeLinecap="round"
          />
          <path
            d="M 12 -2 C 18 10, 35 10, 41 -1"
            fill="none"
            stroke={eyeNeon}
            strokeWidth="5.6"
            strokeLinecap="round"
            filter={`url(#${uid}-eye-glow)`}
          />
          <path
            d="M 13 -1.5 C 18 8.5, 35 8.5, 40 -0.5"
            fill="none"
            stroke={eyeHot}
            strokeWidth="2.5"
            strokeLinecap="round"
          />
        </g>
      );
    }
    return (
      <g className="tp-pet-pulse">
        <rect
          x="-26.5"
          y="-16"
          width="7"
          height="22"
          rx="3.5"
          fill={`url(#${uid}-eye-fill)`}
          filter={`url(#${uid}-eye-glow)`}
        />
        <circle cx="-23" cy="14.5" r="3.8" fill={`url(#${uid}-eye-fill)`} filter={`url(#${uid}-eye-glow)`} />
        <rect
          x="19.5"
          y="-16"
          width="7"
          height="22"
          rx="3.5"
          fill={`url(#${uid}-eye-fill)`}
          filter={`url(#${uid}-eye-glow)`}
        />
        <circle cx="23" cy="14.5" r="3.8" fill={`url(#${uid}-eye-fill)`} filter={`url(#${uid}-eye-glow)`} />
      </g>
    );
  };

  const headAssembly = (
    <>
      {/* 头顶流线弯月弧冠与珠光圆丘基座 */}
      <g transform={`translate(${(-pose.headTurn * 4).toFixed(1)}, -61)`}>
        <path
          d="M 30 5 C 33 -23, 5 -35, -28 -16 C -10 -20, 11 -13, 14 4 Z"
          fill={`url(#${uid}-crest-grad)`}
          stroke="#e2eeff"
          strokeWidth="1.1"
        />
        <path
          d="M 28 3 C 24 -12, 10 -18, -8 -16 C 6 -14, 14 -7, 15 4 Z"
          fill="#354e84"
          opacity="0.62"
        />
        <path
          d="M -22 5 C -15 -11, 15 -11, 21 5 Z"
          fill={`url(#${uid}-helmet-grad)`}
          stroke="#d4e1f7"
          strokeWidth="0.8"
        />
      </g>

      {/* 左侧发光圆环耳罩 */}
      <g transform={`translate(${(-87 + pose.headTurn * 4).toFixed(1)}, 5) rotate(-5)`}>
        <ellipse cx="4" cy="0" rx={leftEarRx.toFixed(1)} ry="28" fill="#172c59" stroke="#cbd9f2" strokeWidth="0.9" />
        <ellipse cx="-1" cy="0" rx={(leftRingRx + 2.2).toFixed(1)} ry="25" fill="#0d1b3a" />
        <ellipse
          cx="-2"
          cy="0"
          rx={leftRingRx.toFixed(1)}
          ry="21.5"
          fill="#234382"
          stroke={neonMain}
          strokeWidth="3.8"
          filter={`url(#${uid}-eye-glow)`}
        />
        <ellipse cx="-2" cy="0" rx={leftRingRx.toFixed(1)} ry="21.5" fill="none" stroke={neonCore} strokeWidth="1.4" />
      </g>

      {/* 右侧发光圆环耳罩 */}
      <g transform={`translate(${(87 + pose.headTurn * 4).toFixed(1)}, 5) rotate(5)`}>
        <ellipse cx="-5" cy="0" rx={(rightEarRx + 1.5).toFixed(1)} ry="28.5" fill="#1a3266" stroke="#cbd9f2" strokeWidth="0.9" />
        <ellipse cx="1" cy="0" rx={(rightRingRx + 2.4).toFixed(1)} ry="25.5" fill="#0d1c3d" />
        <ellipse
          cx="2.5"
          cy="0"
          rx={rightRingRx.toFixed(1)}
          ry="21.5"
          fill="#31569e"
          stroke={neonMain}
          strokeWidth="4.0"
          filter={`url(#${uid}-eye-glow)`}
        />
        <ellipse cx="2.5" cy="0" rx={rightRingRx.toFixed(1)} ry="21.5" fill="none" stroke={neonCore} strokeWidth="1.5" />
      </g>

      {/* 珠光白圆润头盔主体 */}
      <path
        d="M -89 6 C -89 -45, -51 -65, 0 -65 C 51 -65, 89 -45, 89 6 C 89 46, 57 62, 0 62 C -57 62, -89 46, -89 6 Z"
        fill={`url(#${uid}-helmet-grad)`}
        stroke="#c3d3ef"
        strokeWidth="1.1"
      />

      {/* 额头左上柔和高光涂层 */}
      <ellipse
        cx="-30"
        cy="-40"
        rx="35"
        ry="12.5"
        transform="rotate(-18 -30 -40)"
        fill="#ffffff"
        opacity="0.88"
        filter={`url(#${uid}-brow-blur)`}
      />

      {/* 面罩内凹倒角边框 */}
      <path
        className="tp-pet-bezel"
        transform={`translate(${bx.toFixed(2)}, ${(vy * 0.75).toFixed(2)})`}
        d="M -70 6 C -70 -29, -43 -43, 0 -43 C 43 -43, 70 -29, 70 6 C 70 34, 46 44, 0 44 C -46 44, -70 34, -70 6 Z"
        fill={`url(#${uid}-bezel-grad)`}
      />

      {/* 曲面深邃黑曜屏幕 */}
      <g className="tp-pet-visor" transform={`translate(${vx.toFixed(2)}, ${vy.toFixed(2)})`}>
        <path
          d="M -67 6 C -67 -27, -41 -40, 0 -40 C 41 -40, 67 -27, 67 6 C 67 31, 44 41, 0 41 C -44 41, -67 31, -67 6 Z"
          fill={`url(#${uid}-visor-grad)`}
        />
        <path
          d="M -60 -2 C -56 -24, -33 -36, 6 -36 C -18 -29, -42 -14, -55 14 Z"
          fill="rgba(255, 255, 255, 0.16)"
        />
        <path
          d="M -38 37 C -16 40, 16 40, 38 37"
          fill="none"
          stroke={neonMain}
          strokeWidth="1.6"
          opacity="0.52"
        />
        <g className="tp-pet-eyes" transform={`translate(${ex.toFixed(2)}, ${ey.toFixed(2)})`}>
          {renderEyes()}
        </g>
      </g>
    </>
  );

  const orbitalTilt = isWelcome ? -22 : -15;
  const orbitalCy = isWelcome ? 198 : 222;
  const orbitalScale = isWelcome ? 1.0 : 0.86;

  return (
    <div
      className={`welcome-pet-stage${interactive ? " is-interactive" : ""}`}
      style={{ width: size, height: size }}
      data-testid="welcome-pet"
      data-pet-interactive={interactive ? "true" : "false"}
      onMouseMove={handleMouseMove}
      onMouseLeave={handleMouseLeave}
      onClick={interactive ? handleCycle : undefined}
      onKeyDown={interactive ? handleKeyDown : undefined}
      role={interactive ? "button" : undefined}
      tabIndex={interactive ? 0 : undefined}
      title={interactive && status === "welcome" ? "轻触切换吉祥物互动表情" : undefined}
    >
      <svg
        viewBox={`0 0 420 ${showPedestal ? 420 : 325}`}
        className="welcome-pet-svg"
        fill="none"
        xmlns="http://www.w3.org/2000/svg"
        role="img"
        aria-label={STATUS_LABELS[activeStatus]}
      >
        <defs>
          <radialGradient id={`${uid}-helmet-grad`} cx="33%" cy="23%" r="75%" fx="27%" fy="17%">
            <stop offset="0%" stopColor="#ffffff" />
            <stop offset="36%" stopColor="#f5f9ff" />
            <stop offset="68%" stopColor="#d4e0f7" />
            <stop offset="90%" stopColor="#9ab0dc" />
            <stop offset="100%" stopColor={isError ? "#f8cfa2" : "#b8d2ff"} />
          </radialGradient>

          <radialGradient id={`${uid}-body-grad`} cx="32%" cy="24%" r="76%">
            <stop offset="0%" stopColor="#ffffff" />
            <stop offset="42%" stopColor="#edf4ff" />
            <stop offset="78%" stopColor="#c4d5f2" />
            <stop offset="100%" stopColor="#91a9d7" />
          </radialGradient>

          <linearGradient id={`${uid}-arm-grad`} x1="18%" y1="0%" x2="82%" y2="100%">
            <stop offset="0%" stopColor="#ffffff" />
            <stop offset="54%" stopColor="#e5eefc" />
            <stop offset="100%" stopColor="#a2b8e0" />
          </linearGradient>

          <linearGradient id={`${uid}-joint-grad`} x1="0%" y1="0%" x2="100%" y2="100%">
            <stop offset="0%" stopColor="#849fd2" />
            <stop offset="38%" stopColor="#3a5284" />
            <stop offset="78%" stopColor="#172548" />
            <stop offset="100%" stopColor="#5673a8" />
          </linearGradient>

          <linearGradient id={`${uid}-crest-grad`} x1="10%" y1="4%" x2="88%" y2="96%">
            <stop offset="0%" stopColor="#ffffff" />
            <stop offset="34%" stopColor="#b0cefc" />
            <stop offset="72%" stopColor="#6586c6" />
            <stop offset="100%" stopColor="#2c4275" />
          </linearGradient>

          <linearGradient id={`${uid}-bezel-grad`} x1="50%" y1="0%" x2="50%" y2="100%">
            <stop offset="0%" stopColor="#8ba1ce" />
            <stop offset="50%" stopColor="#cddaf2" />
            <stop offset="100%" stopColor="#ffffff" />
          </linearGradient>

          <radialGradient id={`${uid}-visor-grad`} cx="50%" cy="56%" r="62%">
            <stop offset="0%" stopColor="#0b2450" />
            <stop offset="56%" stopColor="#050d20" />
            <stop offset="100%" stopColor="#02050d" />
          </radialGradient>

          <linearGradient id={`${uid}-eye-fill`} x1="50%" y1="0%" x2="50%" y2="100%">
            <stop offset="0%" stopColor="#e0fcff" />
            <stop offset="45%" stopColor="#42dbff" />
            <stop offset="100%" stopColor="#0ea5e9" />
          </linearGradient>

          <radialGradient id={`${uid}-thruster-core`} cx="50%" cy="78%" r="68%">
            <stop offset="0%" stopColor={neonCore} />
            <stop offset="34%" stopColor={neonMain} />
            <stop offset="72%" stopColor={neonDeep} />
            <stop offset="100%" stopColor="#09173b" />
          </radialGradient>

          <linearGradient id={`${uid}-thruster-glass`} x1="0%" y1="0%" x2="100%" y2="100%">
            <stop offset="0%" stopColor="rgba(235, 248, 255, 0.72)" />
            <stop offset="48%" stopColor="rgba(96, 186, 255, 0.24)" />
            <stop offset="100%" stopColor="rgba(205, 238, 255, 0.68)" />
          </linearGradient>

          <linearGradient id={`${uid}-orbit-grad`} x1="0%" y1="50%" x2="100%" y2="50%">
            <stop offset="0%" stopColor="rgba(56, 189, 248, 0.88)" />
            <stop offset="28%" stopColor="rgba(37, 99, 235, 0.34)" />
            <stop offset="70%" stopColor="rgba(56, 189, 248, 0.36)" />
            <stop offset="100%" stopColor="rgba(14, 165, 233, 0.9)" />
          </linearGradient>

          <radialGradient id={`${uid}-floor-glow`} cx="50%" cy="50%" r="50%">
            <stop offset="0%" stopColor={isError ? "rgba(251, 146, 60, 0.48)" : "rgba(56, 189, 248, 0.52)"} />
            <stop offset="55%" stopColor={isError ? "rgba(249, 115, 22, 0.16)" : "rgba(37, 99, 235, 0.16)"} />
            <stop offset="100%" stopColor="rgba(37, 99, 235, 0)" />
          </radialGradient>

          <linearGradient id={`${uid}-pedestal-top`} x1="20%" y1="0%" x2="80%" y2="100%">
            <stop offset="0%" stopColor="#f8fbff" />
            <stop offset="55%" stopColor="#dce8fb" />
            <stop offset="100%" stopColor="#bdd2f4" />
          </linearGradient>

          <linearGradient id={`${uid}-pedestal-side`} x1="0%" y1="0%" x2="100%" y2="0%">
            <stop offset="0%" stopColor="#9fbbe6" />
            <stop offset="32%" stopColor="#e4eefc" />
            <stop offset="68%" stopColor="#93b4e5" />
            <stop offset="100%" stopColor="#b8cef0" />
          </linearGradient>

          <filter id={`${uid}-char-shadow`} x="-25%" y="-20%" width="150%" height="150%">
            <feDropShadow dx="0" dy="9" stdDeviation="9" floodColor="#1e3a8a" floodOpacity="0.16" />
          </filter>

          <filter id={`${uid}-eye-glow`} x="-60%" y="-60%" width="220%" height="220%">
            <feGaussianBlur stdDeviation="3.2" result="blur1" />
            <feGaussianBlur stdDeviation="6.8" result="blur2" />
            <feMerge>
              <feMergeNode in="blur2" />
              <feMergeNode in="blur1" />
              <feMergeNode in="SourceGraphic" />
            </feMerge>
          </filter>

          <filter id={`${uid}-soft-glow`} x="-40%" y="-40%" width="180%" height="180%">
            <feGaussianBlur stdDeviation="3.6" result="b" />
            <feMerge>
              <feMergeNode in="b" />
              <feMergeNode in="SourceGraphic" />
            </feMerge>
          </filter>

          <filter id={`${uid}-warm-glow`} x="-60%" y="-60%" width="220%" height="220%">
            <feGaussianBlur stdDeviation="4.0" result="b" />
            <feMerge>
              <feMergeNode in="b" />
              <feMergeNode in="SourceGraphic" />
            </feMerge>
          </filter>

          <filter id={`${uid}-brow-blur`} x="-30%" y="-30%" width="160%" height="160%">
            <feGaussianBlur stdDeviation="5.2" />
          </filter>
        </defs>

        {/* 底部 3D 水晶悬浮台 */}
        {showPedestal ? (
          <g data-pet-part="pedestal" transform="translate(210, 368)">
            <ellipse cx="0" cy="16" rx="178" ry="36" fill={`url(#${uid}-floor-glow)`} />
            <path
              d="M -140 0 L -140 15 A 140 26 0 0 0 140 15 L 140 0 Z"
              fill={`url(#${uid}-pedestal-side)`}
              stroke="#b4caec"
              strokeWidth="1"
            />
            <ellipse
              cx="0"
              cy="0"
              rx="140"
              ry="26"
              fill={`url(#${uid}-pedestal-top)`}
              stroke="#ffffff"
              strokeWidth="1.6"
            />
            <ellipse
              cx="0"
              cy="1"
              rx="102"
              ry="19"
              fill="rgba(37, 99, 235, 0.16)"
              stroke={neonMain}
              strokeWidth="4.8"
              filter={`url(#${uid}-eye-glow)`}
            />
            <ellipse cx="0" cy="1" rx="102" ry="19" fill="none" stroke="#ffffff" strokeWidth="1.9" />
            <ellipse
              cx="0"
              cy="2"
              rx="78"
              ry="13.5"
              fill="rgba(30, 64, 175, 0.12)"
              stroke="#38bdf8"
              strokeWidth="1.2"
            />
          </g>
        ) : null}

        {/* 后层环绕特效 */}
        {(isWelcome || isIdle) ? (
          <g
            data-pet-part="orbital-ribbon"
            transform={`translate(210, ${orbitalCy}) rotate(${orbitalTilt}) scale(${orbitalScale})`}
          >
            <path
              d="M -182 0 A 182 48 0 0 1 182 0 L 162 -2 A 162 40 0 0 0 -162 2 Z"
              fill={`url(#${uid}-orbit-grad)`}
              opacity="0.52"
            />
            <path d="M -182 0 A 182 48 0 0 1 182 0" fill="none" stroke="#38bdf8" strokeWidth="1.6" opacity="0.72" />
          </g>
        ) : null}

        {isThink ? (
          <g data-pet-part="holo-panels">
            <g transform="translate(58, 44) skewY(-10)">
              <g className="tp-pet-holo-2">
                <rect width="64" height="52" rx="6" fill="rgba(37, 99, 235, 0.16)" stroke="#38bdf8" strokeWidth="1.5" />
                <rect x="9" y="10" width="24" height="4.5" rx="2" fill="#0ea5e9" />
                <rect x="9" y="20" width="42" height="3.5" rx="1.5" fill="#38bdf8" opacity="0.85" />
                <rect x="9" y="28" width="32" height="3.5" rx="1.5" fill="#38bdf8" opacity="0.65" />
              </g>
            </g>
            <g transform="translate(294, 56) skewY(11)">
              <g className="tp-pet-holo-1">
                <rect width="74" height="102" rx="8" fill="rgba(37, 99, 235, 0.18)" stroke="#38bdf8" strokeWidth="1.6" />
                <circle cx="20" cy="26" r="8" fill="none" stroke="#0284c7" strokeWidth="2.4" />
                <rect x="34" y="19" width="28" height="4.5" rx="2" fill="#0ea5e9" />
                <rect x="12" y="46" width="50" height="4" rx="2" fill="#38bdf8" />
                <rect x="12" y="56" width="38" height="4" rx="2" fill="#38bdf8" opacity="0.7" />
              </g>
            </g>
          </g>
        ) : null}

        {isExec ? (
          <g data-pet-part="hud-screens">
            <g transform="translate(76, 46) skewY(-6)">
              <g className="tp-pet-holo-2">
                <rect width="84" height="48" rx="6" fill="rgba(37, 99, 235, 0.15)" stroke="#38bdf8" strokeWidth="1.4" />
                <rect x="10" y="11" width="40" height="4.5" rx="2" fill="#0ea5e9" />
                <rect x="10" y="21" width="60" height="3.5" rx="1.5" fill="#38bdf8" opacity="0.75" />
              </g>
            </g>
            <g transform="translate(286, 54) skewY(11)">
              <g className="tp-pet-holo-1">
                <rect width="90" height="122" rx="9" fill="rgba(37, 99, 235, 0.2)" stroke="#0ea5e9" strokeWidth="1.6" />
                <path
                  d="M 12 58 L 30 46 L 48 61 L 76 40"
                  fill="none"
                  stroke="#0284c7"
                  strokeWidth="2.6"
                  strokeLinecap="round"
                />
                <rect x="11" y="76" width="66" height="5.5" rx="2.5" fill="#0ea5e9" />
                <rect x="11" y="88" width="50" height="5.5" rx="2.5" fill="#38bdf8" opacity="0.8" />
              </g>
            </g>
          </g>
        ) : null}

        {/* 吉祥物主体（带柔和立体环境投影） */}
        <g filter={`url(#${uid}-char-shadow)`}>
          {isSleep ? (
            <g className={pose.floatClass}>
              <g transform="translate(210, 294)">
                <ellipse
                  cx="-42"
                  cy="8"
                  rx="34"
                  ry="16"
                  transform="rotate(10 -42 8)"
                  fill={`url(#${uid}-helmet-grad)`}
                  stroke="#c5d5f0"
                  strokeWidth="0.9"
                />
                <ellipse
                  cx="36"
                  cy="9"
                  rx="35"
                  ry="17"
                  transform="rotate(-12 36 9)"
                  fill={`url(#${uid}-helmet-grad)`}
                  stroke="#c5d5f0"
                  strokeWidth="0.9"
                />
                <ellipse cx="-2" cy="2" rx="56" ry="9" fill="rgba(26, 48, 98, 0.22)" />
              </g>
              <g transform={`translate(212, 232) rotate(${pose.headTilt})`}>{headAssembly}</g>
            </g>
          ) : (
            <g className={pose.floatClass}>
              <g transform={`translate(210, 218) rotate(${pose.bodyTilt})`}>
                {/* 左侧双段关节手臂 */}
                <g transform={`translate(${pose.leftArm.x}, ${pose.leftArm.y}) rotate(${pose.leftArm.rot})`}>
                  <g className={pose.leftArm.waveClass}>
                    <rect x="-20" y="-12.5" width="26" height="25" rx="11" fill={`url(#${uid}-joint-grad)`} />
                    <path
                      d="M -12 -11.5 C -34 -16.5, -72 -14, -77 0 C -72 15, -34 16.5, -12 11.5 C -7 6, -7 -6, -12 -11.5 Z"
                      fill={`url(#${uid}-arm-grad)`}
                      stroke="#c8d7f0"
                      strokeWidth="0.8"
                    />
                    <path
                      d="M -15 -10.5 C -9 -5, -9 5, -15 10.5"
                      fill="none"
                      stroke="rgba(168, 198, 245, 0.9)"
                      strokeWidth="1.5"
                    />
                  </g>
                </g>

                {/* 右侧双段关节手臂 */}
                <g transform={`translate(${pose.rightArm.x}, ${pose.rightArm.y}) rotate(${pose.rightArm.rot})`}>
                  <g className={pose.rightArm.waveClass}>
                    <rect x="-6" y="-12.5" width="26" height="25" rx="11" fill={`url(#${uid}-joint-grad)`} />
                    <path
                      d="M 12 -11.5 C 34 -16.5, 72 -14, 77 0 C 72 15, 34 16.5, 12 11.5 C 7 6, 7 -6, 12 -11.5 Z"
                      fill={`url(#${uid}-arm-grad)`}
                      stroke="#c8d7f0"
                      strokeWidth="0.8"
                    />
                    <path
                      d="M 15 -10.5 C 9 -5, 9 5, 15 10.5"
                      fill="none"
                      stroke="rgba(168, 198, 245, 0.9)"
                      strokeWidth="1.5"
                    />
                  </g>
                </g>

                {/* 机械颈环 */}
                <ellipse cx="0" cy="-38" rx="25" ry="7.5" fill="#081124" />
                <ellipse cx="0" cy="-36" rx="21" ry="4.8" fill="none" stroke="#6482b8" strokeWidth="1.8" />

                {/* 下部半透明蓝晶推进器 */}
                <path d="M -34 24 C -30 52, -14 68, 0 70 C 14 68, 30 52, 34 24 Z" fill={`url(#${uid}-thruster-core)`} />
                <ellipse cx="0" cy="56" rx="16" ry="8" fill={neonCore} opacity="0.88" filter={`url(#${uid}-eye-glow)`} />
                <path
                  d="M -36 22 C -34 49, -16 69, 0 71 C 16 69, 34 49, 36 22 C 21 32, -21 32, -36 22 Z"
                  fill={`url(#${uid}-thruster-glass)`}
                  stroke="#93d5ff"
                  strokeWidth="1.2"
                />

                {/* 饱满珠光白躯干 */}
                <path
                  d="M -36 -33 C -49 -14, -49 18, -34 32 C -17 42, 17 42, 34 32 C 49 18, 49 -14, 36 -33 C 19 -38, -19 -38, -36 -33 Z"
                  fill={`url(#${uid}-body-grad)`}
                  stroke="#c5d5f0"
                  strokeWidth="0.9"
                />
                <path
                  d="M -30 28 C -14 37, 14 37, 30 28"
                  fill="none"
                  stroke={neonMain}
                  strokeWidth="2.4"
                  opacity="0.45"
                  filter={`url(#${uid}-soft-glow)`}
                />

                {/* 胸口发光能量圆环 */}
                <circle cx={(pose.headTurn * 6).toFixed(1)} cy="-1" r="16" fill="#173873" opacity="0.28" />
                <circle
                  cx={(pose.headTurn * 6).toFixed(1)}
                  cy="-1"
                  r="13"
                  fill="#1b3f7d"
                  stroke={neonMain}
                  strokeWidth="4.8"
                  filter={`url(#${uid}-eye-glow)`}
                />
                <circle cx={(pose.headTurn * 6).toFixed(1)} cy="-1" r="13" fill="none" stroke={neonCore} strokeWidth="1.8" />
              </g>

              <g transform={`translate(210, 124) rotate(${pose.headTilt})`}>{headAssembly}</g>
            </g>
          )}
        </g>

        {/* 前层环绕流光与状态专属配件 */}
        {(isWelcome || isIdle) ? (
          <>
            <g transform={`translate(210, ${orbitalCy}) rotate(${orbitalTilt}) scale(${orbitalScale})`}>
              <path
                d="M 182 0 A 182 48 0 0 1 -182 0 L -160 3 A 161 40 0 0 0 162 -2 Z"
                fill={`url(#${uid}-orbit-grad)`}
                opacity="0.86"
                filter={`url(#${uid}-soft-glow)`}
              />
              <path d="M 182 0 A 182 48 0 0 1 -182 0" fill="none" stroke="#38bdf8" strokeWidth="2.1" />
              <path
                className="tp-pet-orbit-flow"
                d="M 182 0 A 182 48 0 0 1 -182 0"
                fill="none"
                stroke="#ffffff"
                strokeWidth="3"
                strokeDasharray="56 364"
                strokeLinecap="round"
              />
              <circle cx="-164" cy="20" r="3.2" fill="#38bdf8" filter={`url(#${uid}-eye-glow)`} />
              <circle cx="-112" cy="37" r="2.6" fill="#0ea5e9" filter={`url(#${uid}-eye-glow)`} />
            </g>
            <g transform="translate(114, 286)">
              <g className="tp-pet-sparkle-1">
                <path
                  d="M0 -8 C1.2 -1.8 1.8 -1.2 8 0 C1.8 1.2 1.2 1.8 0 8 C-1.2 1.8 -1.8 1.2 -8 0 C-1.8 -1.2 -1.2 -1.8 0 -8Z"
                  fill="#38bdf8"
                />
              </g>
            </g>
          </>
        ) : null}

        {isThink ? (
          <g transform="translate(34, 136) skewY(-11)">
            <g className="tp-pet-holo-1">
              <rect
                width="70"
                height="88"
                rx="8"
                fill="rgba(37, 99, 235, 0.22)"
                stroke="#0ea5e9"
                strokeWidth="1.6"
                filter={`url(#${uid}-soft-glow)`}
              />
              <ellipse cx="18" cy="25" rx="6.5" ry="10" fill="none" stroke="#0284c7" strokeWidth="2.4" />
              <rect x="30" y="18" width="28" height="4.5" rx="2" fill="#0ea5e9" />
              <rect x="11" y="47" width="48" height="4.5" rx="2" fill="#38bdf8" />
              <rect x="11" y="57" width="36" height="4.5" rx="2" fill="#38bdf8" opacity="0.75" />
            </g>
          </g>
        ) : null}

        {isExec ? (
          <g transform="translate(18, 114) skewY(6)">
            <g className="tp-pet-holo-2">
              <rect
                width="106"
                height="82"
                rx="9"
                fill="rgba(37, 99, 235, 0.24)"
                stroke="#0ea5e9"
                strokeWidth="1.6"
                filter={`url(#${uid}-soft-glow)`}
              />
              <circle cx="24" cy="27" r="10" fill="none" stroke="#0284c7" strokeWidth="3" />
              <rect x="42" y="18" width="50" height="5" rx="2.5" fill="#0ea5e9" />
              <rect x="13" y="48" width="80" height="6" rx="3" fill="#38bdf8" />
            </g>
          </g>
        ) : null}

        {isDone ? (
          <g data-pet-part="sparkles">
            <g transform="translate(68, 92)">
              <g className="tp-pet-sparkle-1">
                <path
                  d="M0 -24 C3 -6 6 -3 24 0 C6 3 3 6 0 24 C-3 6 -6 3 -24 0 C-6 -3 -3 -6 0 -24Z"
                  fill="#fef3c7"
                  stroke="#f59e0b"
                  strokeWidth="1.2"
                  filter={`url(#${uid}-warm-glow)`}
                />
              </g>
            </g>
            <g transform="translate(336, 72)">
              <g className="tp-pet-sparkle-2">
                <path
                  d="M0 -19 C2.5 -5 5 -2.5 19 0 C5 2.5 2.5 5 0 19 C-2.5 5 -5 2.5 -19 0 C-5 -2.5 -2.5 -5 0 -19Z"
                  fill="#fef3c7"
                  stroke="#f59e0b"
                  strokeWidth="1.1"
                  filter={`url(#${uid}-warm-glow)`}
                />
              </g>
            </g>
          </g>
        ) : null}

        {isError ? (
          <g data-pet-part="warning-sign" transform="translate(278, 36)">
            <g className="tp-pet-holo-1">
              <path
                d="M 34 4 L 64 56 Q 67 62 60 62 L 8 62 Q 1 62 4 56 Z"
                fill="rgba(251, 146, 60, 0.34)"
                stroke="#f97316"
                strokeWidth="3.2"
                strokeLinejoin="round"
                filter={`url(#${uid}-warm-glow)`}
              />
              <line x1="34" y1="22" x2="34" y2="39" stroke="#fff7ed" strokeWidth="4.8" strokeLinecap="round" />
              <circle cx="34" cy="49.5" r="3" fill="#fff7ed" />
            </g>
          </g>
        ) : null}

        {isSleep ? (
          <g data-pet-part="sleep-zzz">
            <g transform="translate(274, 88)">
              <g className="tp-pet-z-1">
                <path
                  d="M 0 22 H 20 L 2 44 H 22"
                  fill="none"
                  stroke="#0ea5e9"
                  strokeWidth="4.6"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </g>
            </g>
            <g transform="translate(302, 56)">
              <g className="tp-pet-z-2">
                <path
                  d="M 0 14 H 28 L 3 44 H 30"
                  fill="none"
                  stroke="#38bdf8"
                  strokeWidth="5.4"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </g>
            </g>
          </g>
        ) : null}
      </svg>
    </div>
  );
}

export interface WelcomeEmptyHeroProps {
  connectionAlert: boolean;
  creatingTask: boolean;
  onNewTask: () => void;
}

/** 主界面中央空状态欢迎舞台：上方悬浮动态宠物，下方显示欢迎用户引导与新建任务入口。 */
export function WelcomeEmptyHero({
  connectionAlert,
  creatingTask,
  onNewTask,
}: WelcomeEmptyHeroProps) {
  const petStatus: WelcomePetStatus = connectionAlert
    ? "error"
    : creatingTask
      ? "thinking"
      : "welcome";

  return (
    <div className="welcome-hero">
      <WelcomePet status={petStatus} interactive size={264} />
      <h2 className="welcome-title">欢迎使用天枢</h2>
      <p className="welcome-hint">
        你好，欢迎回来！用一句话说明你想完成的目标，并指定允许读写的目录；涉及覆盖或删除的操作会单独请求你批准。
      </p>
      <div className="welcome-action">
        <button type="button" className="btn btn-primary" onClick={onNewTask}>
          <IconPlus size={14} />
          新建任务
        </button>
      </div>
    </div>
  );
}
