# Apple Silicon 预览发行链（2026-11-20）

## 范围与决策

首发政策独立位于 `66c9418a11cfd5d4e9c16a26fb73c89145bd20fd`
（`docs(contract): scope first release to Apple Silicon preview`）。本实现叠加其上；政策 PR 与
实现 PR 应分别按保护规则合入。未直接修改 main，也未推送、建 tag、触发远端 workflow 或创建发行。

- 手动布尔开关 `macos_arm64_preview` 默认关闭；启用时只选择 `macos-arm64` 的原生构建、下载、
  校验与汇总，草稿标记为 Apple Silicon Preview / prerelease。默认及 annotated tag 仍走四目标。
- preflight 要求候选 SHA、当前 `origin/main` 与 workflow 定义提交一致，最新一次同 SHA 的
  main push CI 四项 required 名称存在且全部 jobs 成功；最新失败/运行中不能回退旧绿。
- 预览 assembler 只接受指定目标的归档、checksum、manifest、验证报告，执行写前完整性检查，
  拒绝多余/缺失输入、非空旧输出及输出位于输入树内；共用既有身份/哈希/原生 smoke 校验。
  不新增 manifest 或 release-index schema，不接受任意部分目标清单。原四目标 API 默认行为保持。
- workflow 从 release-index 生成白名单，核对输出文件集合，只上传索引资产与索引自身；不再用
  目录 glob 向 Release 附件取件。已有 Release（含 draft/prerelease）拒绝覆盖，API 错误不当作不存在；
  预览须使用未占用 tag，四目标路径已有 tag 须解析到候选 SHA。同版本两入口串行化。
- runtime、安装/卸载脚本、数据/权限契约、依赖锁和 required CI 定义均未改变。修正文档中下载后
  辅助脚本的路径与 Windows checksum 文件名，并说明工作流实跑不等于用户设备验收。

## 冻结对象

- 实现提交：`8c2efcda44b90d77ea9f87a9dc40c670d816893c`
- Git tree：`23afdebccf8a370d8af5102e22ceac8c81431db9`
- 非 Markdown 代码/测试树 SHA256：
  `732d401285c3b815e8ab4ca8c5fb5c9d3452ef1a8a4009a31127778936aeb5b7`

指纹算法：取 `git ls-tree -rz --full-tree HEAD` 的记录，排除路径以 `.md` 结尾者，保留原序，
以 NUL 连接并追加结尾 NUL，再计算 SHA256。后续证据回填仅修改 Markdown；最终提交仍须证明
此指纹未变。云端 required CI 和真实发行必须绑定合入后的精确 main SHA，不继承本机或前驱对象结论。

## 验证与证据边界

> **后续整合核对（2026-11-20）**：下方旧全量退出码与日志摘要作为原始历史保留，
> 其中“隔离环境”的旧判断不再作为子进程隔离证明。verifier 与 runtime smoke 会逐层重设
> `PYTHONPATH`，仅提供测试根环境变量不能保证 `tests/_startup` 在真实子进程加载。
> 本次整合通过纯测试侧双层 guard 修正源码集成 smoke；新冻结对象、限定结论与未验证项见
> [整合门禁记录](2026-11-20-preview-release-integration.md)。不倒推旧运行未触碰生产根，
> 不读取生产现场，也不把新测试绿色冒充未改命令的发行包/用户设备 smoke。

作者先运行新 workflow 定向红测；未实现开关/矩阵/下载限制与输出白名单时失败。非法布尔输入
探针也发现了 Bash case 最后一条条件命令未可靠触发 `set -e` 的问题，改为显式拒绝分支后通过。
assembler 作者先留存缺失新接口及输入/输出重叠的红证，再实现共享校验与预览写前检查。

冻结树的发行域回归与隔离环境全量 pytest 均 **exit 0**；`uv lock --check`、全仓 Ruff、
`actionlint .github/workflows/release.yml`、`git diff --check` 与相对文档链接/锚点核验通过。
全量保留平台与 opt-in 边界：没有把 Windows、真实摘卷、冻结任务集测量或 soak 的跳过
写成验收通过，也未用 skip/xfail 掩盖本次失败。扩展的符号链接/硬链接/FIFO 负控限 POSIX，
已在本次 Mac 运行，不据此声称 Windows 原生验证。精确通过/跳过清单以原始运行日志为准，
不在状态文档手抄静态套件总数。

复现全量命令（独立 checkout，先同步冻结依赖）：

```bash
ev="$PWD/.omc/artifacts/macos-arm64-preview-20261120"
mkdir -p "$ev/full-locks" "$ev/full-registry" "$ev/full-authority"
PYTEST_ADDOPTS= \
TIANSHU_TEST_LOCK_ROOT="$ev/full-locks" \
PYTEST_TIANSHU_REGISTRY_HOME="$ev/full-registry" \
TIANSHU_AUTHORITY_ROOT="$ev/full-authority" \
uv run --frozen pytest -o addopts='' -q -rs
```

全量日志 SHA256：`8e15118e73d6a629b60178f9caf381670434602969d2070918fca9c14d5be30f`；
发行域日志 SHA256：`29d8b737495adf3b22d9a8a6aae80b72ddf28a32dec4ec25b6f635ec1b7d7491`。
原始日志位于本地 ignored `.omc/artifacts/macos-arm64-preview-20261120/`；assembler 原始红绿
日志保存在作者独立 worktree 的 `.omc/artifacts/preview-assembler/`，均不作为发行资产提交。
完整本机原生运行 smoke 仍未执行，详见下文；本机门禁不替代精确新 main 的 hosted CI。

