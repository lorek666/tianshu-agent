# AGENTS.md · AI 开发强制指令

> 本文件由 AI 编码助手在每次会话开始时自动加载。下列「读 / 更 / 规则」是**硬性要求**，不是建议。
> 本项目是 vibecoding 项目，靠机械门禁 + 知识库对账来兜底「自审盲区 / 测试证据造假 / 文档假话」三大风险。
> 规则依据：PRD 红线与反模式、REVIEW-PRA 复审教训、CONTRIBUTING 流程。**与 PRD 冲突时以 PRD 为准**，并顺手修正本文。

## 一、每次开发前（必做，按序）

1. **读 [docs/KNOWLEDGE.md](docs/KNOWLEDGE.md)**（项目知识库）：当前状态、关键决策、踩坑教训、已知限制。这是上下文的第一来源。
2. **改契约前先读 [docs/PRD-v1.md](docs/PRD-v1.md) §5（五道契约）与 §6（十一条红线）**。红线与功能需求冲突时，**以红线为准**。
3. **读 [CONTRIBUTING.md](CONTRIBUTING.md)**：分支 / 提交规范、独立复审、什么该推什么不该推。

## 二、每次开发后（必做）

4. **更新 [docs/KNOWLEDGE.md](docs/KNOWLEDGE.md)**：状态变了 → 更新 §2/§7（数字重新核实）；新决策/新教训 → 追加 §4/§5；**在 §8 会话日志追加一条**（日期 · 做了什么 · 发现/决定了什么），不可省。
5. **文档与代码对账**：README / PRD / 复审报告里任何「已实现什么 / 多少测试 / 覆盖哪些平台」的陈述，必须与当前代码和 CI 一致；改代码就同步改文档，不留假话与悬空引用。

## 三、流程门禁（机械，不可绕过）

- **`main` 分支保护**：必须走 PR、CI 检查绿（`strict`）、`enforce_admins`（所有者也绕不过）、禁强推/删除。**本地绿不算数，不要尝试直推 main。** required 检查为 ubuntu、macOS、Windows Python 三项与 UI 一项，准确名称及范围见 CONTRIBUTING §6；改检查名称时同步核对远端分支保护。
- **提交前**：跑 `uv lock --check` + `uv run --frozen pytest` + `uv run --frozen ruff check .`。CI 的 lock/ruff 命令相同；pytest 在 Ubuntu/macOS 增加 `-o addopts="" -q -rs`；Windows 使用 `uv run --frozen python scripts/pytest_with_stacks.py -o addopts="" -vv -rs --tb=short --durations=20 --stack-timeout=60`，用于汇总、跳过原因与长等待诊断。包装器保留原生fatal handler，以Python线程替代有frame竞态的原生定时转储；诊断失败必须非零，不能提高25分钟job上限掩盖问题。C扩展长期独占GIL时不能保证60秒栈，须如实保留该诊断边界。UI 改动另跑 CONTRIBUTING §5 的前端检查并提交构建产物。
- **高危改动必须独立复审**：碰授权 / 租约 / 并发写 / 事件日志 / 计费，须由「第二读者」（另一会话或人）复审，不读作者自查结论，只按计划验收项逐条对照，并给出**可复现探针证据**。复审结论分「已修 / 部分修 / 未修」，后两者不得当作已解决。
- **复审与 CI 绑定版本**：记录完整提交 SHA、范围、复现命令、关键输出与未决项；后续实质改动重审受影响范围，不把旧提交的结论冒充最新提交已通过。单维护者的 GitHub 批准人数可以为 0，但不能省略高危独立复审。
- **契约变更单独提交**：`docs(contract):` 单独 commit / 单独 PR，body 写「原方案 → 新方案 → 为什么 → 影响哪些代码」，不夹带代码。
- **红就是红，不装作绿**：不用 `continue-on-error` 掩盖失败、不用 `type: ignore` 压契约越界。**失败要么修掉，要么如实摘除并记 issue**；摘除必须在配置里留下指向该 issue 的注释，不许让失败悄悄消失。历史上 Windows 曾因首跑 12 项失败被摘出 CI 矩阵（issue #21），后来逐项修好、issue 关闭、job 加回矩阵——这条规则的价值不在于"永远摘除"，而在于**摘除必须留痕、修好后必须归位**。

## 四、测试纪律（测试即证据）

- **并发用真子进程 + 屏障**，不用「同进程两条连接」冒充两个进程。
- **时序用注入时钟推进**，不靠 `sleep` 猜。
- **契约断言用真实 `tool_manifest()` / 真实 schema**，不用手写替身——上一版正是手写形状盖住了真洞。
- **两个 provider / 两个后端参数化跑同一套契约测试**，不各写各的。
- **崩溃/恢复用 crashpoint 故障注入**（`os._exit` 硬退，仅 `TIANSHU_FAILPOINT` 生效），覆盖每个 failpoint 与恢复幂等。
- **复现不出所声称行为的测试不算证据**；测试测的就是它声称的。

## 五、数据与事件完整性

- **事件日志 append-only**，绝不 UPDATE / DELETE（红线 1）。
- **状态是事件的投影**：改状态必须同事务写事件，不直接写表（否则回放无法解释为什么被 fence）。
- **副作用执行前持久化 intent，执行后写 result + Receipt**（红线 2/6），Receipt 可独立核对。
- **`applied` / `none_applied` / `unknown` 三态不合并**；结果未知绝不盲目重放，只有 `none_applied` 可安全重试（红线 5/11）。
- **事务走 `EventLog.transaction()` 统一入口**，可组合、不嵌套 `BEGIN`。

