# main #133 × T1 恢复原子性 fixture：独立有限集成复审

日期：2026-11-13。结论：`PASS_FIXTURE_INTEGRATION_ONLY`。本范围未决 0。

## 绑定范围

基础提交 `91290950099b1202beeb9b34cd2d71af7e945118`。仅核对 `tests/test_recovery_atomicity.py` 的 setup 调整，最终 SHA256 为 `736d77c60f8f1d655867dab8bb7b0a7e85623613c182e8ca8d0402d6b46e7573`。

本读者没有写作者树。独立副本由 `git archive` 精确基础提交后，覆入原九项冻结 T1 文件及最终适配测试构成。九项逐 hash 一致；`src/tianshu/recovery.py` 原样继承基础提交；其外 254 个基础非 docs 文件的字节及执行位一致。Python 3.12.13，实际正负控均在独立 `/tmp/t1-main133-fixture-independent/` 中执行。

## 原失败根因与改动边界

亲读父初次集成 full 原日志，结果为 **4 failed / 2562 passed / 8 skipped / 432.50s**，不当作绿。四个 corrupt 组合在父 setup 创建首个 EventLog 时即触发 `check_new_file_evidence`，尚未启动恢复 child。新准入拒绝的是「新空主库旁已有历史材料」，原 fixture 提前写坏 manifest 与其冲突。

适配先建立真实 EventLog 并 `_seed_intent`，关闭连接后才写 `b"corrupt"`，在 child 启动前新增字节断言。AST 复原比较证明：除这一个顺序移动与新增字节断言外，整个测试模块与基础版本相同。`_CHILD` 原字串、参数装饰器、原 13 条断言、真实硬退 77、恢复调用、幂等断言及 skip 状态均未变化；最终断言数为 14。

## 独立正控与负控

正控精确命令及清洁环境说明见 `runs.json`：

```text
<python3.12> -m pytest tests/test_recovery_atomicity.py::test_unknown_result_and_block_survive_interrupted_recovery_together -k corrupt -o addopts= -q -p no:cacheprovider --basetemp /tmp/t1-main133-fixture-independent/positive-cases --tb=short
```

亲跑结果：**4 passed / 4 deselected / 0.74s**。四参数是 `after_result`、`before_authority` 与 file/MCP 的 corrupt 组合。每例继承测试原有 child 77、事务一致、authority 修复及第三次恢复不增 seq 的断言。独立只读 SQLite 核验也确认：四例均保留坏 manifest 字节、仅一条 `unknown` result、一条 workspace block；MCP 两例另有一条 object block、dispatch=`error`；所有对应 authority 阻断均 active。

负控使用另一独立副本，只把 `recovery.py` 换为真实历史对象：提交 `e89a95d4c7e8f7196b38c51148e294d4c72bed9a`、blob `4414b71384e68359562e5b46e535431e31d44bf2`。最终 fixture 与所有其它对象保持相同，亲跑同四参数，结果：**2 failed / 2 passed / 4 deselected / 0.72s**。

失败参数为 `[after_result-False-corrupt]`、`[before_authority-True-corrupt]`；两者都通过 child 硬退断言后，在原 `assert bool(results) == bool(blocks)` 断言处出现 `True != False`。独立读取留下的真实事件库，两例均确有一条 unknown result、零条 workspace block。故这次 setup 适配仍能揭穿实际历史非原子恢复，不是只验 child 77 或因导入/setup 失败而红。本报告不声称历史实现的四个参数全部失败。

每个测试进程启动前移除 `TIANSHU_*`、以 `BASE_URL` / `API_KEY` 结尾的环境键，再设置临时 data/authority root；conftest 继续按例钉住 authority。未输出任何环境值。

## 证据与限制

机器证据：`/tmp/t1-admission/recovery-fixture-independent-evidence.json`。原始日志、SQLite 事实、AST 对比、最终边界 hash 与精确命令索引均在 `/tmp/t1-main133-fixture-independent/`。

此结论只通过本次 fixture 集成适配。未重跑 full、未跑 CI、未授权 T2、未给正式提交通过结论；父必须把修后 full、CI 与最终提交内容重新绑定。未借作者 8 绿/346 关联结果充作独立测试证据。
