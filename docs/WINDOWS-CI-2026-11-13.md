# Windows CI 修复证据 · 2026-11-13

初始基线：`main@2100fde`；验证前已整合 `main@3648d79`。修复：[PR #130](https://github.com/lorek666/tianshu-agent/pull/130)。本轮使用 GitHub 托管 Windows runner，不是 Windows 10/11 用户设备实测。

## 1. 原始失败与根因

[历史完整运行 43239428326](https://github.com/lorek666/tianshu-agent/actions/runs/43239428326) 的 Windows job `131255500928`：**56 failed / 2030 passed / 29 skipped**，1014.59 秒。该运行来自较早的 PR 提交，不作为本次 main 的测试总数。

[main 运行 43248429983](https://github.com/lorek666/tianshu-agent/actions/runs/43248429983) 的 Windows job 在 pytest 约 88% 时触及 25 分钟上限；输出已有多条失败，但没有完整汇总。不能将超时全归因于单一问题。

| 历史失败分类 | 数量 | 修复 |
|---|---:|---|
| MCP 假服务端输出中文时按 cp1252 编码崩溃，后续测试缺少已准入工具 | 43 | 夹具标准输入/输出显式 UTF-8；生产 MCP 收发已显式 UTF-8 |
| authority 子进程打印中文 | 2 | 子进程显式 UTF-8，与父进程解码一致 |
| exec 测试使用不存在的 `/tmp` | 4 | 使用 `tmp_path`，不改安全断言 |
| isolation 契约使用非 Windows 绝对路径、把 Windows 当异平台 | 4 | 使用平台有效目录，动态选取另一平台 |
| IPC 密钥认证、POSIX 残留 socket 文件用例 | 2 | 密钥二进制写入；恢复被插入用例错接的平台条件 |
| memory URI 写死 POSIX `%2F` | 1 | 断言平台分隔符编码及 URI 回环，仍禁止裸分隔符 |

## 2. IPC 的真实红证据

诊断提交 `cdef85f` 只增加回归和测试/CI 修正，尚未修改 `load_authkey()`。

[Windows 运行 43281774439](https://github.com/lorek666/tianshu-agent/actions/runs/43281774439)，job `131404619910`：`test_authkey_preserves_binary_bytes_on_disk` **1 failed in 0.57s**。失败发生在真实 `Path.read_bytes()` 与固定生成值的比较：预期开头 `LF CR LF`，磁盘实际为 `CR LF CR CR LF`。没有替换文件 I/O。

`os.open` 未指定 `O_BINARY`，Windows CRT 默认文本模式将 `os.write` 的 LF 扩成 CRLF。创建者返回原始随机 key，后续进程读取磁盘得到不同 key；这不是靠重新连接能修复的偶发网络错误。实现只对新建密钥加 `getattr(os, "O_BINARY", 0)`。

回归还覆盖：首次建服务端后由真实子进程读取 key 并认证/echo；已有含 CRLF、长度大于 32 的历史 key 保持原值，不进行猜测性修复或轮转。

## 3. MCP 夹具的红绿证据

`test_stdio_protocol_is_utf8_under_non_utf8_process_default` 启动真实假服务端进程并设置 `PYTHONIOENCODING=cp1252`。修复前本机 **1 failed in 0.09s**，在 `tools/list` 处 EOF；假服务端因中文描述触发 `UnicodeEncodeError`。修复夹具编码后通过，且校验中文工具描述及 `中文与 café` 双向调用。

## 4. 验证状态

- 本机相关回归：**267 passed in 11.70s**，覆盖 IPC、authority、exec、isolation、resources 与 MCP。
- 本机初始全量：**1 failed / 2107 passed / 7 skipped**，382.65 秒。唯一失败是终端已有 `ANTHROPIC_BASE_URL` 影响 `test_official_endpoint_is_priced`；去除变量后该用例通过。未改计价实现或放宽断言。
- 锁文件检查、Ruff、diff 空白检查通过。
- 整合提交 **`4bb1781`** 的 [CI run 43283015540](https://github.com/lorek666/tianshu-agent/actions/runs/43283015540) **四个 job 全 success**：
  - Windows job `131409199401`：**2127 passed / 30 skipped**，1393.12 秒，Windows Server 2025 / `windows-2025-vs2026`；固定密钥落盘与真实子进程认证均 PASSED。
  - macOS job `131409199202`：**2150 passed / 7 skipped**，569.34 秒。
  - Ubuntu job `131409199622`：**2149 passed / 8 skipped / 3 warnings**，269.43 秒。
  - UI job `131409198854`：类型检查、测试、构建、产物漂移检查通过。
- 本地整合后全量：在移除 `ANTHROPIC_BASE_URL` / `TIANSHU_BASE_URL` 的子命令环境中执行 `uv run --frozen pytest`，**2150 passed / 7 skipped**，386.97 秒；没有修改用户环境变量或产品默认行为。
- 独立复审确认：真实文件与子进程回归有效、旧 key 的字节与 mtime 不变、认证未放宽、合并未夹带其他实现。最终日志另由 verifier 核对。
- 后续证据回填提交仅修改文档；上面的结果绑定已验证实现提交，不冒称为后续提交的新运行结果。

Windows pytest 保留失败语义，增加 `-vv --tb=short --durations=20 -o faulthandler_timeout=60`，便于保留当前用例和长等待线程栈。没有 `continue-on-error`，没有延长作业上限。

## 5. 范围与限制

Windows 新增的跳过只针对 POSIX 残留 socket 文件；Windows 命名管道的连接、认证、关闭及重复实例拒绝仍执行。现有平台跳过须继续通过 `-rs` 暴露原因。

本 PR 不修改分支保护、审批契约或历史密钥，不验证 Windows 桌面安装包、签名、托盘、杀毒软件兼容或 ARM64 用户设备。issue #50 旧记录中「生产不受影响」「必须实机才能定位」的判断已由本轮证据推翻。

Windows 全量用时 23 分 13 秒，距离 25 分钟 job 上限较近。耗时表中两个 isolation fixture 各约 120 秒，仍需后续性能维护；本轮没有用加长超时掩盖问题，单次全绿也不等于长期稳定性证明。
