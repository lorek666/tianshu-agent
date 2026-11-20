# macOS outer bundle 资源封套局部修复（2026-11-20）

## 原因与范围

旧预览静态 manifest、架构和文件集检查通过，但主程序只有 linker-ad-hoc 签名，
缺失外层资源封套；本机原生 codesign/spctl 因资源封套错误退出。该失败不能称作
“系统仅因没有付费签名而警告”，spctl 的操作错误也不是 Gatekeeper 策略结论。
旧草稿、归档和原始解包 App 保持不动，实验副本不得晋升为发行资产。

本修复只改[打包脚本](../../scripts/package_app.py)与[静态 verifier](../../scripts/verify_release.py)，
不改 C1–C5、T1 成本/信任政策、schema、报告字段、索引 `signed: false`、安装器、
权限或凭据逻辑。没有新增版本身份、产品 build、公开发行或远端写入。

## 实现

- 最终 Tauri App 验证后只执行一次 `codesign --sign - --timestamp=none`。
  使用无身份的免费 ad-hoc，不使用 force、deep 签名、xattr 清理、sudo、证书或失败重试。
  无签名与 linker 签名是合法输入；已有封套预先拒绝，其他已有签名由 no-force 原生语义拒绝。
- 封装前后核对普通文件/目录闭包、SHA256 与权限，只允许主程序字节与精确的新
  `_CodeSignature/CodeResources` 变化；原 Resources、Info.plist 与原有模式不变。
- 真正执行 `codesign --verify --deep --strict --verbose=4`。
  直接调用 `release_artifact` 只验证已封装输入，不重签。
- tar 先写入私有 staging，受限还原时先核对所有成员、类型、大小、权限及闭包，
  逐层使用 no-follow dirfd 与 exclusive 文件创建，不使用 `extractall`。
  还原字节/权限核对后对恢复 App 再做原生 strict 验证，通过才公布归档及 checksum。
  归档仍不记录 App 根；私有恢复根的 0700 是本地容器，不是假造的归档根权限。
  失败保留私有现场，不把多文件发布称作事务或物理原子。
- 静态 verifier 仅可选地允许精确的一个目录和一个非空普通 CodeResources 文件，
  拒绝链接、特殊类型、缺失、空、额外、大小写冲突与逃逸路径；历史 unsigned/synthetic
  fixture 兼容保留。它不解析 Apple 信任链，不以 plist 哈希核对冒充 Apple 验签。

## 验证与历史失败

[定向测试](../../tests/test_macos_outer_seal.py)覆盖命令/失败传播、变化范围、
直接输入不重签、打包末端接线、受限往返与归档负控；
[静态测试](../../tests/test_release_verify.py)覆盖可选封套兼容及拒绝分支。
原路径/构建/checksum 测试的 `b"binary"` 夹具只 mock 新增原生边界，
不把 synthetic App 误写成原生 codesign 成功。

- 基线定向 RED 已保留；另对 a073 原脚本调用当前末端接线断言，实际在缺少 `--sign`
  的断言处 RED，当前实现 GREEN。该补充控制首轮未 canonicalize 系统临时路径，
  误触 `/var` 符号链接拒绝，不计作接线反例；旧错误原件保留后仅在 fresh canonical 根纠正。
  修复后定向与受影响 packaging/build/verify/assemble/manifest
  回归实际通过。命令与 raw 日志保存在本候选 ignored `.omc/artifacts/`，测试总计以日志为准。
  回归中的 source-copy smoke 沿用[既有双层测试守卫](../../tests/_startup/release_smoke_test_launcher.py)：
  verifier→smoke、smoke→CLI 在注入测试 startup 前核对原始 argv/cwd/env，子进程在执行入口前
  核对实际 maintenance/registry/authority getter、源码包路径并写 PID/roots 收据。
  这些是受控源码 Host/Daemon 测试，不是原生发行物 smoke，也不是 OS 物理隔离；
  不允许将测试钩子带入产品或用环境变量声称日常账号的原生 bundle 已隔离。
- 本机 Apple clang 21.0.0 / arm64 普通编译最小无副作用 C 文件，**从未运行产出的二进制**。
  `-Wl,-no_adhoc_codesign` 生成真实 unsigned，`-Wl,-adhoc_codesign` 生成 linker-ad-hoc；
  两者用当前 helper 实际封装、strict 验证及真实 tar 往返通过。
  另将 fresh standalone linker 输出先普通 ad-hoc 签名、再组成 fresh App，
  实际 no-force sign 返回 `is already signed`，helper 拒绝且未修改输入。
  原 argv、版本、exit、stdout/stderr 和 hash/mode 清单逐次保留。
- 首轮普通签名负控曾错误地在已成包主程序上签名，原生工具生成了 bundle 封套，
  所以 helper 在已有封套前置拒绝，未到预期原生拒绝分支，测试为真实 RED。
  旧证据不覆盖；只在全新 standalone 夹具纠正这个前提，不清签名、不换 flags 重试。
  首轮长行 lint RED 也保留，修排版后 scoped lint/diff 检查通过。
- 本机结构实验不等于新产品 archive、原生启动、Gatekeeper 或安装验收；
  未运行旧资产中的 Python、桌面壳、Host/Daemon 或 GUI，未触及生产 Keychain/数据。

## 尚待门禁与边界

当前是本地未合入候选：`2c5a1dd` 的代码/测试独立第二读者预审已通过；
最终集成冻结、冻结后唯一最终全量、正式提交绑定、新 hosted required CI 和原生验收
尚待完成，不能继承其他 SHA 的绿色结论。

免费结构封套不提供认证发布者身份、公证或系统策略放行，也不取消
原生启动/runtime smoke、Keychain、核心任务、SLO、进程恢复、升级/回退/备份门禁。
源树扫描与归档之间的同 uid TOCTOU、原始 runtime archive provenance 边界仍存在。
本机 arm64 证据不代替 Intel Mac、其他 macOS 版本或 Windows 用户设备验证。