## 六、安全与授权

- **授权只在执行侧判定**；模型输出 / 工具输出 / MCP 注解都不构成授权依据（红线 4）。
- **不把权限写进 system prompt**；**不用字符串黑名单做安全**（拦 `rm -rf` 挡不住 `find -delete` / 写脚本执行）。
- **路径先 canonicalize**（解析符号链接 / `..`）再与 workspace 比较，不直接比原始路径。
- **批准绑定具体动作 + 参数**，改目标 / 改参数即作废；一次性消费，执行侧二次核验 digest。
- **区分「模型想错了」与「执行侧跳过了校验」**：后者是 bug，必须修，不能兜底吞掉。
- **不要把路径边界说成安全边界**：`Workspace` 只回答"能不能碰这个路径"，不隔离进程；`LocalExecBackend().capabilities.isolation == "none"`，v1 没有交付过物理沙箱。

## 七、契约与类型纪律

- **封闭集强制**：未知枚举报错，**不得存 `unknown` / 兜底值**（历史非法值读取时抛错，不洗白）。
- **跨层只传版本化契约对象**；SDK 对象 / 异常实例不得越界（红线 7）。
- **工具 schema 变化即视为新工具**，不自动升级（红线 8）。
- **错误是对象（`ModelError`）不是伪 `finish_reason`**；不用 `type: ignore` 压越界值。
- **能力声明只用于路由，不用于授权**（红线 9）。
- **新增封闭集时三处同时改**：代码里的集合、`schemas/` 下的 enum、PRD 对应小节；测试必须把前两者绑在一起，改一处忘另一处要红。

## 八、依赖与平台

- **零第三方运行时依赖**（纯标准库），不擅自引入运行时库。
- **新增依赖须四目标可用**：`mac-arm64` / `mac-x64` / `win-x64` / `win-arm64`（TECH-ARCH §8.3）。
- **`uv.lock` 必须提交且不让它漂移**（CI 有 `uv lock --check` 门禁）；用 `uv run --frozen` 避免顺手改锁文件。
- **平台陈述如实**：没测过的平台不宣称支持。Windows 已在 CI 矩阵里；具体提交的通过/失败状态以 CI 结果与知识库 §2 为准，runner 系统版本以该次日志为准，不把 `windows-latest` 永久等同于某个 Windows Server 版本。**CI 不是用户设备实测**，应写清验证环境，不宣称未经验证的 Windows 10/11、ARM64 或桌面体验支持。
- **Python 3.12**（`>=3.12,<3.13`），不升 3.13 / 3.14。

## 九、提交与仓库卫生

- **Conventional Commits**，新写提交的 body 写「为什么这么改」；Git 自动生成的同步 merge commit 可保留默认消息。squash 最终正文须保留原因、验证与未决项；不为补格式重写已共享历史。Codex 分支使用 `codex/<scope>`，其他分支规范见 CONTRIBUTING §2。
- **公开仓库：密钥 / token / `.env` 永不提交**（推上去等于泄露）。真实模型 key 只从环境变量读；用过的中转 key 应自行作废。
- **不提交** `refs/`（762MB 参考仓库）、`playground/`、本地数据 / 日志 / 快照（`.tianshu/`）。
- **单用户假设（D2）**：不引入多租户 / 多用户设计；租约只防「同用户双实例」。
- **运行时数据目录按入口区分**：内核 CLI 的 `--db` 默认是**当前工作目录下的 `.tianshu/events.db`**（`cli.DEFAULT_DB`）；`daemon` / `host` 已通过 `ipc.default_data_root()` 使用平台标准目录（macOS `~/Library/Application Support/AgentOS`、Windows `%LOCALAPPDATA%\AgentOS`、Linux `$XDG_DATA_HOME/AgentOS`，缺省 `~/.local/share/AgentOS`），可由 `--data-root` / `TIANSHU_DATA_ROOT` 覆盖。两者都不是可提交的项目源码；测试必须使用临时数据根。

## 十、反模式（明确禁止，PRD §12）

- 聊天 transcript 当唯一状态（换框架即丢状态）
- 向量库当事实数据库（索引损坏即丢事实）
- 字符串黑名单做安全
- 业务状态存进框架私有 checkpoint（框架升级 = 数据迁移）
- 权限写在 system prompt 里（越权不可防）
- 「OpenAI-compatible」当语义兼容（工具调用 / 结束原因细节全不同）
- 角色制多 agent 自由讨论（状态不可审计）
- 用 chat 的重试语义重试生成作业（重复计费）
- 把长作业塞进工具调用（流式超时、取消失真）

## 快速定位

- 现状与教训 → [docs/KNOWLEDGE.md](docs/KNOWLEDGE.md)
- 权威规格 → [docs/PRD-v1.md](docs/PRD-v1.md) / [docs/TECH-ARCH.md](docs/TECH-ARCH.md)
- 流程规范 → [CONTRIBUTING.md](CONTRIBUTING.md)
- 复审范例 → [docs/REVIEW-PRA.md](docs/REVIEW-PRA.md)
- 各里程碑验收证据 → [docs/M0-verification.md](docs/M0-verification.md) … [docs/M4-verification.md](docs/M4-verification.md)
