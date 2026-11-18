# 路线图 · 「完整 Agent 系统」全量工作流（2026-11-14）

> 应对维护者决策「全做」：把缺口分析（同日会话）转成**有序工作流账本**。
> 每个工作流独立 SPEC → 实现 → 测试/负控 → 文档对账 → 独立 PR → 四平台 CI；
> 契约相关改动（C1/C2/C5、schemas/*）必须先走 `docs(contract)` 独立 PR。
> 状态只有六档：`未开始 / SPEC / 实现 / 门禁绿 / CI绿 / 已合`——不许把未合写成已合。
> 占用面每日重核；被其他会话占着的领域不并行开工。

## 工作流账本

| # | 工作流 | 缺口出处 | 契约影响 | 冲突面 | 外部阻塞 | 状态 |
|---|---|---|---|---|---|---|
| W1 | Keychain 凭据后端（macOS） | §7 F12 | 无（接缝已在） | credentials.py 冷区 | 无（本机可测） | **已并入 main**（#151，`595be35`） |
| W2 | net.py 速率限制/重试 | §7 G2 网络 | 无 | tools.py 接线（中） | 无 | **已并入 main**（#152，`7f4c07d`，run43668070804） |
| W3 | state 响应 8MiB 上界 | §7 指标条 | IPC 语义（谨慎，加字段不升版本） | control.py（已交付，见 SPEC-STATE-RESPONSE-BOUND-2026-11-15） | 无 | **已并入 main `c637b52`**（#166，run 43721830851） |
| W4 | 流式输出 | capability 声明 | **C1 + 事件面，docs(contract) 先行** | model.py / loop.py（热） | 无 | **已并入 main**（契约 #190 `beedf51` / 实现 #191 `e18624c`，run 44173429146） |
| W5 | 调度/定时任务 | 全库 0 匹配 | 无（v1 零新事件族；C3 评估见 CONTRACT-W5-SCHEDULER-2026-11-15.md） | daemon.py（_driver_loop 已接线：装配点 + tick 调用） | 无 | **已并入 main `c637b52`**（#167，run 43724954970） |
| W6 | 记忆语义层：真 embedding provider + 检索评测 | §7 N2/N3 | provider 接缝已在 | memory_index.py（冷） | 无 | **已并入 main**（#154，`24eabc4`，run43670453171） |
| W7 | 记忆 L2/L3 生成式合成 + 冲突消解 | §7 N3 | 另立契约（N3 教训） | memory_distill.py（冷） | 无 | **已并入 main**（#157，`c8b53bb`，run43681412578） |
| W8 | 预算账本 UI + 子任务 UI | §7 挂账④⑤ | 无 | ui/src（冷） | 无 | **已并入 main**（#155，`d37a84c`，run43676855243） |
| W9 | 网络深度：分页读取 / DNS 二次校验 / 搜索 API | §7 G2 | 工具 schema 评估 | net.py / tools.py | 拍板已锁定：DNS 二次校验 + 分页读取 | 实现准备中 |
| W10 | 子代理深度：写权限 / 深度>1 / fork 式 | §7 G2 D 系 | C2/C4 评估 | loop.py / worker.py（热） | 无 | **已并入 main**（#156，`134e9f0`，run43679118848） |
| W11 | 巨石模块拆分（dispatcher/loop/tasks/memory/cli） | 工程债 | 无（纯重构，契约不动） | **热文件，按依赖分层推进** | 无 | 实现中 |
| W12 | C1/C2/C5 字段级 schema 冻结 | §7 疤 | **docs(contract) 独立 PR** | schemas/ + PRD | 无 | **已并入 main**（契约 #190 `beedf51` / 绑定与 `run.end` v2 #191 `e18624c`，run 44173429146） |
| W13 | 桌面外壳：Tauri + 托盘 + 安装包（R0–R3） | M5-D1/D2/D5 | IPC/UI 契约不动 | `desktop/` + `scripts/*release*` + `release.yml` | 签名/公证按 T1 不做；四目标 release 实跑与真机安装待验 | **R0–R3 代码已并入 main**（#170 / #174 / #175 / #186）；四目标 release 实跑与真机验收待完成 |
| W14 | 进程级沙箱（第二 ExecBackend + 接产品路径） | §7 隔离条 | F2 门语义 | exec*/isolation | 拍板已锁定：macOS Seatbelt + Linux Landlock | 实现准备中 |
| W15 | 保留期/GC（summaries、dispatch_requests、审计表） | §7 多处挂账 | 事件面评估 | 与 G3 后续重叠 | 拍板已锁定：保守安全 GC（零删 events/summaries） | **v1 只读审计已并入 main**（#169，`7f88994`）；安全 GC 下半场实现准备中 |

## 外部阻塞清单（做了做不了 / 需要拍板，不许冒充已解决）

- **Windows 10/11、ARM64 实机**：CI runner（Server 2025）不等于用户设备；所有平台陈述保持「未验证」。
- **Apple 签名 / 公证**：需要开发者账号与证书；W13 先交付无签名安装脚本（CONTRIBUTING §8 的 T1 既定策略）。
- **真实模型评测（m4.5 复跑）**：中转站 503 全模型不可用（M4-verification §7.4），等凭据可用。
- **图像/视频生成**（PRD 非目标）：E2 作业底座已在（jobs.py）；是否进入 v2、走自建还是接服务（可灵/Runway/Veo）+ 计费模式，**待拍板后另立 PRD 增量**，不顺手做。
- **MCP 真实第三方 server 验证**：需要选定真实 server 清单。
- **多租户 / 防本机用户篡改**：D2 明确排除，维持非目标（除非维护者改拍板）。

## 排序依据

1. 先做冷区、无契约、单会话可闭环的（W1 → W2 → W6）；
2. 被占用面挡住的（W3/W5/W15 等 #146；W12 等 #141）排后，落地即开工；
3. 契约相关（W4/W12）走 docs(contract) 先行，实现 PR 跟后；
4. 需要拍板/外部资源的（W9 搜索 API、W13 签名、W14 沙箱方案、图像视频）单列，逐项拍板后进入实施；
5. W11（拆分）放最后：它把所有热文件翻一遍，必须在途工作全部落地后做，避免制造大规模冲突。
