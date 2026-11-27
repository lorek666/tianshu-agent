# 2026-11-24 · 材料写入 target 守卫（#204）

## 背景与根因

Tier 1 修复由真实负控驱动，共发现并关闭三类独立问题：

1. 私有 `.<db>.materials` / `.memory.materials` 是命名边界，不应被祖先中的 `artifacts`、`blobs`、`memory_blobs` 误判为标准材料域。此前异根 `data_root` 或自定义 `--db` 的上述组合会 fail-closed 拒写。
2. raw 路径显示为标准域、canonical 解析后却落在另一标准域时，不能继续沿用 raw 同根 `events.db` 的归属。标准材料现在强制 raw/canonical 同根同域；私有 target 则强制位于其 canonical 容器内。
3. 只校验目录 parent 会漏掉无需竞态的静态子目录链接；只校验“尚不存在”的 blob 会漏掉预置 leaf symlink。快照、撤销 journal、事件/内容/记忆 blob、执行/循环/子任务 spill 现在均守卫具体 target，并先守卫再判断是否已存在。

blob 仍先于事件事务或 manifest 引用持久化；拆分 lease 至多留下孤儿 blob，不提交虚假事件或材料引用。

## 验证

- `uv run --frozen pytest -o addopts="" -q -rs tests/test_restore_eventlog_access.py`
- 受影响回归：restore / backup / recovery / undo / events / content / memory / snapshot / dispatch / tasks / summary loop。
- `uv run --frozen pytest`
- `uv lock --check`
- `uv run --frozen ruff check .`
- `git diff --check`

最终本机全量 `pytest` 退出码为 0；托管 required CI 结果以关联 PR 的 Actions 日志为准，不用本机通过替代。

独立第二读者只读审阅完整 `restore_access.py`、全部生产调用点与完整回归文件，首轮结论为 **PASS**；当时记录绑定的是 `git diff --binary` SHA256 `00d58ddd286a295387e9e62bf05a7f5c8a7f4f50b25b4f5208bde546af7d7a98`。该口径只保留作历史，不作为现行 code/test tree 绑定；后续 Windows 跟进已按“仓库相对路径 + 实际文件字节”重新计算并最终取代首轮结论。

新增的 snapshot preimage 负控预置真实逃逸文件，而不是悬空链接：在隔离工作树仅回退“先验 target 再判断 exists”这一处后，用例确定性失败于 `DID NOT RAISE RestoreScopeBusy`；最终候选同一用例通过，并断言保留区文件字节未变、manifest 未提交。

## PR #231 Windows shard0 跟进（2026-11-24）

- 真实失败不是放宽隔离要求的理由，而是测试夹具与 Windows 链接种类不一致：`_symlink_or_skip` 对所有链接固定 `target_is_directory=True`，snapshot preimage 负控因此创建了“目录种类、指向现有普通文件”的 leaf symlink。CPython 3.12 `ntpath.realpath(strict=False)` 的非严格错误白名单不含 `WinError 267`，在 readlink 回退前抛出 `NotADirectoryError`；原守卫只从 `resolve()` 结果看保留范围，随后被材料守卫包装为“无法解析目标”。
- 具体 target 先于 `exists()` 守卫的顺序保持不变。`ensure_path_not_held` 现在独立遍历 target/祖先的显式 `readlink` 目标（带循环保护），不再把可能失败的 `realpath` 当作唯一范围线索；若 canonicalize 仍不可用，材料写入继续 fail-closed。私有 `.<db>.materials` / `.memory.materials` 容器和标准域 raw/canonical 同根同域规则未放宽。
- 夹具按真实 target 种类创建链接。新增确定性 `WinError 267` 注入负控、现有文件 leaf symlink 的同根正控，以及 foreign-root / nonstandard 逃逸负控；本机 mac-arm64 定向、完整 access 文件及受影响 restore/snapshot/event/material 回归均通过。未在本机运行 Windows，不能称 Windows 已验证；须由主代理在新 code/test tree 上重跑托管 Windows shard0 与全量门禁。
- Windows 跟进的独立第二读者结论为 **已修**，复核 code/test tree 的路径+实际字节 SHA256 为 `b8b434b7e9d8f581b2f1ceecd62b2b4ba93ea26c35a4b4e7ee1d8b79f362a279`。探针覆盖相对/绝对/链式/祖先链接、循环终止、`WinError 267` 与 readlink 失败拒绝、target 先于 `exists()`，以及同根正控和 foreign/nonstandard 负控。1200 层链接触发 `RecursionError` 的非阻断观察仍拒绝写入，未发现可达绕过；托管 Windows shard0 仍是独立平台门禁。
- Windows 跟进后的新 code/test tree 已在本机 mac-arm64 重跑完整 `uv run --frozen pytest`，退出码为 0；锁文件、Ruff、diff 与文档相对链接门禁通过。该结果不替代托管 Windows shard0。

## PR #231 Windows 扩展路径前缀二次跟进（2026-11-24）

