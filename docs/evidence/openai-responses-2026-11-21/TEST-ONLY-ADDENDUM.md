# 仅测试变更的独立复核附录

**结论：PASS。** 版本断言修正为 **fixed / 已修**，没有部分修复或未修项。上一轮 `FINAL-REPORT.md` 对产品实现的独立 PASS 可沿用到本提交：两棵 Git 树中除目标测试外的所有路径均无差异。本复核不是新候选全量测试或远端 CI 的通过证明。

- 本轮对象：`fccd469325558dae9bc176a88f766b777614f217`，Git tree `d6eb0cf9757ad07efa36771388b8e654f0c14eba`。
- 直接父提交、已审产品对象：`b7c3773a7dcd310aafe765f2c456313a59742a2c`，Git tree `eed4d8fd25f9b25745cb8a2469fbc794a0faeaab`。
- 独立工作树 `responses-independent-review`，2026-11-21。完整增量仅 `tests/test_low_disk_run_creation.py`，3 行新增、1 行删除。二进制 diff 的 SHA-256：`01276b3b48c7eee5cc98de5999a092dd55b052ad404539fc65370e6e3f1fdb47`。排除该文件后 `git diff --exit-code` 返回 0。

## 核对与负控

1. 批准的 `docs/model-usage-v2-contract.md` §2.1 要求 `model.result`、`run.model_turn` 的**所有新写**使用 v2，其余事件不跟随升版。新测试逐名断言这两个事件为 v2，保留 `run.start`、`run.end` 的原 v2 断言；遍历真实 `EVENT_TYPES` 中其余每个事件并要求 v1。既没有把模型事件直接跳过，也没有将断言放宽为接受多个版本。
2. `run.start` 的历史版本集合、v1 不含 `execution_policy`、非法模式拒绝、新写旁表精确记录 v2 等后续断言均未改动。独立运行目标测试、两模型事件 v0/v1 的固定 schema 摘要、历史缓存字段封闭性、C3 JSON 镜像、缺省历史版本和损坏版本拒绝等针对性测试：**13 passed，退出 0**。`test-only-targeted.log` 为原始输出。
3. 红绿及反例：用 `git show` 读取上一提交的真实测试函数，在未变的产品实现上执行，复现旧测试第 23 行的 `assert all(...)` 失败。随后在本进程内逐一替换 `event_schema.current_version` 对单个事件的返回值，每次直接调用新候选的真实 `test_run_start_v2_only`。四个指定 v2 事件各自降至 1 或升至 3，均在对应的具名断言失败；其余 41 个事件分别升至 2，全部在第 25 行的通用断言失败。共 **49/49** 次错误被拒绝，均在写库前失败。仅替换当前版本查询的返回值；历史读取另由上项真实 schema 与 EventLog 测试覆盖。详见 `test-only-negative.log`。

## 复现

以独立工作树根为工作目录：

```sh
git diff --exit-code b7c3773..fccd469 -- . ':(exclude)tests/test_low_disk_run_creation.py'
git diff --no-ext-diff --no-textconv --binary b7c3773..fccd469 | shasum -a 256
export UV_OFFLINE=1 TIANSHU_CREDENTIAL_BACKEND=env TIANSHU_API_KEY=review-only
uv run --frozen python -m pytest -o addopts= -q --tb=short \
  tests/test_low_disk_run_creation.py::test_run_start_v2_only \
  tests/test_responses_contract.py::test_historical_schema_identity_is_frozen \
  tests/test_responses_contract.py::test_model_result_v2_preserves_frozen_v0_v1_and_cache_usage \
  tests/test_responses_contract.py::test_new_cache_field_alone_cannot_expand_historical_or_model_turn_schema \
  tests/test_responses_usage.py::test_c3_json_preserves_both_historical_usage_schemas \
  tests/test_event_schema.py::test_historical_row_without_version_record_reads_as_v0 \
  tests/test_event_schema.py::test_corrupt_version_row_is_explicit_unknown
```

用以下命令可独立重做版本变异断言（对应上文 49 项）：

```sh
PYTHONPATH=tests uv run --frozen python - <<'PY'
import tempfile
import traceback
from pathlib import Path
from unittest.mock import patch
from test_low_disk_run_creation import test_run_start_v2_only as check
from tianshu import event_schema
from tianshu.events import EVENT_TYPES

v2_lines = {"run.start": 21, "run.end": 22, "model.result": 23, "run.model_turn": 24}
real = event_schema.current_version
with tempfile.TemporaryDirectory() as tmp:
    for name in sorted(EVENT_TYPES):
        for wrong in ((1, 3) if name in v2_lines else (2,)):
            with patch.object(event_schema, "current_version",
                              lambda key: wrong if key == name else real(key)):
                try:
                    check(Path(tmp))
                except AssertionError as err:
                    last = traceback.extract_tb(err.__traceback__)[-1]
                    assert last.name == check.__name__ and last.lineno == v2_lines.get(name, 25)
                else:
                    raise AssertionError((name, wrong))
    assert not list(Path(tmp).iterdir())
PY
```

本轮未读取作者自查或账本，未改跟踪文件，未重跑前三份产品探针、全量或线上 CI，也未查询远端状态。原三份报告与其证据保留；在本附录目录运行 `shasum -a 256 -c TEST-ONLY-SHA256SUMS` 校验本次附录、日志与上一轮校验清单。
