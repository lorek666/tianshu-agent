# MCP 客户端与执行器（G2 MCP 阶段的落地与边界）

- 立据：`docs/PRD-v1.md` §11.1 **G2 完整用户体验**（「按网络 → MCP → 记忆 → 子代理
  推进」）、§5.2.3（动态发现不等于获得调用权限）、§5.4.1（规则编号）、§8.1.1（隔离
  门禁）、§6 红线 4/6/8/9/11。施工规格：[`SPEC-G2-MCP-2026-11-12.md`](SPEC-G2-MCP-2026-11-12.md)。
- 状态：**已实现**——stdio 客户端、工具准入、派发链路、任务级 server 白名单、逐次
  人工批准、凭据与诊断。**不成立的部分**逐条登记在 §5，不写成"已做"。

## 1 这一层交付了什么

| 部件 | 位置 | 作用 |
|---|---|---|
| stdio 客户端 | `src/tianshu/mcp.py` | 换行分隔 JSON-RPC 2.0；`initialize` → `notifications/initialized` → `tools/list`（分页）→ `tools/call`；协议版本只认已知集合，认不出就**拒绝该 server**，不降级 |
| 配置 | 同上 | `TIANSHU_MCP_CONFIG`（**绝对路径** JSON）；未知字段 / 相对 `argv[0]` / 越界名称一律启动期拒绝；只把 `PATH`（Windows 另加 `SYSTEMROOT`）与显式声明的 env 传给子进程 |
| 准入 | `src/tianshu/tool_registry.py` | server 的 `inputSchema` 归一进 `toolschema` 封闭方言；类型不匹配的关键字**拒绝整个工具**；`additionalProperties` 缺省补 `false` 并记 `narrowed`；工具 id `mcp__<server>__<tool>`（两段都查冲突）；`tool_version = mcp1:<sha256(原始 schema + 归一 schema + 描述)>` |
| 注册表 | 同上 | 进程级 `ToolRegistry`（内置 `SPECS` + 已准入 MCP）；`manifest()` 内置按插入顺序、动态工具追加末尾；无 MCP 时与旧工具面**逐字节相同** |
| 派发 | `src/tianshu/dispatcher.py` | `mcp://<server>/<tool>` 资源种类与执行器；**任务白名单 → server 可用性 → 隔离要求 → 逐次人工批准**（前三道都在弹批准之前）；超时/断线/协议失败按 `unknown` 结算；成功产 `receipt(backend="mcp")` |
| 配置入口 | `src/tianshu/cli.py` | 任务级 `--allow-mcp-server <name>`（run / `task create`）；只读诊断 `tianshu mcp list [--json]`（server 状态、协议版本、准入/被拒工具与原因、截断） |
| 契约增量 | `event_schema.py` / `rules.py` / `schemas/` | `task.created.mcp_servers`（恒写）；`tool.intent` / `tool.not_dispatched` / `receipt` 的 `tool_id` 从枚举改为**形状 pattern**（动态 id 无法枚举，放宽的是数据契约不是授权）；`receipt.backend` += `mcp`、`evidence.kind` += `mcp_call`；6 个新规则编号 |

## 2 三道门（缺一不可）

1. **进程级配置 = 用户的信任决定**。装一个 MCP server 就是让一台外部程序以当前用户
   权限跑起来。配置只从用户给的 JSON 读，**不读模型、不读任务参数、不读工作区文件**。
2. **任务级白名单**（`task.created.mcp_servers`，CLI `--allow-mcp-server`）。空 = 一个
   都不许；创建时定死、`task revise` 不可改（扩权必须是新任务）。判在**弹批准之前**。
3. **逐次人工批准**（既有 `c4.mcp_call`，恒需批准，没有自动放行路径）。

三道的顺序是刻意的：白名单与可用性这类"注定失败"的条件先判完，用户不会收到一张
"批了也发不出去"的批准卡片。

## 3 结果与错误口径

| 情况 | 结论 |
|---|---|
| 响应 `result`（含 `isError: true`） | `applied`（它跑过了）；`isError` → `status=failed` |
| JSON-RPC 错误 −32700 / −32600 / −32601 / −32602 | `none_applied`（请求没被当成工具调用执行） |
| 其他错误码（含 −32603、MCP 自定义 −320xx） | `unknown` + `dispatch.mcp_protocol_error` |
| 超时 / 管道断开 / server 退出 / 畸形帧 | `unknown` + 对应原因（`mcp.timeout` / `mcp.closed` / `dispatch.mcp_protocol_error`）；超时自动发 `notifications/cancelled`（**只是请求**） |
| 结果超 `max_result_bytes` | 客户端按 UTF-8 字节截断并报 `bytes` / `truncated` / `dropped_bytes`；`structuredContent` 计入同一预算 |

### 3.1 崩溃与恢复（收尾轮 + PR #124 实测口径）

