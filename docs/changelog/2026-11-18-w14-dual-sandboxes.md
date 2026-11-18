# 2026-11-18 · W14 双系统进程级沙箱后端（macOS Seatbelt + Linux Landlock）

## 1. 背景与目标

对照 [CONTRACT-PHASE2-G3-2026-11-18.md](../CONTRACT-PHASE2-G3-2026-11-18.md) §4 与 [F2-sandbox-policy.md](../F2-sandbox-policy.md) §8，在不引入任何第三方运行时依赖、保持内核默认 `DEFAULT_EXEC_BACKEND = "local"` 不变、且严守「目标沙箱不可用时一律 `PolicyUnsupported`、绝不静默降级回 `local`」红线的前提下，落地 macOS Darwin 与现代 Linux 原生进程级沙箱后端并打通审批前实测视图。

## 2. 关键实现与踩坑决策

1. **`isolation.PROBE_VERSION = "v2"` 与 `_probe_process_tree` 探针工作区自清理（`src/tianshu/isolation.py`）**：
   - 原 `v1` 进程树探针把 `grandchild.pid` 写往 `data_root` 下的 `probe_dir`；真实进程沙箱按策略封锁工作区外 `data_root`，导致孙进程 PID 文件因 `PermissionError` 无法落盘而被误判为 `unknown`。
   - 改为在受授权 `workspace` 下创建每次探测唯一的临时子目录存放 `grandchild.pid`，并在 `finally:` 中递归清理；同步将 `PROBE_VERSION` 升至 `"v2"` 自动淘汰旧版缓存。
2. **macOS `SeatbeltExecBackend`（`src/tianshu/exec_seatbelt.py`）**：
   - 仅在 `sys.platform == "darwin"` 且 `/usr/bin/sandbox-exec` 可执行时注册 `"seatbelt"`（其他平台保持 `UNIMPLEMENTED_EXEC_BACKENDS`）。
   - 动态生成 `(version 1) (deny default)` SBPL：在现代 macOS（含 Cryptexes 共享缓存与 `uv` 托管 Python 前缀 `sys.base_prefix` / `sys.prefix`）精确赋 `file-read* file-map-executable`，仅对规范化 `policy.cwd_roots` 与每进程私有 `TMPDIR` 赋读写权限；使用 `(target same-sandbox)` 允许沙箱内子进程信号管理同时阻断向宿主进程发信号；末尾显式追加 `(deny network*)`、封锁 `com.apple.securityd*` Mach 服务及 `~/Library/Keychains`、`~/.ssh`、`~/.aws`、`~/.tianshu` 等敏感目录。
3. **Linux `LandlockExecBackend`（`src/tianshu/exec_landlock.py`）**：
   - 纯标准库 `ctypes` 直调 `x86_64` / `aarch64` Linux 系统调用 `444`（`create_ruleset`）、`445`（`add_rule`）、`446`（`restrict_self`）与 `prctl(PR_SET_NO_NEW_PRIVS, 1)`。
   - `LandlockPathBeneathAttr` 强制 `_pack_ = 1`（模块导入期断言 `sizeof == 12`）；ABI `1..3` 传 `attr_size = 8` 且如实声明 `isolation = "process_tree"` / `supports_network_isolation = False`；ABI `>= 4` 传 `attr_size = 16` 与 `handled_access_net = 0x3`（零放行端口规则，阻断全部 TCP bind/connect），声明 `isolation = "app_container"` / `supports_network_isolation = True`。
   - 按 `stat.S_ISDIR(os.fstat(fd).st_mode)` 严格剥离非目录文件/字符设备（如 `/dev/null`、`argv[0]`）上的目录专属位，防止内核返回 `EINVAL`；子进程 `preexec_fn` 任何系统调用或必需路径失败立即 `os._exit(126)`（fail-closed，零裸跑窗口）。
4. **后端级强制凭据环境清洗与审批前实测接线（`exec.py` / `exec_backends.py` / `dispatcher.py` / `tools.py`）**：
   - `scrub_sandbox_env()` 在 `seatbelt` 与 `landlock` 内部独立于 `ExecPolicy.env_allowlist` 强制剥离 `TIANSHU_*`（含 `TIANSHU_ISOLATION_SENTINEL`）、`*_API_KEY`、`*_SECRET*`、`*_TOKEN*`、`*_PASSWORD*`、`AWS_*`、`OPENAI_*`、`ANTHROPIC_*`、`SSH_AUTH_SOCK`、`DBUS_SESSION_BUS_ADDRESS` 等敏感环境变量。
   - 新增 `TIANSHU_EXEC_BACKEND=auto` 与 `preferred_exec_backend()`；无原生沙箱平台显式请求 `auto` 直接抛 `PolicyUnsupported(backend="auto", feature="exec_backend")`。
   - 当 `backend.capabilities.isolation != "none"` 且 `data_root is not None` 时，即使 `policy.isolation_requires` 为空，`dispatcher._precheck_exec` 与 `tools._run_command` 也通过 `probe_backend_cached` 填入绑定现场的实测 `IsolationReport`，使审批前 `ApprovalView.isolation` 直接展示实测等级与四边界状态（`local` 默认路径维持 0ms 快路径不变）。

## 3. 本机 macOS 实测输出与验证命令

本机（`macos-arm64`，Darwin 25.6.0，Python 3.12.13）实跑 `probe_backend(SEATBELT_EXEC_BACKEND)` 原始结果：
- `files_authority`: `enforced`（工作区外读写与权威 SQLite `BEGIN IMMEDIATE` 均 `FAIL`）
- `network`: `enforced`（本机 loopback TCP `connect` `FAIL`）
- `credentials`: `enforced`（宿主环境哨兵 `SENTINEL=FAIL`）
- `process_tree`: `enforced`（超时进程组杀树后孙进程消失，`descendants_may_survive=False`）
- `measured_level`: `"app_container"`，`unverified=False`，`contradicted=False`

复核命令：
```bash
uv lock --check
uv run --frozen ruff check .
uv run --frozen pytest -o addopts="" -q tests/test_w14_os_sandboxes.py tests/test_exec.py tests/test_exec_backends.py tests/test_execpolicy.py tests/test_isolation.py tests/test_isolation_gate.py tests/test_rule_ids.py tests/test_windows_compat.py
```

## 4. 已知边界（诚实声明）

- Windows 尚无原生进程级沙箱后端（`app_container` 与 `container` 维持诚实 `PolicyUnsupported`）；Linux 内核实测覆盖由 GitHub Actions `ubuntu-latest` 原生内核承担，旧于 5.13 或禁用 Landlock 的 Linux 内核如实拒绝。
