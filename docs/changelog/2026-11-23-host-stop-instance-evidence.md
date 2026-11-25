# Host 停止与实例证据修复

状态：PR #228 的后续候选，未合入、未发布；#224 保持开放。

## 完整 CI 拒绝

v10b head `49a52a7703facbcec4879674d03ec173a25da289` 的完整 run
[45183420559](https://github.com/lorek666/tianshu-agent/actions/runs/45183420559)
失败：Windows runner 1 与 Windows required 汇总失败，其余 job 成功。
六份原始分片报告均完成选集；唯一测试失败为 SLO/soak smoke 的 soak 阶段，
报告 `orphan_pids=[9168]`。`temporary_soak_root_retained` 是发现残留后保留现场，
不是第二份泄漏证据。PID 9168 的真实现场原因仍不能确定。
全部 job 日志、经 GitHub digest 核验的四份 pytest ZIP 与六份报告保存在
忽略目录 `.omc/artifacts/native-process-isolation/`，不提交或覆盖，也不重跑旧 run 追绿。

## 已证实机制与修复

- 同一旧锁反复读取会把后来占用同 PID 的进程身份或不可读结果追加为旧 owner。
  真实活子进程及原生身份配合受控归属转换已复现该误报机制；不是现场 PID 复用证明。
  现在逻辑 owner 绑定 canonical 锁路径、PID、启动时间及 token/instance_id 的摘要，
  同 owner 只首次绑定身份；新 owner 或不同数据根可重新登记，未知初始身份仍保守。
  后续重读保留首末身份、来源与阶段，不替换原归属。
- 真实 macOS Host/Daemon 的退出屏障证明：Host 已退出、端点和锁已消失时，
  原 Daemon 仍可活过完整孤儿检测窗口。旧监督器发终止请求后没有最终 wait，
  并丢弃了仍活的 owned `Popen`。
  现在保留完整默认 5 秒优雅窗口，升级和最终核验共享额外 3 秒；
  同一个 owned 对象必须 wait 确认，未确认则保留对象。已回收对象不再发信号。
- 停止先禁止新 spawn，核对监控线程确已退出。Host 停止失败时保留原桥、锁和主循环，
  CLI 非零且可重试；重试保留最初锁快照，不把替代 owner 当作原目标。
  附着实例无 owned 句柄，升级后端点仍活则失败，不把 taskkill 超时当作成功。
- 可选 Host 诊断只在 SLO 显式启用时记录实际 wait、终止分类与退出码。
  stderr 写入失败不改变已确认退出。SLO 的 Host 与崩溃恢复 Daemon 均用原生临时文件
  接收 stderr，避免未读取 PIPE 或继承 PIPE 阻塞；只提取白名单字段，不转发原文、
  argv、token 或私有路径。孤儿失败保留终局 PID/身份、保留原因及来源/owner 的首末观察对应。

首次停止预审的提前终止与附着超时观察只有会话转录，原始版本源码未保留，
不能独立重放；stderr 异常当时仅静态检查。本轮另写正式回归并保存实际 RED，
不把转录或静态分析冒充原始运行证据。

## v11 拒绝与 v12 后续候选（2026-11-24）

v11 冻结 tree `c0898a8241e83397278e381c32733ddf8d5eb424`
被正式独立复审拒绝：真实附着 Host 首次停止读取 Daemon 锁发生一次 EIO，
CLI 与 Host 均返回成功、原桥和 Host 锁消失，但原 Daemon 仍持原锁并在线。
主控从固定树重新导出到新目录重放，命中同一命名断言；
探针最终回收 Host 与 Daemon。v11 自身裸全量主动中止，实际 exit 2，
本地总门禁 FAIL，不能复用为新候选的通过证据。

v12 将附着快照与同一认证连接的握手身份绑定；自有快照在启动就绪时
另核对锁 PID 与实际根进程。停止重试保持原快照，读锁错误或探活不明
保留原入口。只有明确 ENOENT 且端点不存在才确认附着端点消失。
认证和停止 RPC 放入受管原生辅助进程，分别受启动预算与剩余优雅预算约束；
超时回收辅助根，启动就绪失败也回收自有根。内置 Daemon 直接使用原生解释器，
不改写任意外部命令；旧根未确认退出时拒绝启动下一代。
这些约束不把 PID 查询扩大为任意后代归属证明。

POSIX marker-only 残留现在进入原来源台账；诊断中的 action/outcome
先检查字符串类型再匹配白名单，避免错误类型中断采集。真实子进程及屏障、
注入时钟的回归已保留 RED/绿原件，认证停滞及替代实例的错误路径单独取证。

v12 冻结的负控未通过：将停止目标改成当前锁的变异仍通过测试。
原测试在父进程替换请求函数，但真实请求已在辅助子进程执行；
短预算也可能在坏请求送达前超时。v13 只将测试守卫移到监督器的实际请求边界，
保留真实替代实例的存活、锁与端点断言；不修改产品实现，也不沿用 v12 验收。

v13 的最终变异负控全部按预期命中断言，但八文件受影响域实际退出 1：
`test_supervisor_shutdown_rejects_an_endpoint_that_ignores_the_request`
给辅助进程只留了 0.2 秒，负载下请求尚未送达便超时，因此不能断言已收到 shutdown。
同一测试单独复现退出 0，说明它是预算竞态而不是稳定红测。v14 只把该测试请求
预算调到 2 秒，产品实现不变；v13 的失败原件保留，v14 重新冻结并绑定后续证据。

v14 的 25 项最终变异负控和八文件受影响域通过，但两路独立第二读者各复现一个
P1，主控据此拒绝该冻结并中止其裸全量：Daemon 就绪后 UI 桥绑定失败只清 Host 锁，
遗留自有 Daemon 根、锁和端点；崩溃恢复 Daemon 仍以未读 `stderr=PIPE` 启动后直接
wait，写入 256 KiB 即可稳定复现阻塞。v15 在 Host 退出路径只回收仍由同一监督器
持有的 `Popen`，附着实例不受影响；恢复 Daemon 改用原生临时文件。但 v15 八文件
受影响域随后真实失败：较早 Host 发布失败时，后一个 Host 已接管共享 Daemon，
退出清理仍杀死了该共享根。v16 在回收前要求当前 `host.lock` 仍等于本实例快照；
无法确认本实例仍拥有 Host 身份时不发停止信号。两个 P1 红绿、v15 接管红绿及
v14/v15 拒绝原件均保留，不沿用被拒冻结的验收。

v16 的私有负控与受影响域通过后，主控静态复核发现身份读取和 owned 回收之间仍有
TOCTOU 窗口：真实双 Host 探针让次 Host 在旧 Host 停止前完成接管，随后共享 Daemon
被旧 Host 杀掉，次 Host 因无法确认实例身份而退出。v17 把身份核验与 owned 根回收
放入同一 Host 元数据临界区，次 Host 在旧根回收完成前不能接管；新增双 Host 屏障
回归在 v16 私有副本为红、v17 为绿。v16 裸全量在发现该反例后停止，不作最终验收。

## v26–v35 独立复审与停止竞态修复（2026-11-24）

v28 冻结被两路独立读者拒绝：`stop_monitor()` 未与 spawn 锁串行化，
监控恢复在一次存活检查后可能永久失监，SLO readiness 在 HTTP 返回前没有复核
进程出生身份与完整 Host 实例，冻结 manifest 的描述也无法按字节复算。
后续私有变异还依次暴露了 Popen 前二次停止检查、旧监控代等待后复核，
以及“先置停止事件再抢 spawn 锁”仍不足以约束 in-flight Popen 的顺序问题。
v29–v34 的逃逸与失败原件全部保留，没有用后续绿覆盖。

v35 将 `stop_monitor()` 固定为“先取得 spawn `_lock`，再置位 `_stop`，最后在
`_monitor_lock` 内递增代际并 join”；`_spawn_and_wait()` 在入口和 `Popen`
前各检查一次停止事件。监控恢复先递增代际并启动替代线程，旧代在等待前、
等待后和退避后均复核代际，不再依赖一次 `is_alive()` 猜测。
SLO readiness 在 HTTP 健康检查后再次核对原进程存活、OS birth identity 和完整
HostInfo；HTTP 期间早退立即保留 rc/stderr，启动与 `/state` 共享同一总截止。
Windows 仅改写当前 Python launcher，并保留调用方环境与项目导入路径；非 Python
命令原样返回。冻结清单改用 `git ls-tree -r -z --full-tree`，明确排除 `docs/`
及所有 `.md` 路径，并把 NUL 记录分隔符转为 LF 后计算 SHA256。

v35 首次生成凭证时脚本误沿用 `freeze-v34.json` 文件名，独立读者据此拒绝绑定；
主控保留误命名原件，生成正确命名的 `freeze-v35.json` 后重新绑定两路复审。
最终 Host 与 Process/SLO 限定复审均为 PASS，主控在新私有目录重放关键绿灯、
两项变异红测和 manifest 逐字节重算均通过。该结论只绑定下述非文档代码树，
不代表 Windows 原生沙箱、任意后代监管、hosted CI 或用户设备验收。

## v17 验证与边界（历史）

本机最终受影响 Host、SLO/soak、进程身份及集成回归退出 0；
30 项私有副本变异负控均实际触发预期断言失败，原源文件字节未变。
精确冻结自己的裸 `uv run --frozen pytest`、lock/Ruff 与两份 diff 检查退出 0，
正式限定独立第二读者及主控对原件核验和新目录重放均已通过。
验证环境为 `macOS-26.6.2-arm64-arm-64bit` / Python `3.12.13`；
不是 Windows 用户设备证明。历史红与后续证据分开保留，不以本机绿替代 required CI。

- 非文档清单 SHA256：`c19d77ea98b914da30c8e16f9ad22c9c8c325786a1e5a2b6a12be32b3855221c`。
- src 树：`dc8fd1cca5a8c4b5bf46da2772b5aa7b2489b395`；
  tests 树：`12092eb6f9ebfb89abfcc6a365092f40df2583e1`；
  scripts 树：`29cc59748e522048d87f8cc0d9a1e9e5608ae4f5`。
- 裸全量日志 SHA256：`9c6d25c7ae6e4d96e37c336270e540e9da091127a3dd950d43d20f3085545fa4`。
- 第二读者报告 SHA256：`ad3093909d1d40f362f5811bb57e828248727b8ca603331e664e9d2545e1b909`；
  主控复审验收回执 SHA256：`e24fffade375fae281aca475d2f24ed616ccc5e3ed3467dcfd46175ae773e8ec`。

## v35 当前候选验证与边界

- 冻结 index tree：`f8f54975b0ca593455c160c405a11ee124fe7172`；
  src 树：`317defbd5e35ad8f5295bdea1667ad6a11f4361c`；
  tests 树：`46abdfb0154b0b874380ab3f993edfc11518e69a`；
  scripts 树：`f3d26727678cdc28e2b2b293a8a071b55352bd14`。
- 非文档清单 SHA256：`c3908bf54edcd5b96a2d605be9a1a202f8ebe8edd2b9b45f652ac766f4014b03`；
  冻结凭证 SHA256：`a568c461957cb807eb26615a3ae2e10c46aba036b4a50692c96e49fffd68e899`。
- 完整私有变异负控 receipt SHA256：`c55f79282ee19af30b813ac62141e58391a99009f7f88daa7ba5a1dfc614d675`；
  八文件受影响域日志 SHA256：`12da52b716e1dc1ae153cc884fa12502ea55c988d6bec63eecfc944bbaca4625`。
- Host 绑定复审 SHA256：`7f5fb90cb700d021075e182213c5926e2d04458dfc9e4d8656a5b343b75a5331`；
  Process/SLO 绑定复审 SHA256：`a934daec6291e792d4f229df90d3124713989168ea7499d2c8e1a562292407ed`；
  主控私有重放回执 SHA256：`6100948547803b26a21fa11ff63507fdd73808b3f0903d72ee16fe8dcf3cadde`。
- 验证环境为 `macOS-26.6.2-arm64-arm-64bit` / Python 3.12.13；本机结果不替代
  原生 Windows、hosted required CI、真实付费模型或用户设备验收。
- 首次裸全量实际退出 1：系统盘 `/` 仅约 348 MiB 可用，真实触发每根至少 2 GiB
  的产品准入；失败日志 SHA256
  `5c9ec797de6c2465fff682a9b8a4128f863f43aca8ecdce1c2691cd6f2f77309`。
  仅切换 `TMPDIR` 到高空间卷的重试仍退出 1，剩余失败都硬编码 `/tmp`，
  继续触发同一低盘准入；日志 SHA256
  `32dc317e18ca5ae5a97b6bf1686bee715b98509bc2d72a101a97bba2b028fe79`。
  两份失败原件均保留，不改产品门槛、不跳过失败。
- 用冻结树已有的 `TIANSHU_TEST_TMP_ROOT` 与 `TMPDIR` 将测试数据移到
  `/Volumes/Date/tianshu-v35-tmp.kiJxww` 后，全部剩余失败用例定向复验退出 0；
  日志 SHA256
  `ecf57d762b443151696ef4727347520d0d5715719bac1e0cd93caddf627ffa61`；
  机器可读回执 SHA256
  `5397a7a56f3d5a853c931463d8d38bcb0986604d9fabe320ee276615939e64a6`。
  该开关只移动测试临时根，不改变执行侧低盘准入或测试选集以外的产品行为。
- 最终无筛选全量命令
  `TMPDIR=/Volumes/Date/tianshu-v35-tmp.kiJxww TIANSHU_TEST_TMP_ROOT=/Volumes/Date/tianshu-v35-tmp.kiJxww uv run --frozen pytest`
  实际退出 0；动态总计与跳过原因以日志为准，日志 SHA256
  `0eaca6235ea2ab724398bae7c9b0869a00b83c26a50d3e5568e2d96110d37850`。
  同一冻结非文档代码树上的 lock、Ruff、两份 diff 与本地链接检查也均退出 0。

回填仅改文档，须核非文档清单不变并重新通过静态与本地链接检查。
最终提交及新完整 hosted CI 的 head/base/合成 merge、全部 job 与原始报告验收，
以 [PR #228](https://github.com/lorek666/tianshu-agent/pull/228) 对精确对象的记录为准；
这里不把旧 CI 或 draft 跳过记为通过。

## v35 hosted CI 拒绝与 v36 Windows 收尾修复（2026-11-25）

v35 head `dbb6a43c5de0684368acfd53f1366fd659328fbe` 的 run
[45368602694](https://github.com/lorek666/tianshu-agent/actions/runs/45368602694)
真实 `completed/failure`：十项 job 中八项成功，Windows shard 0 与 Windows required
汇总失败。四份 pytest ZIP 和 Windows restore ZIP 均核 GitHub `digest`，ZIP member
与解压报告逐字节一致。原始阶段 JSON 记录六项失败：三项 Host 清理在 Windows 删除
`logs/daemon.log` 时触发 `WinError 32`，三项 SLO 状态背压测试在真实 Host 子进程
启动前拒绝测试锁根。两个 worker 共用 stdout，`-vv` 进度行会交叉拼接；额外
`FAILED` 行不能推翻 GitHub digest 绑定的阶段 JSON 与两段 `FAILURES`。失败日志
SHA256 为 `81c09ee64e6d8e80c5ab61bbbe3535a7623f90342ea6323780a0fec355a3fb1d`。

v36 只修测试收尾证据与锁根等价性，不改产品停止契约：测试清理先观察锁中的 PID
与 OS birth identity，退出请求绑定原 `instance_id`；只有锁快照未变才允许升级终止。
SLO guard 保留 `PYTEST_CURRENT_TEST`、实际维护锁根和预期锁根三重检查，改用文件系统
对象同一性避免 Windows 路径别名误拒。v36 的两项私有副本变异负控均实际退出 1，
受影响域回归退出 0，日志 SHA256 为
`5a8f774821452cf9ebb0091d84af73f9748a5ee16166625d630ccdc28ccdf425`。

v36 首次裸全量在系统盘低于产品 2 GiB 准入时实际退出 1，失败日志 SHA256 为
`95fbc5e038d9f1fafcce7ebd004e3706d8394ad150780b669825b7d52a9b5f69`；仅把
`TMPDIR` 与 `TIANSHU_TEST_TMP_ROOT` 指向高空间卷后，无筛选全量退出 0，日志 SHA256
为 `819327448569b1c8bed3e87b79e53129ba70aff599a7684e67994dd8f0e50f53`。
但独立 Host 复审仍拒绝 v36：birth identity 不可读时退回 IPC 探活会误判 OS 进程
退出；强制终止前只比较锁字节，没有再次核验 birth identity，PID 复用可能误杀。
主控在私有探针中复现两项反例，拒绝该冻结，不把本机全量绿当作最终通过。

## v37 当前候选验证与边界（2026-11-25）

- v37 冻结 index tree `8466fc6fff0fdafc3a3f1f56c76aee3bc068fd18`；src 树
  `317defbd5e35ad8f5295bdea1667ad6a11f4361c`；tests 树
  `2ed5d979c0ade66a4741a851a763a3cc284f5b1e`；scripts 树
  `1477cba5f743a3180b3e4dee42584262c106c28d`。
- 非文档清单 SHA256 `05561b0169e4e7d944a12144c75d30b1d3464370716ae60208488a94abd4a6c8`；
  冻结凭证 SHA256 `e7a4d2ed415ea81397410c44927d67e84458417b867ae4b0438d872d7de280e1`。
- birth identity 不可读时失败关闭，不再用 IPC 探活证明退出；强制终止前同时要求锁
  快照未变且原 OS 创建身份仍相同。新增回归覆盖不可读身份与 PID 复用，旧 helper 在
  私有副本对两项回归均实际失败；撤掉等待与改回路径字符串的变异也分别退出 1。
  负控回执 SHA256 `a161db483774c9a2f0595fc5f519a29d294258b3fb639ae8f684ef5ebe98dce4`；
  受影响域日志 SHA256 `72d48c0cf24e5b78e0dd4160bc91d16f40c9cd6f79cc04290a74e220e957caf6`。
- Host 与 SLO 两路独立复审均 PASS，报告 SHA256 分别为
  `2aad93ecae6679fddad216d3ef53178f72656e91f9e23aff36625cd5c49cd802` 与
  `9708574c1366745be7e4cee92076cfd4c4db009cf3960f08739117177f36ab78`。
  主控核全部原件哈希后，在新私有目录逐字节重算 manifest，重放关键绿灯、两项 Host
  变异和 SLO 变异均通过；主控回执 SHA256
  `afb512c654302f605af698079a068eb58184698e59c7f3ca62f0d5bcfcc046da`。
- 同一冻结树的无筛选全量命令仅设置 `TMPDIR` 与 `TIANSHU_TEST_TMP_ROOT` 到高空间卷，
  实际退出 0；动态总计与跳过原因以日志为准，日志 SHA256
  `063db1468d14037ae85f4a820531854c8dbde8333ec73d9231d6ef3f6675799b`。
  lock、Ruff、两份 diff 与本地链接检查也均退出 0。环境为
  `macOS-26.6.2-arm64-arm-64bit` / Python 3.12.13；不替代 Windows 原生验证。
- 文档回填只改文档；须重新核对非文档清单不变。最终提交及新完整 hosted CI 的
  head/base/合成 merge、全部 job 与原始报告验收以 PR #228 对精确对象的记录为准。

250ms 孤儿检查宽限和零孤儿门槛不变，不更改 workflow、权限来源或 C1–C5 契约。
PID 存活与创建身份分别查询，明确不是原子句柄观察；锁内容也不是认证父链证明。
owned launcher 的退出不证明任意后代已退出，附着端点检查不冒充拥有 OS 句柄。
Windows 原生沙箱、任意后代监管和远程 MCP 隔离仍未交付。
本轮不合并、不发布、不改 #225/#227 冻结对象，不自动开始下一大项。

## v39 整合冻结与最终独立复审（2026-11-25）

- 整合提交 `5e33b732a2f1514cddf2395384e40874e5f9ba3f` 保留远端平行提交
  `c1feb4185bec29d64cbaa7b4624c7ea4ccd086fb` 的 Windows 子进程环境、Host
  readiness 身份复核与 SLO 锁根判定，并合并两侧重复的测试清理 helper。
- 最终规范对象是 `git ls-files -z src tests scripts` 的 384 个跟踪文件。按
  repository-relative UTF-8 路径排序后，依次更新
  `path bytes + NUL + SHA256(complete file bytes)`，其中文件摘要是 raw 32-byte
  digest，不能把原始文件内容直接拼入总哈希。该规范 tree SHA256 为
  `a693e9d3c3756ab1c61b32964e8f5a8e7b3666a2ef5a3576923f4f96298bcf89`。
  它与 v35–v37 的历史 manifest 口径不同，不互相替代。
- 第一轮最终独立复审返回 `partial_fixed`，主控在私有探针中复现两项缺陷：测试
  清理把“后续 birth identity 不可读”误判为原进程退出；初始 `killpg` 抛出
  `ProcessLookupError` 时并未实际发信号，却被当作 process group 已清理。两项均
  先新增在旧实现上失败的回归，再修复为失败关闭。
- 测试清理现在分别处理相同 birth、birth 改变、PID 确认不存在，以及 birth 不可读
  但 PID 仍存在。POSIX 已退出但未回收的子进程用 `WNOWAIT` 确认 exited，不释放
  PID；未知状态绝不升级终止。process group 只有实际成功发过信号且随后观察到
  不存在才报 exited，初始 `ProcessLookupError` 保持 unknown。
- 生命周期、执行进程组隔离、SLO 环境/锁根三路独立读者均对最终规范树返回
  `fixed`。报告 SHA256 分别为
  `a4f69af0d1696784c67993d717e7657c43f80244a313dd0a13dab203dc9beba7`、
  `29b9047f281da725bea6834f66a96339a052d53b21590c28066f1d4a701c61fa` 与
  `22e9ae66549223ddc9ba0dd631d287c64d85a2f56f76cfcb397653508056f067`。
- 主控另建全新私有副本，逐字节重算规范树，重放关键绿灯和三项定向变异。恢复后
  关键回归再次通过，规范树哈希不变；回执 SHA256 为
  `60130e11b97d37b8c77956072af7696aef2ef9ec9a31232649faef2a046dfe02`。
- 受影响域回归、`uv lock --check`、`uv run --frozen ruff check .`、两份 diff 和
  本地链接检查均退出 0。同一冻结代码树上的无筛选全量命令
  `TMPDIR=/Volumes/Date/tianshu-pytest-8dGaXd TIANSHU_TEST_TMP_ROOT=/Volumes/Date/tianshu-pytest-8dGaXd uv run --frozen pytest`
  实际退出 0；动态总计与平台/opt-in 跳过原因以日志为准，日志 SHA256 为
  `dcc1c560a46c1b2a7cbf7a13e67027a82d4d0233c98450eb960621526faab74b`。
  环境为 macOS arm64 / Python 3.12，不替代 Windows 原生验证。
- 纯文档回填后已再次核对上述规范树哈希零变化。最终 head 的 hosted required
  CI、两份 Windows shard、合成 merge 与原始报告尚待严格验收；本机 macOS 结果
  不替代 Windows 原生验证。Windows 任意后代监管仍由 #224 跟踪。
