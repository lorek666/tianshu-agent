# 隔离支持矩阵（PRD §8.1.1 · W3）

> 规格：`docs/SPEC-PRD-REVIEW-2026-11-11.md` §4；基线 `main @ da84d92`。
> 本文只写**实测过**的结论。没有实机的平台/架构一律 `unknown`，不用 CI 结果或
> 声明冒充实测证据；`unknown` 与 `not_enforced` 一样**阻断**依赖它的能力。

## 1. 结论先行（本机 macOS arm64）

| 后端 | OS / 架构 | 文件与权威数据 | 网络 | 凭据 | 终止与资源 | 结论 |
|---|---|---|---|---|---|---|
| `local-posix`（`LocalExecBackend`） | macOS arm64（本机实测） | `not_enforced` | `not_enforced` | `not_enforced` | `enforced`（POSIX 进程组杀树） | **不能启用任何依赖隔离的能力**：四边界只证明了一条 |
| `local-posix` | macOS x64 | `unknown` | `unknown` | `unknown` | `unknown` | 无实机，未验证 |
| `local-posix` | Linux x64 | `unknown` | `unknown` | `unknown` | `unknown` | 无实机，未验证 |
| `local-windows`（`LocalExecBackend`，Windows 分支） | Windows x64 | `unknown` | `unknown` | `unknown` | `unknown` | 无实机；代码声明 `supports_process_group=False`/`supports_job_objects=False`，杀树不可确认 |
| `local-windows` | Windows arm64 | `unknown` | `unknown` | `unknown` | `unknown` | 无实机，未验证 |
| `app_container` 后端 | 任意 | — | — | — | — | **v1 未实现**（`docs/TECH-ARCH.md` §6 的 L3 未交付），不存在可测后端 |
| `container` 后端 | 任意 | — | — | — | — | **v1 未实现**，不存在可测后端 |

本机只有 `local-posix` 一个后端可用；它如实声明 `isolation="none"`。
探针不认声明，只认观察：三个边界被真实子进程越过，进程树终止在 POSIX 上可确认。

## 2. 本机实测证据（2026-11-11）

环境：

```
Python 3.12.13 (main, Apr 14 2026, 14:33:40) [Clang 22.1.3 ]
Darwin 25.6.0 arm64
```

运行方式（`probe_backend` 全部 spawn 真实子进程，父进程只观察文件/端口/进程；
探针驱动进程退出码 `0`）：

```bash
PYTHONPATH=/Volumes/Date/ts-wt-isolation/src \
  /Volumes/Date/天枢Agent/.venv/bin/python -c \
  "from tianshu import isolation; from tianshu.exec import LOCAL_EXEC_BACKEND as B; \
   print(isolation.probe_backend(B, workspace='/tmp/i-ws', data_root='/tmp/i-data'))"
```

实测输出（原样，仅临时路径省略）：

```
backend=local-posix platform=macos-arm64
- files_authority: not_enforced
  evidence: 子进程读工作区外文件 / 写工作区外文件 / 以 RW 打开权威库（BEGIN IMMEDIATE 后回滚）；
            退出码=0，观察={'OUTSIDE_READ': 'OK', 'OUTSIDE_WRITE': 'OK', 'AUTHORITY_DB': 'OK',
            'outside_written': True}
- network: not_enforced
  evidence: 父进程监听 127.0.0.1:60569，子进程尝试 connect；退出码=0，观察={'LOOPBACK': 'OK'}
- credentials: not_enforced
  evidence: 父进程设哨兵环境变量 TIANSHU_ISOLATION_SENTINEL（值不落文档），子进程读；
            退出码=0，观察={'SENTINEL': 'OK'}
- process_tree: enforced
  evidence: 超时后按进程组杀树，孙进程 9309 在观察窗口内消失
            （超时=True descendants_may_survive=False 耗时=1502ms 孙进程pid=9309）
declared_claims={"files_authority": "not_enforced", "network": "not_enforced",
                 "credentials": "not_enforced", "process_tree": "enforced"}
enabled_capabilities=[]
capability direct_exec_subagent: REFUSED（not_enforced；feature=isolation:files_authority）
capability model_generated_code: REFUSED（not_enforced；feature=isolation:files_authority）
capability untrusted_local_mcp: REFUSED（not_enforced；feature=isolation:files_authority）
exit_code=0
```

各边界探针做了什么（都可复现，见 `src/tianshu/isolation.py`）：

- **文件与权威数据**：子进程读 `<probe>/outside/secret.txt`、写
  `<probe>/outside/written-by-child.txt`、对 `<probe>/authority-probe.db` 做
  `BEGIN IMMEDIATE` 后回滚。三项全部成功 → `not_enforced`。探针用的是
  `data_root` 下的独立临时 SQLite，**不碰真实事件库**。
- **网络**：父进程监听 `127.0.0.1:0`（随机端口），子进程 `connect` 成功 →
  `not_enforced`。
- **凭据**：父进程把哨兵变量放进**允许通过 env 白名单**的子进程环境（测的是
  继承/隔离，不是策略过滤），子进程读到相同值 → `not_enforced`。
- **终止与资源**：子进程派生孙进程并写 pid；超时（1500 ms）后后端按进程组
  `SIGKILL`；观察窗口内孙进程消失且 `descendants_may_survive=False` → `enforced`。
  Windows 分支没有 Job Object 时即使本次看到孙进程消失也只报 `unknown`——
  一次观察不能升级成能力声明。

## 3. 门关闭了哪些能力

`isolation.require_capability()` 要求能力依赖的边界**全部**有 `enforced` 证据。
本轮登记的三个能力（`CAPABILITY_BOUNDARIES`）都依赖四边界：

