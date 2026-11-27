# Windows Job Object 进程树切片（2026-11-28）

## 背景与范围

Issue [#224](https://github.com/lorek666/tianshu-agent/issues/224) 的 Windows 进程树边界尚未交付。契约已由 PR [#256](https://github.com/lorek666/tianshu-agent/pull/256) 单独冻结；本候选只实现显式 `windows_job_object` 后端，不改变默认 `local`/`auto`，不宣称文件、网络或凭据隔离，也不关闭 #224 的整体验收。

重放冻结起点：`acd2df00250d2094ac254dabba380881bf338b64`，tree `64774b4cc3fce4d1a07f30e42c231993ab287ca5`。分支已重放到 `origin/main` `b5bf2b8f63a285f2e1344b386699201ed868a90c`，不包含 native 工作树提交；后续复审修复与最终提交另绑定，不把该起点当作验收树。

## 实现决策

- 使用 `CREATE_SUSPENDED` 与 `PROC_THREAD_ATTRIBUTE_JOB_LIST` 在创建时原子绑定 Job；显式传入应用程序路径，避免命令名解析歧义。
- Job handle 不可继承，启用 `KILL_ON_JOB_CLOSE` 并禁止 breakaway。stdio 通过 handle list 只继承三条子进程管道。
- `ResumeThread` 前用 `IsProcessInJob` 复核精确 Job。绑定复核失败时，除终止 Job 外还按精确进程句柄终止 suspended leader；各清理步骤独立 best-effort。
- leader 正常退出也必须查询 Job；活跃进程归零才可确认 `exited`。查询失败、仍有进程或收尾不确定时保持 `unknown`，设置 `descendants_may_survive=true`。
- Admission 使用同一原生创建路径，不回落 `Popen`；Windows 探针覆盖普通 `DETACHED_PROCESS` 后代与 breakaway 创建拒绝，通过 `DuplicateHandle` 取得精确句柄，并按 PID + 原始 `FILETIME` 出生身份核验、等待与清理。
- 已知后端首次注册与替换都绑定名字、隔离级别及完整能力承诺。`isolation` 保持 `none`，`supports_job_objects=True` 不作为隔离证据。

## 共享执行收尾修复

- POSIX 超时清理在等待 leader 后继续用原 PGID 观察进程组；发过信号但组仍存在或无法观察时为 `unknown`，不再把 `killpg` 成功当作整树退出。
- 管道初始化失败也进入清树与流关闭路径。读线程被强制停止时不得冒充自然 EOF；无法在收尾上限内停止时抛错，拒绝并发 `finish()`/`feed()`。
- PTY master fd 在外层 `finally` 关闭，覆盖创建、读取和收尾异常。

## 验证证据

定向命令：

```bash
uv run --frozen pytest -q \
  tests/test_exec.py \
  tests/test_exec_backends.py \
  tests/test_exec_job_object.py \
  tests/test_execution_admission.py \
  tests/test_isolation_gate.py \
  tests/test_windows_compat.py::test_product_never_uses_text_mode_file_io
```

结果：通过；Windows 原生用例在本机 macOS 按预期跳过。`uv lock --check` 与 `uv run --frozen ruff check .` 通过。

有效变异负控均观察到预期 RED，恢复后定向转绿：

1. 关闭 Job 能力校验：首次注册测试未抛错。
2. 关闭已知后端名字校验：替身注册测试未抛错。
3. 进程组仍存在时直接报 `exited`：树状态断言变红。
4. 初始化失败后不关流：三条流关闭断言变红。
5. 强制停止读线程仍报自然完成：held-pipe 树状态断言变红。
6. 关闭 Admission 原生 Job 分支：触发禁止回落 `Popen`。
7. 关闭精确 `TerminateProcess`：suspended leader 清理断言变红。
8. 原生 HANDLE 返回值改回 `.value`：直接复现 `AttributeError`。
9. 探针忽略 `tree_state`/`descendants_may_survive`：unknown 被误报为 `enforced`。
10. 跳过精确句柄 PID/birth 核验：stale-birth 负控触发身份不符保护。
11. `CloseHandle` 失败后锁死 controller：两个重试用例变红。
12. query 失败后跳过 Job 终止：终止调用断言变红。
13. 管道 `open_osfhandle` 后延迟转移所有权：失败路径仍保留 raw HANDLE。
14. 第二个 pump 尚未 append 就启动：首个已启动线程未被 join。
15. 收集异常时跳过 `proc.close()`：Job 关闭断言变红。
16. 把 signaled leader 的合法退出码 259 当作仍在运行：`poll()` 错误返回 `None`。

最终代码复审另修正了原生整数句柄、259 退出码消歧、句柄关闭重试、管道所有权转移、双读线程收尾、query 失败仍终止与收集异常必关 Job；探针清理进一步取消“先采样 PID、再按 PID 终止”的竞态。恢复后的定向套件与本机静态门禁通过。

本机裸全量、最终独立第二读者结论与 Windows required CI 结果将在取得后回填；不得用本机 fake 测试、macOS 跳过或历史 CI 代替 Windows 真子进程证据。

## 未决边界

- Windows required CI 尚须覆盖真实 stdio、wait/returncode、多代与 detached 后代、leader 先退、breakaway 拒绝、父 Job handle 关闭及 Admission 探针。
- Windows 10/11、ARM64、文件系统、EDR、桌面会话与 SKU 差异仍无用户设备证据。
- Job Object 不防御同 UID 宿主、内核攻击或 Job 外进程，不提供文件/网络/凭据隔离。
- Issue #224 保持开放，完整四边界与各架构实机验收未完成。
