# `run_command`：把执行原语接成模型可见的工具（已实现）

关联：PRD §7 **F1**（执行原语，已实现 `src/tianshu/exec.py`）、**F2**（执行策略，
`src/tianshu/execpolicy.py`）、§8.3（工具级默认授权）。

## 一句话

模型终于能执行命令了，但**默认一条都跑不了**：任务不显式开 exec 策略，
`run_command` 连审批都不会发起；开了策略也只是"允许申请"，每次执行仍需人工审批。

## 为什么默认必须全拒

默认放行 `python` / `bash` 是最难被发现的越权：用户以为自己在"只读整理文件"，
实际上模型能执行任意代码、读任意路径、联网。默认安全只能靠 **deny by default**：

- 任务没有 `ExecPolicy` → C4 判定 `c4.exec_not_configured`（拒绝，不是"工具不存在"）；
- 有 `ExecPolicy` 但 argv 不在白名单 / cwd 越界 / pty 未许可 → `c4.exec_policy_denied`；
- 后端做不到请求的方式（如 Windows 上请求 PTY）→ `c4.exec_unsupported`（改策略无用）；
- 全部通过 → `c4.exec`：**需要人工审批**。

## 开启 exec 的入口（显式动作）

CLI（推荐）：

```
tianshu task create --goal "修测试" --workspace /path/to/repo \
  --allow-exec-cmd /usr/bin/python3 --allow-exec-cmd /usr/bin/pytest
```

- `--allow-exec-cmd` 可重复，必须是**绝对路径**；不传就是全拒。
- cwd 限制自动取本任务的**可写**绑定；env 只放行 `PATH`（凭据不进子进程）；
  默认 `timeout_ms=30_000`、`max_output_bytes=1 MiB`。
- `tianshu run --task ... --workspace ... --allow-exec-cmd ...` 兼容入口同样支持。

程序化：

```python
policy = ExecPolicy(
    argv_allowlist=("/usr/bin/python3",),
    cwd_roots=(workspace_dir,),
    env_allowlist=("PATH",),
    timeout_ms=30_000,
    max_output_bytes=1024 * 1024,
)
task = stack.tasks.create_task(goal, [str(workspace_dir)], exec_policy=policy)
```

策略存进 `tasks.exec_policy`（JSON），审批绑定的 `preconditions` 里有
`exec_configured` 标记——任务有没有开 exec，是动作摘要的一部分。

## C2 工具

`run_command`（`exposed=True`，`side_effect_level="non_idempotent"`，`exec_model="E3"`）：

| 参数 | 类型 | 说明 |
|---|---|---|
| `argv` | `array[string]`（必填） | 可执行文件 + 参数。**字符串命令直接拒绝**，不经过 shell（全仓库没有 `shell=True`）。 |
| `cwd` | `string`（可选） | 相对 workspace 的目录；缺省为 workspace 根。必须是任务可写绑定的子目录。 |
| `pty` | `boolean`（可选） | 本次调用是否请求 PTY。默认 `false`；`allow_pty` 只是策略许可，不会自动启用。 |

## 结果（`tool.result.output`）

必须具备并可审计：

- `exit_code` / `signal` / `timed_out`：正常退出、被信号杀、超时是三种不同状态；
- `stdout` / `stderr` / `stderr_merged`（PTY 下 stderr 合并进 stdout，不可分离）；
- `truncated` / `stdout_ref` / `stderr_ref`：截断后的完整原文按内容寻址落在
  `artifacts/blobs/<sha256>`；没有 blob 目录时只报 `truncated`、不伪造 ref；
- `duration_ms`；
- `policy`：本次 exec 策略的判定结果（allowed 时记 argv0/pty；denied 时记
  F2 的 `rule_id`）。C4 的判定结果在 `dispatch.requested` 事件的 `rule_id` 里。

## 凭据（Receipt）

命令没有"文件写后哈希"：exec 凭据的 `before_state="unknown"`、
`after_state="exec:<digest>"`（digest 覆盖 argv/退出码/信号/输出），
`resource` 是命令的 cwd（绝对路径，必须落在记录的 workspace 根内）。
`verify_receipts` 对 exec 凭据**核对事件引用与结果字段，但不比较磁盘**——
磁盘上哪些文件被改过，命令自己没说，我们不假装知道。

## PTY 的平台状态

- **POSIX（macOS / Linux）**：标准库 `pty.openpty()` 可用。
- **Windows**：标准库没有 ConPTY → 一律 `PolicyUnsupported`（`c4.exec_unsupported`），
  **不会用管道假装 PTY**。当前仓库 CI 只有 macOS + Ubuntu，Windows 行为尚未实测。

## 明确没做

- 交互式 stdin / 长驻会话 / 取消 API（当前是同步执行到结束或超时）。
- 不做 shell 解析、不做命令字符串拆分。
- 不做 Seatbelt / Landlock / 容器；`LocalExecBackend.isolation="none"`，
  argv/cwd/env 是策略约束，不是隔离。