- 上一轮的托管 Windows shard0 不是超时：新增 `WinError 267` 注入用例在材料入口抛出了 `RestoreAccessError`，没有观察到预期的 `RestoreScopeBusy`。该红推翻了上一轮绑定 `b8b434b7…` 的“已修”结论，历史记录保留但不作为当前证据。
- 根因是 Windows `os.readlink()` 可返回 `\\?\C:\…` 或 `\\?\UNC\server\share\…`。显式链接守卫已经读取到真实目标，但范围比较仍把扩展长度前缀当成路径的一部分，因而匹配不到普通形式的保留恢复根；随后 canonicalize 才因注入的 `WinError 267` 失败。
- 恢复范围键现在仅折叠 Windows 驱动盘与 UNC 的这两种扩展前缀，再交给既有 `overlap_key()`；链接递归、实际打开和写入仍使用原始访问路径，不把比较用的规范化结果误当成文件访问结果。target 与链接循环保护也复用同一范围键。
- 新增驱动盘/UNC 前缀等价回归；现有 `WinError 267` 负控在 Windows 上额外强制 `readlink()` 返回扩展前缀，确保托管平台覆盖真实失败形态。仅在内存中撤回前缀折叠后，新回归确定性失败；恢复实现后通过。完整 access 回归、受影响域回归、锁文件、Ruff 与 diff 按 Tier 1 重跑，独立第二读者须绑定新 code/test tree；托管 Windows shard0 全绿前不销账。
- 独立第二读者完整阅读生产实现与对应回归后结论为 **已修**，复核 code/test/schema tree（`src` / `tests` / `schemas` 的 tracked + untracked 实际文件字节，按仓库相对路径排序）SHA256 为 `d0a47f386db17c65f8129eb5be0b0e1cd0c02232505dcd5dce5a8c0a05d8fa73`。探针覆盖相对/绝对/链式/祖先链接、realpath 失败、循环终止、readlink 失败拒绝、target 先于 `exists()`、标准域与私有容器边界；未发现新增绕过或异常吞没。该结论不替代原生 Windows 与托管 shard0。

## 主干整合：撤销执行先准入再读材料

- 第一份整合候选 `849a1ea` 的独立复审判定为部分修：写前快照会在检查具体 manifest 前读取它，撤销会在检查具体 journal 前读取它。复审探针都用临时保留区中的真实文件，没有执行用户文件变更。
- 写前快照在具体 manifest 的准入锁内读取旧清单、更新并落盘。撤销先准入 manifest 和 journal，再读取证据、打开控制 Run、执行动作或补写完成事件。CLI 的 `--approve` 直接走该执行入口，普通只读预览仍可独立调用。
- 没有常驻 EventLog 时，标准材料仍以只读核验后的独占主库访问为前提。仅同一进程和线程的嵌套操作可复用该范围内同一主库的访问，每个 target 都重新核验。POSIX `fork` 后的子进程必须独立申请，即使继承了线程局部数据。
- 已完成的撤销只在日志、磁盘和请求的工作区范围一致时补记完成事件。`--workspace` 不匹配时不补记，匹配时仍可幂等恢复。
- 定向回归覆盖保留区拒绝前不读取目标、不调用执行回调、不追加事件，以及未启动 EventLog 时同根撤销可完成。第二份整合候选 `a10f686` 因 `fork` 继承复用范围、完成态撤销未核对请求范围被独立拒绝。两轮中断的本机全量记录均保留，不作为通过证据。
- 最终本地冻结 `b8b005e` 排除 `docs/` 的 Git 树清单 SHA256 为 `1710143bad4a4336b280bfce8de37911dd192b697b2cc4caab8c22f20c7d476e`。在实际合入 #236 的 `c5b7882` 上重建后，该哈希不变。本机裸 `uv run --frozen pytest` 退出 0，原始日志 SHA256 为 `c564d17e3d06ecc79c22fe634d3ddb3e9ffe83545bac9d63ebed70942757967b`。收尾发现 macOS 封套测试生成了仓库内未跟踪诊断文件；另行核对索引、跟踪文件与冻结对象均未改变，保留原始收尾记录与诊断文件。
- 独立第二读者对最终冻结判定为已修，复审原件清单 SHA256 为 `2a4861b804bd027ea0c1f06f1e24ed7f7ee8f6ecbb5cfd34e3c7a5e050a1b985`。主控重新导出并逐路径核对 mode 与 blob，重放全部探针与预期变异失败。Windows 语义注入不代替新 PR 的托管原生 CI。

## 残留边界

材料守卫核验的是 lease 获取时的具体 target。同 uid 仍可在检查后替换目录链接；本轮没有实现 fd-relative/openat 物理封闭，也不把 canonical containment 或本机测试通过表述为物理隔离。Windows 用户设备与最终托管 CI 状态须以对应 Actions 日志为准。

## 与 MCP 结果持久化的整合复验（2026-11-27）

整合候选保留 MCP 内容引用、生命周期与 Receipt 的校验。派发的共用写入入口核验具体 blob 目标，再检查文件是否存在并按需落盘；读回正文时继续校验内容完整性。撤销和快照校验各自的具体材料。主干四 Windows 片、两 macOS 片与时限未改。恢复材料、撤销、快照及本地 MCP 受影响回归通过；最终全量、独立复审和新托管 CI 以当前对象的回执为准。

## Windows 热启动门禁跟进（2026-11-27）

[CI 45922950669](https://github.com/lorek666/tianshu-agent/actions/runs/45922950669) 的 Windows 第 3 片在真实热启动冒烟测试中记录 p95 为 2,584.645 ms，超过 2,500 ms 门槛。最慢样本的 Host 就绪耗时为 2,407.288 ms，Daemon 内部就绪指标为 839.094 ms。该轮 Windows 汇总失败，短 soak 通过且无孤儿进程。原始分段指标无法单独确定 Windows 上全部延迟的原因。

Daemon 启动时会反复检查恢复见证。每个临时或正式见证现在只查询一次目录项元数据，查询本身不跟随链接。悬空链接仍能被看见并进入原校验，缺失目录项可以继续检查，权限或 I/O 错误仍拒绝准入。具体材料的范围核验、显式链接目标及实体身份检查保持原约定。定向回归在旧实现上检出普通文件与缺失见证的重复查询，也验证了带悬空目标的临时见证仍被拒绝。Windows 热启动结论以新候选的原生 CI 为准。
