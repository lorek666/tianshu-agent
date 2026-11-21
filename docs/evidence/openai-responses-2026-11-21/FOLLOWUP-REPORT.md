# Responses 冻结候选独立复审：仍阻断

**结论：`blocker`，不能判 `PASS`。** 首轮 10 项原探针在新候选上全部通过；14 项新增探针中 10 项通过、4 项失败。四个失败断言对应下列两个根因。按首轮六项发现的原义判定，五项已修，尝试计数部分修复；另外发现父 Run 等待子任务时的投影重建缺陷。

日期：2026-11-21。第二读者：独立工作树 `responses-independent-review`。共同基线 `71eb57ef1544d126cc929b9172f5463a14999359`；首轮候选 `8637a4f5bf7eca31ced99bdce3bb5fdb6007a965`，Git tree `2ddd8ebd35bbc7dbb0b13c738bfbe57cba0028e8`；本次候选 `6e9bf340f7c7fe728f0eb19f44818b799ad1369d`，Git tree `c7a973da0f17d007abb4a6709f184094349a6359`。本轮逐文件核对了两候选间的 5 个产品文件和 1 个测试文件，首轮证据保留在 `REPORT.md` 与 `SHA256SUMS`。

## 阻断项

1. **P1，未修：一个合法挂起的父 Run 就能让整库 `rebuild_from_events()` 失败。** 使用 Responses 发出 `subtask.spawn`，走实际审批、创建子任务并正常挂起。原游标完整，摘要与 `run.cursor` 一致；事件中第 36 条是游标，第 37 条才是 `job.parked(waiting_children)`。实际写入顺序是清空 `model_turn` → 保存游标 → 挂起作业（`src/tianshu/loop.py:713`）。重建器却见到 `job.parked` 才清空，在此之前已按旧值校验游标并拒绝（`src/tianshu/_responses_rebuild.py:94`、`:109`）。探针核对了所有更早的游标，最后两份游标之间唯一变化的字段就是 `model_turn`。投影完好时整库重建抛 `TianshuError`；删除该 Run 的两张派生表行后直接 `resume_run` 也抛同错，新模型调用为零。没有观察到重复生成或写盘。修复应保留摘要校验，让重建按实际存盘顺序解释同一事务里的挂起和游标。
2. **P2，部分修：控制面首次生成在提交前崩溃，恢复时把一次尝试算了两遍。** 真实 `ControlPlane.handle("run.start", ...)` 只入队，不预存初始游标（`src/tianshu/control.py:625`）。子进程在 `model-result-before-commit` 通过生产故障点退出 77，无需删除任何行。恢复后只有两条 `model.intent`，尝试编号为 `[1, 2]`，新传输只发一次；但 `RunResult` 与最终游标都精确报告 **3**，`run.end.unbilled_model_intents` 仍是正确的 1。无保存点时重建器已经把旧 intent 算入返回的进度（`src/tianshu/_responses_rebuild.py:35`、`:112`）；恢复入口把序号边界置为 0，又加一遍（`src/tianshu/loop.py:512`）。错误计数进入游标摘要后，已正常结束的 Run 也会使整库重建失败。修复应让重建器与恢复入口对同一条 intent 只计一次。没有观察到重复收费。

## 首轮发现逐项复核

1. **已修，已提交终态且派生投影丢失。** 删除 `run_cursors`、`run_turns` 后直接恢复，零次新请求，结果 `noop`。此结论不包括上面的挂起父 Run。
2. **已修，终态暂停后切模。** 切到另一种模型后仍正常收尾 `noop`，新模型调用为零。
3. **已修，待审批提案的身份与摘要。** 显式重建前后的完整游标相等，`request_id`、`approval_id` 均保留，`claim_epoch` 同为 1，摘要匹配。独立扩展：同一模型回合先读、后写，写操作已批准；在真实 `after_result` 子进程硬退后，保存点为 16，工具结果为 27。直接恢复和先显式重建再恢复都只将两个结果按原顺序各交给模型一次；同一工具写 intent、receipt 各一条，文件内容正确，离线 replay 通过。
4. **已修，首轮三个 SSE 冲突。** 未知 added 状态、前后不同的非空 reasoning 密文、不同的 `arguments.done.name` 都被拒绝。独立补测 `arguments.done.call_id`：仅改变该字段即抛 `ModelError`，值一致时同一帧序列正常得到 `tool_use`。
5. **已修，中立提案与续接 JSON 类型。** C3 v2 写入校验拒绝 `true` 与 `1`。嵌套对象及数组内的 `true`/`1`、`0`/`false`、`"1"`/`1` 也拒绝；字段顺序改变但类型与值一致则接受。
6. **部分修，提交前崩溃的真实尝试计数。** 存在保存点时，原例得到 2 次；新增两回合用例中，最后游标前后各有一次 intent，恢复再发一次。保留或删除派生行均得到 3 次，与事件一致。缺少首次保存点的真实控制面路径仍失败，见 P2。

## 证据及复现

工作目录为独立工作树根 `/Users/ronfee/.codex/worktrees/responses-independent-review/天枢Agent`。使用真实 `tests/conftest.py` 隔离 authority 与维护锁，使用临时事件库、测试工作区、合成 env 凭据和进程内受控 transport。`after_result`、`model-result-before-commit` 均经真实子进程 `os._exit(77)`，父进程另开数据库；没有编辑事件。删行测试仅删除指定 Run 的可重建投影。

```sh
E=.omc/artifacts/independent-responses-review
export UV_OFFLINE=1 PYTHONPATH=tests TIANSHU_CREDENTIAL_BACKEND=env TIANSHU_API_KEY=review-only
uv run --frozen python -m pytest -p conftest -o addopts= -s -q --tb=short "$E/test_independent.py"
uv run --frozen python -m pytest -p conftest -o addopts= -s -q --tb=short "$E/test_followup.py"
uv run --frozen python -m pytest -p conftest -o addopts= -s -q --tb=short "$E/test_followup_impact.py"
```

- `followup-original.log`：`10 passed`，退出 0。首轮探针文件 SHA-256 仍为 `5d46fd8bcd3f231668512b43c474a26003100e6f141f41fda47e01b207e24f93`。
- `followup-new.log`：`2 failed, 10 passed`，退出 1。关键输出：`no_checkpoint drop False last cursor seq 0 intents 2 reported 3 saved 3`；`parked: durable cursor seq 36 park seq 37 original digest matches True`。
- `followup-impact.log`：`2 failed`，退出 1。分别证明错误计数导致整库重建失败、挂起父 Run 缺投影后无法直接恢复。失败均为产品断言，没有 `xfail`、跳过或故意吞错。
- 两候选增量摘要：`git -c core.quotePath=false diff --no-ext-diff --no-textconv --binary 8637a4f..6e9bf34 | shasum -a 256`，SHA-256 `4ab44a01f6ea6e0f57e584480248431c53a6607f0cee73cceadd323bf388d76d`。在本报告所在目录分别执行 `shasum -a 256 -c SHA256SUMS`、`shasum -a 256 -c FOLLOWUP-SHA256SUMS` 即可复核原证据和本轮证据。

## 未判事项

本轮是修复增量的离线复审。没有运行全量测试、远端 CI、官方或网关付费 smoke，没有使用真实钥匙串、用户设备或实际断电，也没有核对远端 PR 当前状态。首轮关于线上工具名字符限制等未判事项保持原口径。没有读取作者自查日志，没有修改跟踪文件；作者的测试总数不作为本报告证据。