| 能力 | 本机结论 | 依据 |
|---|---|---|
| `model_generated_code`（模型生成代码在宿主上跑） | **关闭** | 文件/网络/凭据 `not_enforced` |
| `untrusted_local_mcp`（不可信本地 MCP 程序） | **关闭** | 同上 |
| `direct_exec_subagent`（直接执行命令的子代理） | **关闭** | 同上 |

`enabled_capabilities(LOCAL_EXEC_BACKEND, report=本机报告)` 实测返回 `[]`。
如果要开启任何一个，需要换一个在四边界上都给出 `enforced` 实测证据的后端
（v1 没有这样的后端），并在本矩阵里登记该平台/架构的实测输出——不是改
`ExecBackendCapabilities.isolation` 字符串就能开。

`require_isolation(backend, boundary, report=...)` 单边界门可用：本机对
`process_tree` 放行（有实测证据），对另外三个边界拒绝；**不传报告**一律按
`unknown` 拒绝（没有证据不等于隔离成立）。

## 4. 负控（门不是摆设）

### 4.1 声称隔离但探针不通过的假后端

`tests/test_isolation.py::test_lying_backend_is_refused_despite_claiming_isolation`
构造 `capabilities.isolation="app_container"`、`supports_network_isolation=True`
的后端，但 spawn 实际委托给无隔离的本地后端。探针测得
`files_authority`/`network`/`credentials` 均 `not_enforced`，门拒绝
`untrusted_local_mcp`，`enabled_capabilities == []`。**声明没能给自己发通行证。**

另：`BrokenBackend`（spawn 直接抛错）全部落 `unknown`，门同样拒绝——探针自己
坏掉不会被读成"边界被挡住了"。

### 4.2 把 local 探针结果改成 enforced → 测试必须红（已实跑）

临时在 `probe_backend` 里把所有 finding 的状态强制改成 `enforced`，跑
`tests/test_isolation.py`：**8 failed, 5 passed in 3.23s**，其中包括：

```
FAILED test_local_backend_statuses_match_reality
        assert local_report.finding(boundary).status == status
E       AssertionError
FAILED test_local_declaration_does_not_overclaim_measured_evidence
FAILED test_gate_refuses_every_capability_on_local_backend[direct_exec_subagent]
FAILED test_gate_refuses_every_capability_on_local_backend[model_generated_code]
FAILED test_gate_refuses_every_capability_on_local_backend[untrusted_local_mcp]
FAILED test_no_dependent_capability_is_enabled_on_local_backend
        assert isolation.enabled_capabilities(...) == []
E       AssertionError: assert ['direct_exec...ed_local_mcp'] == []
FAILED test_lying_backend_is_refused_despite_claiming_isolation
FAILED test_broken_backend_probe_is_unknown_not_enforced
```

恢复后 `13 passed`。负控证明这些断言钉的是**实测状态**，不是恒真。

## 5. 测试与实现位置

```
$ PYTHONPATH=/Volumes/Date/ts-wt-isolation/src \
    /Volumes/Date/天枢Agent/.venv/bin/python -m pytest -o addopts="" -q tests/test_isolation.py
.............                                                            [100%]
13 passed in 3.79s
```

- 实现：`src/tianshu/isolation.py`（四边界探针、`IsolationFinding`/`IsolationReport`、
  `require_isolation`/`require_capability`、`enabled_capabilities`）；
  `src/tianshu/exec.py` 只加只读声明（`ISOLATION_BOUNDARIES`、三态常量、
  `platform_label()`、`local_isolation_claims()`），**没有改 spawn 语义**。
- 测试：`tests/test_isolation.py`（真实子进程 + 超时保护；假后端负控在 §4.1）。
- 既有 `run_command` 默认行为**未改**：本轮只落门与证据，不把门接进
  `tools.py`/`dispatcher.py`（避免与既有验收冲突）；接入点与依赖关系见
  `CAPABILITY_BOUNDARIES`。
- `recovery.py`/`host.py`/`loop.py`/`permissions.py` 等并行会话占用的文件未改；
  `events.py`/`errors.py` 未改（新异常 `IsolationRefused` 定义在 isolation.py）。

## 6. 仍未验证 / 仍不成立的宣称

- **Windows x64 / arm64 无实机**：四个边界全部 `unknown`；`local-windows` 的
  `supports_process_group=False` 意味着即使本次观察到孙进程消失也不能声明杀树。
  本分支不声称任何 Windows 隔离结论。
- **macOS x64、Linux x64 无实机**：`unknown`。Linux 上 POSIX 进程组杀树**预期**
  与 macOS 相同，但本轮没有实机输出，不写成结论。
- **真正的隔离后端不存在**：`app_container` / `container` 只是能力声明里的级别
  名字，v1 没有实现；因此本机没有任何"四边界全绿"的后端，依赖隔离的能力全部
  关闭是**如实结果**，不是测试没配好。
- **探针只覆盖列出的五类行为**：不覆盖内核攻击面（ptrace、注入、SIP/TCC 交互）、
  不覆盖磁盘配额/CPU 上限、不覆盖 DNS/出站网络（只测 loopback 连通）。
  这些边界的结论仍 `unknown`，不能由本矩阵推出。
- **声明与证据的一致性只在 local 后端上验证**：`local_isolation_claims()` 与实测
  对得上（测试 `test_local_declaration_does_not_overclaim_measured_evidence`）；
  第三方后端要各自提供探针输出。
- **本轮未接产品路径**：模型生成代码 / 不可信 MCP / 直接执行子代理这些能力本身
  在 v1 尚不存在（见 PRD 与规格 §9），门先以库 API 落地；`run_command` 仍只受
  `ExecPolicy` 约束，不经过隔离门——这是本轮的**明确边界**，不是已完成接入。
