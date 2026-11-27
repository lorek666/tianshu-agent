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

## 执行侧后续修复

- 显式 Job 命令要通过通用与 detached/breakaway 两个探针才会请求审批。审批记录保存实际后端及执行对象摘要；消费和启动前各核一次。换后端、同路径脚本内容变化或缺摘要均拒绝，其他后端沿用既有审批规则。
- 业务命令等待期间观察 Run 控制、认领与工作区租约。取消、已有的 Run fence 或认领失效都触发清树。公开目标修订仍与执行共享守卫；原生用例在 Job 运行时从独立子进程驱动真实的 `run.fenced` 转移，不把改目标已绕过守卫当作验收。
- 终态由 Job 查询和精确 leader 句柄的 FILETIME 共同绑定。归并核对 Run、Task、subject、PID 和出生身份。未知终态及工作区阻断同事务记录，工具结果明示 `descendants_may_survive`。人工解除普通目录阻断并不能证明旧 Job 已清空，仍禁止自动重启该 Job；无有效终态证据时不根据 PID 重放。
- 未知终态的 `recovery.blocked` 同时登记用户级权威根，换用另一 `--db` 仍拒绝同工作区写入；权威根写入失败会在结果中明确报告跨数据根阻断未生效。Goal V2 与执行恢复共用 Run、Task、subject、PID 和出生身份核对，身份不符的 `execution.ended` 不会推进阶段。
- Win32 包装保留 `HeapFree` 的失败结果，并按 `VOID` 声明属性列表删除函数；当 `STILL_ACTIVE` 与 leader signal 发生竞态时重新读取退出码。关闭失败保留原句柄以便有限重试。

## 共享执行收尾修复

- POSIX 超时清理在等待 leader 后继续用原 PGID 观察进程组；发过信号但组仍存在或无法观察时为 `unknown`，不再把 `killpg` 成功当作整树退出。
- 管道初始化失败也进入清树与流关闭路径。读线程被强制停止时不得冒充自然 EOF；无法在收尾上限内停止时抛错，拒绝并发 `finish()`/`feed()`。
- PTY master fd 在外层 `finally` 关闭，覆盖创建、读取和收尾异常。

## 前审冻结树被拒绝与根因修复

对 pending tree `8635c166b3054087cf9dd4ea5b6355bfc82e7537` 的独立复审结论为“部分修，建议阻断合入”；当时已启动的全量随即停止，不作为通过证据。复审发现四项问题，本轮逐项修复并新增负控：

1. **通用 Windows 探针仍可能按复用 PID 清理**：根进程现在通过 `DuplicateHandle` 把孙进程精确 HANDLE 交给父进程，文件记录 PID + 原始 FILETIME；父进程核验身份后用 `WaitForSingleObject` 观察、精确 `TerminateProcess` 并关闭句柄。身份不符、句柄不可核验或清理未确认均为 `unknown`，不再调用 Windows bare-PID `os.kill`。`PROBE_VERSION` 升至 `v4`，旧探针缓存失效。
2. **Admission 探针结论合成不严**：detached `enforced` 只能把通用 `unknown` 升级为 `enforced`，不得覆盖通用 `not_enforced`；detached 的负向或未知结果继续按保守优先级阻断。
3. **MCP 关闭协议与 `ManagedProcess` 不兼容**：execution 分支只调用 `PreparedExecution.__exit__()` 并关闭 stdin，不再执行 `poll()/terminate()/kill()/wait()`；普通 `Popen` 分支保持原协议。两条路径都 join 读线程并唤醒等待者。
4. **失败句柄重试没有进入生产路径**：`JobController.close()` 对每个未关闭 HANDLE 有界重试；持续失败保留 `_open_handles` 和 controller 可重试状态。backend `finally` 与 MCP 关闭都经过同一自动重试路径，close 非 `exited` 时结果标记 `descendants_may_survive=true`。

本轮新增真实子进程参数回归、精确 HANDLE 身份/清理故障注入、Admission 七组合矩阵、MCP 等待者唤醒和 backend/MCP 首次关闭失败负控。Windows 原生用例另覆盖 local 与 Job 后端的通用精确句柄探针，以及真实 MCP stdio + Job 的关闭、线程 join 与响应唤醒；本机 macOS 只执行同形控制流，不冒充原生证据。

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
17. detached 结论无条件覆盖通用进程树结论：通用 `not_enforced` 被洗成 `enforced`，准入测试未抛错。
18. MCP execution 关闭分支重新调用 `poll()`：`ManagedProcess` 形状负控直接触发禁止 Popen 清理。
19. 关闭生产路径自动重试：backend 首次失败后只关闭一次，MCP 路径的 controller 保持未关闭。
20. 身份不符后重新按裸 PID 清理：精确 HANDLE 负控实际记录到 PID kill 调用。

前一轮最终代码复审另修正了原生整数句柄、259 退出码消歧、句柄关闭重试、管道所有权转移、双读线程收尾、query 失败仍终止与收集异常必关 Job；探针清理进一步取消“先采样 PID、再按 PID 终止”的竞态。本轮四项新修复的变异均实际 RED，恢复后 GREEN；定向套件与本机静态门禁另列实时结果。

本机裸全量、最终独立第二读者结论与 Windows required CI 结果将在取得后回填；不得用本机 fake 测试、macOS 跳过或历史 CI 代替 Windows 真子进程证据。

## 未决边界

- Windows required CI 尚须覆盖真实 stdio、wait/returncode、多代与 detached 后代、leader 先退、breakaway 拒绝、父 Job handle 关闭、Admission 探针、通用/detached 精确 HANDLE 清理、MCP + Job 关闭与首次 `CloseHandle` 失败自动重试。
- Windows 10/11、ARM64、文件系统、EDR、桌面会话与 SKU 差异仍无用户设备证据。
- Job Object 不防御同 UID 宿主、内核攻击或 Job 外进程，不提供文件/网络/凭据隔离。
- Issue #224 保持开放，完整四边界与各架构实机验收未完成。
