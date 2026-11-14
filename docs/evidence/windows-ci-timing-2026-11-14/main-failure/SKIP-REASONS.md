# 本次main原生CI实际跳过原因

保留原始日志行号、分组计数和理由；仅解码Unicode转义便于阅读。原始日志与facts保留原内容。

## test (ubuntu-latest) · py3.12：13 skipped

- [1] `tests/test_backend_contract.py:386`：memfs 没有符号链接，不承诺这条（job日志L235）
- [2] `tests/test_backup_materials.py:592`：当前平台没有 /tmp 的系统路径别名（job日志L236）
- [2] `tests/test_data_cli.py:118`：验证 macOS /tmp → /private/tmp 系统别名（job日志L237）
- [1] `tests/test_exec.py:525`：仅 Windows：本机不可执行（job日志L238）
- [1] `tests/test_exec.py:540`：仅 Windows：本机不可执行（job日志L239）
- [1] `tests/test_host_cleanup.py:514`：真实无 FILE_SHARE_READ 句柄只适用于 Windows（job日志L240）
- [1] `tests/test_ipc.py:612`：AF_UNIX 队列满时'立即拒绝'是 macOS 语义；Linux 的 backlog 只是提示，同样连接会排队。重试逻辑见 test_probe_result_retries_transient_errors_deterministically（job日志L241）
- [1] `tests/test_powercut_image.py:223`：真实摘卷实验是 opt-in：设 TIANSHU_POWERCUT=1 才跑（CI 默认 skip）（job日志L242）
- [2] `tests/test_powercut_image.py:269`：真实摘卷实验是 opt-in：设 TIANSHU_POWERCUT=1 才跑（CI 默认 skip）（job日志L243）
- [1] `tests/test_soak.py:186`：浸泡测试默认跳过：TIANSHU_SOAK=1 才跑（有界、按 TIANSHU_SOAK_SECONDS 秒）（job日志L244）

## test (macos-latest) · py3.12：8 skipped

- [1] `tests/test_backend_contract.py:386`：memfs 没有符号链接，不承诺这条（job日志L236）
- [1] `tests/test_exec.py:525`：仅 Windows：本机不可执行（job日志L237）
- [1] `tests/test_exec.py:540`：仅 Windows：本机不可执行（job日志L238）
- [1] `tests/test_host_cleanup.py:514`：真实无 FILE_SHARE_READ 句柄只适用于 Windows（job日志L239）
- [1] `tests/test_powercut_image.py:223`：真实摘卷实验是 opt-in：设 TIANSHU_POWERCUT=1 才跑（CI 默认 skip）（job日志L240）
- [2] `tests/test_powercut_image.py:269`：真实摘卷实验是 opt-in：设 TIANSHU_POWERCUT=1 才跑（CI 默认 skip）（job日志L241）
- [1] `tests/test_soak.py:186`：浸泡测试默认跳过：TIANSHU_SOAK=1 才跑（有界、按 TIANSHU_SOAK_SECONDS 秒）（job日志L242）

## windows shard 1 · py3.12：17 skipped