判定认**两件事**：有没有落盘的 `tool.result`；没有的话，这次调用有没有越过**外发边界**
（发包前提交的 `dispatches.state=executing`）。

| 库里有什么 | 恢复给的结论 | 凭据 | 阻断 |
|---|---|---|---|
| 有 result（含 `isError:true`） | 不动它：`applied` + `status=failed` | 有（`backend="mcp"`，可独立核对） | 不阻断 |
| 有 intent、没有 result；派发行是 `intent`/`revoked`/`rejected`（**没越过外发边界**） | `failed` + `none_applied` + `reason=mcp.not_sent` | **不补**（没有可归因的效果） | **不阻断**：可直接 `resume` |
| 有 intent、没有 result；派发行是 `executing`，或**无从证明**（老库没有派发表 / 没有行 / 状态不认识） | `status/effect_state = unknown`，`reason=mcp.crash_unsettled` | **不补**（unknown 不满足 applied） | 阻断**调用方传入**的那个工作区目录 |

实现上有四处必须说清楚：

1. **按 id 形状认 MCP，不按"`SPECS` 里没有它"认**。MCP 工具确实故意不进静态
   `SPECS`，但"不在表里"是会变的前提（有人把动态 id 塞进静态表、或注册表装配错）。
   认错了就会去查快照清单，把"没有写前证据"读成"确定没写"——把一次可能已经生效的
   调用洗成 `none_applied`。所以 `recovery` / `assess_run` 都在最前面按
   `mcp__` 前缀分流；即使这个 id 出现在静态表里，也仍按 MCP 结算（不读磁盘）。
2. **不猜 server 侧发生了什么**：`output` 里只有错误说明、`recovered`、
   `tool_id` 与一条"自己去 `tianshu mcp list` 查"的指引；**没有** `after_state` /
   `sha256` / 正文。也不按 `__` 反解 server/tool 名字（规范化把 `-`/`.` 换成了 `_`，
   反解出来的名字可能不是用户配的那个）。
3. **派发行同时推到终态**：恢复补写结果时，`dispatches` 行必须离开
   `intent/executing`（`applied → done`，其余 `→ error`），否则
   `dispatch_record()` 会继续显示"仍在执行"，与事件里的"已结算"互相打脸。这条不只
   为 MCP 修：文件写的恢复分支原来也漏着（复审 R2），现在一起修了。两个边界：
   ① **只从 `intent/executing` 推进**——已经 `revoked`/`rejected`/`done`/`error` 的行是
   审计事实，恢复补一条结果不该把它改写（撤销先于结算时尤其明显）；
   ② `updated_at` 用**恢复这一刻**的时钟，不用行自己的 `created_at`（否则更新时间倒退，
   审计/排序会看到"越恢复越旧"）。老库没有 `dispatches` 表时只写结果、不崩。
4. **外发边界标记是"发没发出去"的唯一本机证据**（PR #124）：真正写 server stdin 之前
   先把派发行从 `intent` 推到 `executing`。它只从 `intent` 推进（撤销/作废是审计终态，
   不能被后到的执行改回"正在执行"），并与 `revoke` 共用执行守卫——撤销先到就不发，
   标记先到撤销就明确报错。标记**没有**落盘时的崩溃（`mcp_before_call`）恢复才敢判
   `none_applied`；标记之后的崩溃一律按"可能已发出"处理。

**外发边界**：真正写 server stdin 之前，派发行从 `intent` 推进到 `executing`
（`_mark_mcp_send_boundary`；只从 `intent` 推进、与 `revoke` 共用执行守卫）。它把"请求
发没发出去"变成一条**本机持久化**的事实，恢复不再靠"没有结果就当没发生"去猜。

**三个窗口，结论各不相同**（`tests/test_mcp_crash.py` 真子进程 + `os._exit(77)`）：

- `mcp_before_call`（标记**没**落盘）：请求一定没写进 server stdin → `none_applied` +
  `reason=mcp.not_sent`，**不阻断**；解除成本为零，可以直接 `resume`。
- `mcp_marked`（标记已落盘、请求还没写）：恢复**没法**证明没发出去 → `unknown` +
  阻断。这一小段窗口是有意付出的代价（宁可多挡一个目录，也不把可能已经发生的副作用
  说成没发生）。
- `mcp_after_call`（响应已收到）：假 server 的 `--effect-log` 证明它真的执行了、结果/
  凭据/终态都没落盘 → `unknown` + 阻断，**不重放**（server 只被调用 1 次）。

撤销与发送也由这条标记分界：撤销先到（行已 `revoked`/`rejected`）→ 这次**不发**，结论
是确定的 `rejected`/`none_applied`；标记先到 → 撤销会明确报错"已经开始执行"，而不是假装
撤销成功。`revoked`/`rejected` 在恢复侧同样算"没越过边界"，但**行保持终态**（撤销过就该
一直看得出撤销过）。

## 4 判据与负控

