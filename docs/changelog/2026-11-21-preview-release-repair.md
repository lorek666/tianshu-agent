# 0.1.1 组合修复：模型用量精确版本与发行身份

日期：2026-11-21。状态：**本地实现及定向门禁已完成，最终整合预审、全量、required CI 和原生/真实模型验收尚待完成**。这不是发行或真实评测通过声明。

## 原因与范围

旧基线 `a0732d9881247998df24db7af9f1c7989c1ae7a4` 已按保护规则合并；原 [ready RED 44670311820](https://github.com/lorek666/tianshu-agent/actions/runs/44670311820) 保留。修后 head `314e987ea5c8d1179caff6a4bcdf488c055f577f` 的 [ready CI 44678467012](https://github.com/lorek666/tianshu-agent/actions/runs/44678467012) 与 a073 的 [main CI 44681321379](https://github.com/lorek666/tianshu-agent/actions/runs/44681321379) 均为各自首次 attempt 成功，不是重跑旧失败。精确 a073 的 [release workflow 44731411389](https://github.com/lorek666/tianshu-agent/actions/runs/44731411389) 已生成未发布的 `0.1.0` draft/归档；静态闭环与 staged runtime smoke 通过不抵消归档 App 的原生 codesign 资源封套失败。旧 spctl exit1 也不构成有效 Gatekeeper 策略结论，GUI/安装/Keychain 等原生验收未完成。最终 App 资源封套修复见[独立记录](2026-11-20-outer-bundle-seal.md)，本次没有修补或覆盖旧发行对象。

C1 合法缓存用量不能通过 C3 v1 封闭 usage，真实模型响应后会在结果提交处失败；只改第一事件仍会在回合事件再次失败。[独立契约 PR #206](https://github.com/lorek666/tianshu-agent/pull/206) 已先合入主干 `53424e3e91f0d97732be8ff262baedb57313fe9a`，实施与契约没有混在同一 PR。

- 仅 `model.result`、`run.model_turn` current 升为精确 v2，旁表记录 2；全局默认仍 1，原 run.start/end v2 不变。
- 完整深拷贝旧 v1；全部原 v0 各自解释不变，尤其 turn v0 usage 仍为 open object|null。新 v2 可选缓存键为 integer|null、minimum 0，usage 仍封闭，input/output 仍必填。
- 新缓存字段负整数值数值 `-1.0` 被拒，合法 JSON integer `2.0` 仍接受；不改全局 validator、旧版本或 input/output 语义。
- 两个 JSON current 与历史 v1 同步；legacy 镜像不变。不删缓存、不增加任意 extras、不补历史默认值、不 UPDATE/DELETE 旧事件，不自动重放已发模型调用。
- pricing/budget/summary/授权/loop 未重写；非零缓存仍金额未知，已知 tokens/requests 与未知 micros 分维度处理。内存“已知费用和”为零不能冒充总费用免费。

## 单一新发行身份

旧 `0.1.0` 对象保持原样，组合候选用 `0.1.1`。仅修改 `pyproject.toml`、包 `__version__`、`control.DAEMON_VERSION`、uv lock 自家 editable package、Cargo package、自家 Cargo lock entry、Tauri 顶层版本。锁文件的第三方内容逐结构比较不变；无依赖新增、协议升版或全局替换。独立 UI 私有 package/lock/src/dist 不动。

新增[实际七处身份绑定测试](../../tests/test_release_version_identity.py)，从真实声明和模块读取，不用手写替身；故意漏改 Daemon 常量会准确失败。仅真实 source-copy smoke 的 positive/negative 前置版本采用当前 `__version__`；`9.9.9` 漂移负例、shadow sentinel、自包含历史 `0.1.0` fixtures 不变。真实拷贝包的 release identity 更新后正常重算 manifest，不绕过 runtime 检查。

## 本地证据与保留的 RED

- 原 a073 的实际两个 adapter→实际 loop 在第一事件拒缓存；独立实际 `commit_model_turn` 在第二事件拒缓存，均是真实失败而非测试启动错误。
- 修复后[真实产品路径回归](../../tests/test_model_usage_v2.py)通过不联网的受控 Transport 保存完整规范化用量及精确版本；验证 result/turn/cursor/版本同事务、first/second 版本行和第二事件故障整体回滚，派发前已持久化，暂停/恢复不重发已发模型回合，重放/重建不加模型调用，未知 micros 占用不退款且幂等。
- 固定原 schema/payload/hash/version；精确旧 a073 完整 schema 源配合未改的 EventLog/data_admission，在重开全库准入处拒新 v2，早于 WAL/schema 初始化。连接、restore access、SQLite 打开已发生，不宣称全文件零接触或所有旧二进制均可安全回退。
- 新测试一度错误要求已开连接逐行读立即复核旁表 type；保留该 RED，仅将测试落在实际重开全库准入拒绝点。已开连接不重验 side.type 是原有边界，未借本修复改 events/data_admission。
- 首轮金额负控错返 int，类型错误不能证明账目 detector；原件保留。改用实际 Cost 零对象后，未知占用断言准确失败。分别回退任一事件、镜像漂移、旧 schema 污染、丢缓存、开放 extras、删除 float minimum、重放多发请求也都被实际断言捕获。
- 新 source-copy 版本门禁通过；`cargo fmt --check`、`cargo test --locked`、`cargo clippy --locked --all-targets -- -D warnings` 单次完整完成。仅普通测试编译，不是产品 App build/启动/安装验收。

定向命令沿用[贡献流程](../../CONTRIBUTING.md)的 source-only 父进程入口：清空测试锁/registry 注入变量，设工作树私有数据/权威根、`PYTHONPATH="$PWD/src"`、`PYTHONDONTWRITEBYTECODE=1`、`TIANSHU_CREDENTIAL_BACKEND=env`、`PYTEST_ADDOPTS=''`；`uv run --offline --frozen pytest -o addopts='' -q -rs` 后显式选受影响文件。没有自定义 basetemp、父 startup hook 或真实凭据。`uv lock --offline --check`、冻结 Ruff 和 diff 检查也通过。实际日志与各轮 code/test tree 摘要按协调记录保留，不用本文静态测试总数代替日志。

## 尚未完成

最终整合 code/test tree 仍须独立预审后按授权执行唯一全量，再绑定新 required CI。原真实首组 unsafe、失败、未知金额及未运行组不改判；新候选真实复评尚未执行。旧 `0.1.0` 的原生 RED 不因本地封套结构探针或 CI 绿变成 PASS；原生启动、Keychain、核心任务、安装、升级/回退、备份恢复等原门禁保持。
