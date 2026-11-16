# Windows 原生发布验证修复规格

## 问题与证据

精确 main `2e52bda205ae5a6a6cbe26b86b6e8997f64218fe` 的 release run
[`43949045992`](https://github.com/lorek666/tianshu-agent/actions/runs/43949045992)
中，preflight 与 macOS arm64/x64 均成功，Windows x64/ARM64 都完成了 Tauri
build、UI 一致性检查和 `pnpm.cmd` 调用，但在 `Verify release bytes and target`
失败：

- windows-x64：
  `发行验证失败：SHA256 文件格式不合法`
- windows-arm64：
  `发行验证失败：python/vcruntime140_1.dll PE 架构与 target windows-arm64 不匹配`

失败日志已冻结在
`.omc/artifacts/windows-pnpm-release-fix/release-43949045992-failed.log`，
release 元数据 SHA256 分别为
`9c7ba8deba38552fb2539eca1af821d2cf3461ca1c18e7f888a84064fb39ea24`
（failed log）与
`29225ff79e75b41c04f4b8d284c6ce5b8ccd248e8622a9a52894fa4bfffb0b69`
（run JSON）。

## 根因

### CRLF checksum

`scripts/package_app.py` 通过 `Path.write_text()` 写
`<sha256>  <artifact>\n`。Python 文本模式在 Windows 把 `\n` 翻译成
`\r\n`，而 `scripts/verify_release.py::_verify_checksum()` 要求固定单行
LF 格式。macOS 使用 LF，因此同一实现对 mac 绿、Windows 红。

### ARM64 runtime 的未引用 x64 DLL

锁定的
`cpython-3.12.16+20261110-aarch64-pc-windows-msvc-install_only.tar.gz`
（SHA256 `7122bf62...`）中：

- `python/python.exe`、`python/python312.dll` 和
  `python/vcruntime140.dll` 都是 ARM64；
- `python/vcruntime140_1.dll` 是 x86-64；
- 对该归档所有保留的非 `site-packages` 文件做字节扫描，只有
  `vcruntime140_1.dll` 自身包含该 DLL 名，解释器与核心 DLL 的 PE import
  table 都不引用它；
- 当前 `_prune_runtime()` 已删除整个 `Lib/site-packages`，因此其中正常的
  多架构 distlib launcher 不会进入 bundle。

现有 verifier 的 PE 架构封闭检查是正确的，不能为这个多余文件放宽。

## 不变量

- 四目标 checksum 文件必须始终是 ASCII、恰好一行、以 LF 结束，不能随宿主
  `os.linesep` 变化。
- verifier 继续拒绝 CRLF、多个换行、非 ASCII、文件名不一致和 hash 不一致。
- Windows x64 bundle 保留 `vcruntime140_1.dll`；该 DLL 是该架构 runtime 的
  合法依赖候选。
- Windows ARM64 bundle 只删除锁定的上游归档中已确认未被任何保留文件引用的
  `vcruntime140_1.dll`；其他 ARM64 runtime 文件保持不变。
- `python.exe`、`python312.dll`、`vcruntime140.dll` 必须保留。
- 不放宽 PE 架构断言、不按扩展名整体删除 DLL、不切换 runtime、不忽略该文件。
- Python 3.12、四目标 lock、零第三方运行时依赖和现有 Tauri/NSIS 契约不变。

## 验收

1. 在旧实现上，Windows 原始 release run 必须提供上述两类红日志；本机用
   Windows 路径语义或字节级 fixture 模拟 checksum 文本模式翻译时必须红。
2. `release_artifact()` 的 checksum fixture 直接断言 bytes 以 `b"\n"` 结束且
   不含 `b"\r"`，避免 `read_text()` 归一化掩盖问题。
3. `_prune_runtime()` 对 `windows-arm64` 删除
   `python/vcruntime140_1.dll`，同时保留 `vcruntime140.dll`；对
   `windows-x64` 保留两者。测试必须检查真实文件存在性，不能只检查 manifest。
4. verifier 的现有 PE 架构负控继续通过，修复不得放宽。
5. 相关 package/build/verifier 测试、全仓 lock/Ruff、全量 pytest 在新提交上通过。
6. 新 PR 精确 SHA 的 Ubuntu、macOS、Windows 两分片、Windows 汇总、Windows
   Rust 与 UI required checks 全绿，并进行独立第二读者复审。
7. 合并后在精确 main 重跑四目标 release；四个 target 都必须产出 artifact、
   通过 verifier 并由 assembler 汇总，Draft Release 才能运行。

## 范围外

Windows x64/ARM64 用户设备安装、升级/回退、SmartScreen、签名、自动更新以及
物理掉电仍不在本修复的证明范围内；GitHub runner 成功不等同于用户设备实测。