### 独立 Tier 1 复审

第二读者会话 `695897fa-5eb0-4284-aaf6-9c0ba35c2fd6` 独立核验以上冻结 SHA/tree/非文档指纹，
对 assembler、workflow、关联测试与生产者/消费者约束复审，结论 **PASS（限本次范围）**。
本范围未发现已确认 P0/P1 或直接阻断预览契约的 P2，无需重开源码。

除关联域测试与静态检查外，第二读者执行了真实本地 Git 和实际 workflow shell 探针：
旧/新 full assembler 的文件字节与模式差分；preview/full 的精确上传计划；缺失/额外/链接输入、
身份/哈希/smoke 错配、旧输出拒绝；CI 最新失败/运行中不回退旧绿、缺 required 或辅助 job
失败/跳过均拒绝。探针仅替换安装与外部 gh transport，不把合成资产当真实原生或 hosted 证据。

报告 SHA256：`07f1c62342c156cbc87e19bff73b9329d0cb12274415b5888a497b77aa381289`；
独立探针脚本 SHA256：`087b9b21daec4f5cae8a3c73594a59202f357a0387f62389baff41918dd173fa`；
结果 JSON SHA256：`45db849c82f6d4232a90475d9d6d3e19ba8283ff3190df1db00419ea8f3fc508`。
报告与原始探针保存在该第二读者会话的 `scratch/final-preview-review/`。最终回填只改 Markdown，
不得据此将公开发行、原生构建或用户设备验收写成已通过。

### 本机原生构建：已通过；完整运行 smoke：未执行

在独立 worktree 的同一冻结提交执行真实锁定 bundle 准备与 Tauri
`aarch64-apple-darwin` 优化构建/打包，`uv lock --check`、`uv sync --frozen`、
`build_release.py`、`package_app.py` 及 `git diff --exit-code -- ui/dist` 均 exit 0。
本机观测为 arm64 / macOS 26.6.2 (25G83)，CLT-only 环境完成构建；这不是该 macOS 版本的
完整用户设备验收或最低支持版本承诺。构建解释器为 CPython 3.12.13，随包 runtime 仍是锁定的
CPython 3.12.16 / build 20261110，没有替换版本或锁。

本机归档 `Tianshu-Agent-0.1.0-macos-arm64.app.tar.gz`：39,446,305 bytes，SHA256
`d00518e840fc0d09be9f9d241e6e86fbeea1f36bfab5e317765e84464431ad7c`；manifest SHA256
`b1e6caacfceff38d3f50ea789071797933a5c698032ff0d234dc7966dec04445`。
锁定 runtime archive SHA256 校验通过，App 壳与 bundled Python 均识别为 Mach-O arm64。

随后只调用未改动的 `verify_release(..., run_smoke=False)` API 作 **PARTIAL 静态诊断**，
source/归档/布局/哈希校验通过，保留 `runtime_smoke.status=not_run_by_caller`。
这不是完整 CLI verifier 通过；没有生成可供 assembler 接受的原生 PASS 报告，没有调用 assembler
或捏造 Actions 构建 run。bundle/归档仅为本地工程证据，不作为待公开发行资产。

完整 smoke 在本账号为 **BLOCKED / NOT_RUN**：生产维护锁根绑定实际 OS uid，恢复 registry
绑定系统 Home；测试专用环境变量仅通过 pytest 启动钩子隔离，不作用于真实 bundled CLI。
因此不在日常账号上抢占生产用户级锁或访问该 registry，也不改 HOME、注入启动钩子或绕过校验。
完整原生运行验收留给干净标准用户测试账号或独立 runner；安装、Keychain、升级/回退与
Gatekeeper 等用户验收仍须单独完成。没有安装进日常账号、调用真实模型、访问生产凭据或公开发行。
本机报告由会话 `45f368ee-48fb-4fd6-a530-4bf201c0c4d1` 生成，SHA256
`c6c27674f7f5d4c8c17e77dee88e28cef7cfcc54a17cf38c19f98e1d8c417b8d`；独立 worktree 的
原始构建、打包、静态诊断和干净状态凭据保留在 ignored `.tmp/native-release-8c2efcd/`。
PARTIAL JSON SHA256 为 `c0f33eb9155902c3f0d2a3a208aa7251d0d71217a3af4eb575f13b9218364ff9`。

## 发布前仍需实际完成

保护合入政策及实现 PR → 新 main 提交的 required CI → 正式预览 workflow 实跑 → 草稿下载、
tag/SHA/索引与全部字节核验。之后在干净标准用户账户与隔离数据上验收首次安装、Keychain、核心任务、
受控旧构建升级、失败回退、卸载保留数据和备份恢复，再完成适用的 PRD §10.5/§10.6。
后续独立政策 `d4ef379bc785de5d52379d2c15d5f291dff3924f` 仅取消首次 `macos-arm64` 限定预览的
固定 ≥10 小时 soak 门禁，不设替代时长；长时稳定性未验证，四目标正式发行的原要求不变。
未获得其他必过项证据前不公开发行，不宣称未测 macOS 版本或其他平台可用。

版本级 workflow concurrency 不锁定外部操作者；人工公开前仍须重核 tag 与候选字节。已有的路径检查
不构成恶意同 uid 并发写者的物理隔离；未签名/未公证、无自动更新、无物理断电持久性承诺等边界不变。