- [1] `tests/test_backend_contract.py:386`：memfs 没有符号链接，不承诺这条（job日志L1850）
- [2] `tests/test_backup_materials.py:592`：当前平台没有 /tmp 的系统路径别名（job日志L1851）
- [1] `tests/test_daemon.py:911`：Windows 命名管道没有'只 listen 不握手'的裸句柄（见 tests/support.HangupPeer）（job日志L1852）
- [1] `tests/test_daemon.py:964`：Windows 上 os.kill 的 SIGTERM 不是优雅退出路径（job日志L1853）
- [1] `tests/test_host_cleanup.py:500`：chmod 000 读权限语义只适用于 POSIX（job日志L1854）
- [1] `tests/test_ipc.py:214`：Windows 端点不是文件路径，没有长度上限（job日志L1855）
- [1] `tests/test_ipc.py:405`：Windows 命名管道没有'只 listen 不握手'的裸句柄（见 tests/support.HangupPeer）（job日志L1856）
- [1] `tests/test_ipc.py:500`：Unix socket 的监听队列；Windows 走命名管道（job日志L1857）
- [1] `tests/test_ipc.py:526`：命名管道没有 ENOENT/ECONNREFUSED 分层（job日志L1858）
- [1] `tests/test_ipc.py:550`：重试语义只在 POSIX 探活实现上成立（job日志L1859）
- [1] `tests/test_ipc.py:601`：Unix socket 的排队语义；Windows 走命名管道（job日志L1860）
- [1] `tests/test_ipc.py:721`：残留 socket 文件是 POSIX 概念；命名管道不是文件（job日志L1861）
- [1] `tests/test_ipc_send_close.py:18`：POSIX Connection._send 短写循环；Windows PipeConnection 使用不同的发送实现（job日志L1862）
- [1] `tests/test_isolation.py:200`：进程组杀树只有 POSIX 能确认（job日志L1863）
- [1] `tests/test_leases.py:238`：PID 存活探测仅 POSIX 实现（job日志L1864）
- [1] `tests/test_soak.py:186`：浸泡测试默认跳过：TIANSHU_SOAK=1 才跑（有界、按 TIANSHU_SOAK_SECONDS 秒）（job日志L1865）

## windows shard 0 · py3.12：20 skipped

- [1] `tests/test_backup.py:566`：POSIX 语义（job日志L1999）
- [2] `tests/test_data_cli.py:118`：验证 macOS /tmp → /private/tmp 系统别名（job日志L2000）
- [1] `tests/test_exec.py:196`：Windows 没有进程组/信号，见 Windows 专属用例（job日志L2001）
- [1] `tests/test_exec.py:467`：Windows 标准库没有 PTY（job日志L2002）
- [1] `tests/test_exec.py:484`：Windows 标准库没有 PTY（job日志L2003）
- [1] `tests/test_exec.py:502`：Windows 没有 Unix 信号（job日志L2004）
- [1] `tests/test_integration_m5.py:676`：SIGKILL / AF_UNIX 语义；Windows 用 taskkill，本测试不覆盖（job日志L2005）
- [1] `tests/test_maintenance.py:272`：POSIX uid; Windows inherits OS user-directory ACL（job日志L2006）
- [1] `tests/test_powercut_image.py:223`：真实摘卷实验是 opt-in：设 TIANSHU_POWERCUT=1 才跑（CI 默认 skip）（job日志L2007）
- [2] `tests/test_powercut_image.py:269`：真实摘卷实验是 opt-in：设 TIANSHU_POWERCUT=1 才跑（CI 默认 skip）（job日志L2008）
- [1] `tests/test_sandbox_backend.py:57`：Windows 没有 POSIX 权限位语义（chmod 只能切只读位，S_IMODE 对可写文件恒为 0o666）（job日志L2009）
- [1] `tests/test_sandbox_backend.py:70`：Windows 没有 POSIX 权限位语义（chmod 只能切只读位，S_IMODE 对可写文件恒为 0o666）（job日志L2010）
- [1] `tests/test_takeover.py:179`：SIGKILL 与 PID 存活探测仅 POSIX 实现（Windows 无对应语义）（job日志L2011）
- [1] `tests/test_takeover.py:220`：SIGKILL 与 PID 存活探测仅 POSIX 实现（Windows 无对应语义）（job日志L2012）
- [1] `tests/test_takeover.py:257`：SIGKILL 与 PID 存活探测仅 POSIX 实现（Windows 无对应语义）（job日志L2013）
- [1] `tests/test_takeover.py:280`：SIGKILL 与 PID 存活探测仅 POSIX 实现（Windows 无对应语义）（job日志L2014）
- [1] `tests/test_takeover.py:297`：SIGKILL 与 PID 存活探测仅 POSIX 实现（Windows 无对应语义）（job日志L2015）
- [1] `tests/test_takeover.py:352`：SIGKILL 与 PID 存活探测仅 POSIX 实现（Windows 无对应语义）（job日志L2016）