- 判据：`tests/test_mcp_client.py`（43）、`tests/test_mcp_registry.py`（33）、
  `tests/test_mcp_dispatch.py`（12）、`tests/test_mcp_cli.py`（6）、
  **`tests/test_mcp_crash.py`（15；收尾轮 9 → PR #124 扩到 15）**，另有契约判据在
  `tests/test_event_schema.py` / `tests/test_rule_ids.py` / `tests/test_g1d_resource_kinds.py`。
  → 规格 §4 的 c1–c15 与本文件的测试一一对应。
- 负控：规格 §5 的 NB1–NB24；已实测红的逐条列在验收报告 §3（每条都是"回退保护 →
  目标用例红"，并记录 pytest 返回码与耗时）。

## 5 仍然不成立 / 未验证（必须一起读）

1. **协议事实未联网核验**（本机 web provider 不可用）：实现按 MCP 2025-06-18 的已知
   形状写，与**自写假 server** 对得上；**真实第三方 server 兼容性 = 未验证**。自测
   通过不等于兼容性证据。
2. **没有沙箱**：server 以当前用户权限运行，能读写任何文件、联网、读环境变量。三道门
   降低的是"模型自作主张"的风险，**不是**"server 本身恶意"的风险。
   `isolation.CAPABILITY_UNTRUSTED_LOCAL_MCP` **未启用**。
3. **MCP 进程不经过 `exec_backends` 注册表**（长连接 vs 一次性 `spawn` 的语义差）；
   `tool.result.output.mcp.spawn.via_exec_backend=false` 如实标注。因此任务的
   `isolation_requires` 非空时 MCP **一律拒绝**（`c4.mcp_isolation_unproven`）——现有
   实测报告测的不是这个进程，跨对象作证不可接受。
4. **只做 stdio**：HTTP/SSE、OAuth、远程 server 未做。**只做 tools**：resources /
   prompts / sampling / roots / logging / progress / `list_changed` 未做。
   **等批准期间取消不立即生效**：`tools/call` 同步阻塞，取消只在发包前检查一次。
5. **图片/音频/资源正文不收**，只记类型。大结果**不落内容引用**（客户端截断并报字节，
   与 `run_command` 的 `stdout_ref` 不是同一机制）。
6. **没有 MCP 专属预算维度**：调用不预占账本；server 的 CPU/内存/磁盘占用无上限（只有
   结果大小、超时与 `max_tools` 上限）。同 server 的调用串行，跨 run 无压力测试。
7. **Windows 未实机验证**：stdio 管道与线程只在 `windows-latest` 上跑，而 Windows 本来
   就是已知基线红；不许把 CI 绿/红当实机证据。
8. **UI 只有审批卡片上的 `mcp://` 目标**：没有独立的 MCP 面板；`tianshu mcp list` 是
   命令行诊断。
9. **回放只读**：不重连 server、不重放调用；事件里只有结果摘要（`mcp:<sha256>`）与
   有界文本，**没有** server 原始响应全文。

10. **崩溃后的结论分三档**（PR #124）：标记之前 → `none_applied` + 不阻断；标记之后
    或无从证明（老库没有派发表 / 没有行 / 状态不认识）→ `unknown` + 阻断。仍然不成立的：
    **标记提交与真正写出之间**那一小段窗口仍按"可能已发出"处理（有意保守，见 §3.1）；
    阻断对象只有 workspace 字段、**没有**对象级阻断；"挡哪个目录"由调用方负责
    （`recover --workspace` 必填，代码不按 task 绑定核对，传错就挡错——已知代价）；
    server 侧的私有状态（它到底动了什么）**没有任何证据**，恢复也不去问它。

## 6 后续工单

1. ~~MCP 崩溃夹具 + 恢复判据~~ **已完成（收尾轮）**：`tests/test_mcp_crash.py`。
2. ~~**发送前持久标记**~~ **已完成（PR #124）**：复用 `dispatches.state`
   （`intent` → `executing`），不新增事件、不改事件契约；恢复/接管按它分档，撤销与发送
   共用执行守卫。**新剩下的**：标记提交与真正写出之间的窄窗口仍保守阻断；要再往前推需要
   server 侧回执协议（未做）。
3. **对象级阻断**：以 `mcp://server/tool` / server 为阻断对象；现有 guard 只有
   workspace 字段，先定阻断对象模型，不拿调用方传的路径冒充 MCP 的目录。
4. 把长连接 spawn 收进 `exec_backends` 接缝（新 `spawn_session` 语义 + MCP 专属隔离
   探针），之后才谈 `isolation_requires` 对 MCP 成立。
5. 真实第三方 server 兼容性验证（至少两个真实 server），并把协议版本集合与
   `initialize` 形状按官方规范复核。
6. MCP 专属预算维度（调用次数 / 结果字节）与并发调用。
7. UI 的 MCP 面板（server 状态、准入/被拒工具、截断说明）。
8. 大结果内容引用（与 `content://` 同一机制）与文本分页读取。
