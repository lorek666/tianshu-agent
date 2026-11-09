# F1 执行原语：`spawn`（已实现）

关联：PRD §7 F1、TECH-ARCH §6。上游是 F2 的判定（`src/tianshu/execpolicy.py`），
本模块只负责**真正创建并等待进程**。

## 先说清楚：这不是沙箱

`LocalExecBackend.capabilities.isolation = "none"`。

argv 白名单、cwd 限制、env 白名单都是**策略约束**，不是隔离。子进程照样能读
整个磁盘、能联网、能 fork。真正的隔离（Seatbelt / Landlock / AppContainer /
容器）不在 v1 的交付范围——`docs/TECH-ARCH.md` §6 已经定调，本模块不假装有。
任何 UI 都不得把 `isolation="none"` 显示成"已隔离"。

## 公开 API

```python
@dataclass(frozen=True)
class ExecResult:
    exit_code: int | None      # None = 未正常退出（被信号杀死 / 超时）
    signal: int | None         # 被信号杀死时的 signum（POSIX）
    timed_out: bool
    stdout: str                # 已按 max_output_bytes 截断
    stderr: str
    truncated: bool
    stdout_ref: str | None     # 完整原文的 sha256（截断且给了 blob_dir 时）
    stderr_ref: str | None
    duration_ms: int
    stderr_merged: bool        # PTY 模式下 True：stderr 已并入 stdout
    descendants_may_survive: bool  # 超时/终止时是否无法确认子树已死

def spawn(
    argv, *, cwd, env, policy, capabilities, blob_dir=None, pty=False
) -> ExecResult
```

`spawn` 是**同步**的：等进程结束（或被超时杀掉）后返回。`LocalExecBackend`
把能力声明与 `spawn` 绑在一起；`LOCAL_EXEC_CAPABILITIES` 是当前平台的实例。

调用顺序：`filtered_env()` 过滤环境 → `check_spawn()` 判定（argv/cwd/上限/env）
→ 若本次 `pty=True`，再过「策略许可 + `check_capabilities()`」两道门 →
`Popen(shell=False)`。任何拒绝都在创建进程之前抛出，绝不降级后照跑。

## PTY 什么时候用（每次调用的请求，不是策略模式）

`spawn(..., pty=True)` 是**本次调用**的请求；`ExecPolicy.allow_pty` 只是许可
（"允不允许用 PTY"），不是"这次要不要用"。四种组合：

| 本次请求 | 策略 `allow_pty` | 后端能力 | 结果 |
|---|---|---|---|
| `pty=True` | `True` | 支持 | 走 PTY；`stderr_merged=True`，stderr 不可分离 |
| `pty=True` | `True` | 不支持 | `PolicyUnsupported`（换后端/换平台，改策略没用） |
| `pty=True` | `False` | 任意 | `PolicyDenied`（规则 `f2.pty_not_allowed`，改策略能解决） |
| `pty=False` | 任意 | 任意 | 走管道；`stderr_merged=False`，stderr 可独立读取 |

**默认必须走管道。** 如果"策略打开 PTY"就等于"每次调用都走 PTY"，会静默改变
一串真实命令的语义：`isatty()` 变真（git 开颜色、less 分页、进度条、Python
改缓冲策略）、行尾从 `\n` 变成 `\r\n`、stdin 不再是 DEVNULL。用户看到的
行为会与终端不一致，而且事后极难归因。

PTY 是终端语义：stdout/stderr 只有一条流，所以 `stderr` 恒为空、内容都在
`stdout` 里，结果用 `stderr_merged=True` 明确标出来——不允许多一个"看起来
空其实被合并了"的字段。

## 能力矩阵（如实声明）

| 能力 | macOS / Linux（posix） | Windows |
|---|---|---|
| `isolation` | `none` | `none` |
| `supports_pty` | ✅（`pty.openpty`） | ❌（标准库没有 ConPTY） |
| `supports_signals` | ✅（Unix 信号语义） | ❌（只有退出码 / TerminateProcess） |
| `supports_process_group` | ✅（`start_new_session` + `killpg`） | ❌（零依赖拿不到 Job Object） |
| `supports_job_objects` | ❌ | ❌（未接 pywin32/ctypes） |
| `supports_network_isolation` | ❌ | ❌ |

**Windows 的证据状态**：本机是 macOS，Windows 专属分支的用例标了
`skipif(os.name != "nt")`，当前没有 Windows CI，因此这些行为**尚未被实测**。
不接受"看起来写了就算验证过"。

`pty=True` 在 Windows 上永远是 `PolicyUnsupported`（标准库没有 ConPTY）；
即使调用方伪造 `supports_pty=True`，实现也会拒绝，不会用管道假装。

## 退出语义（三者分开）

| 情况 | `exit_code` | `signal` | `timed_out` |
|---|---|---|---|
| 正常退出 | 进程退出码 | `None` | `False` |
| 被信号杀死 | `None` | signum（如 `SIGTERM=15`） | `False` |
| 超时 | `None` | 可能是我们发的 `SIGKILL=9`，也可能 `None` | `True` |

超时**不伪装成退出码 1**；被信号杀死也不报成"失败退出码"。

## 超时与杀树

- POSIX：`Popen(start_new_session=True)`，超时后 `os.killpg(SIGKILL)` 杀整个
  进程组（子进程是组长；它的孙进程默认同组）。
- Windows：零依赖拿不到 Job Object → `supports_process_group=False`，只
  `kill()` 直接子进程，`ExecResult.descendants_may_survive=True`。
  **孙进程可能存活**，文档和结果里都不假装杀了树。
- 超时后最多再等 5 秒收尸；读线程另有 5 秒收尾上限，超过就关读端。宁可
  少收一点输出，也不让调用方永久挂住。

## 有界 stdio（不能停读）

两条流各一个线程，按 64 KiB 分块读：

- 前 `max_output_bytes` 字节留在内存里（返回给调用方）。
- 超出后**继续读但不保留**：溢出的第一块开始时，把"已保留的头 + 后续内容"
  流式写进临时文件（`blob_dir` 里有就同目录，保证 `os.replace` 原子），
  读线程永远不停止读取。
- **为什么不能停读**：子进程写满管道缓冲区后会阻塞在 `write`，永远不退出，
  超时逻辑也就永远拿不到退出。测试用 2 MiB 输出 + 4 KiB 上限钉死这一点。
- 收尾：`truncated=True`；有 `blob_dir` 时把完整原文按 sha256 内容寻址放到
  `<blob_dir>/<sha256>` 并返回 `*_ref`；没有 `blob_dir` 时只报 `truncated`、
  `*_ref=None`（不假装有证据）。
- `stdout` / `stderr` 按 UTF-8 解码（非法字节替换）。上限按**字节**算，
  截断点可能切断多字节字符。
- PTY 模式下两条流合成一条：内容都在 `stdout`，`stderr=""`，
  `stderr_merged=True`；`stdout_ref` 记录的是这条合并流的完整原文。

落盘用的是流式临时文件 + `fsync` + `os.replace` + 目录 `fsync`，与
`fsutil.atomic_write_bytes` 同一套原子替换语义；区别是它不要求把完整输出
读进内存（那会让"有界"变成一句空话）。

## 明确没做（后续工单）

- 模型可见的 `run_command` 工具（ToolSpec / C4 动作 / Dispatcher 接线）。
- 异步句柄、取消 API、PTY 交互式写入（当前 PTY 只读，stdin 不写）。
- Seatbelt / Landlock / AppContainer / 容器。
- Windows Job Object：零依赖做不了，`supports_job_objects=False` 是如实声明。
