"""M4 评测（可评估）。

`replay` 与 `evaluate` 是**两件事**，PRD §6 红线 10 明令不得混称：

| | replay（审计） | evaluate（评测） |
|---|---|---|
| 是否调模型 | **不调**。从事件日志重建消息序列并比对 `messages_hash` | **调**。换一个模型重新跑 |
| 前提 | 历史日志必须完好 | fixture 必须干净 |
| 回答的问题 | "这份记录能重建当时的终态吗" | "换模型之后，同一批 case 分数变了吗、失败该归给谁" |

`replay` 是 M0 就有的判定器（`tianshu.replay`）；本包只负责 `evaluate`。
它跑真实的 `AgentLoop`、真实的 C4 派发、真实的崩溃恢复，只是把"哪条 case、
用哪个模型、以什么判据算通过"固定下来，让两次运行的分母相同。

最小用法：

    from tianshu.eval import CASES, run_suite, build_report

    suite = run_suite(CASES, model_for=lambda case: my_model, repeat=3, workdir=tmp)
    report = build_report(suite, CASES)
    report["verdict"]  # pass / fail / unsafe

命令行入口见 `tianshu.cli evaluate`。
"""

from .cases import (
    CASE_BY_ID,
    CASES,
    CATEGORIES,
    HAPPY_PATH,
    MIN_PER_CATEGORY,
    SUITE_VERSION,
    Case,
    select_cases,
)
from .checks import Check, CheckResult, Observation, evaluate_checks
from .report import build_report, compare_reports, fixture_digest
from .runner import SuiteRun, run_suite

__all__ = [
    "CASES",
    "CASE_BY_ID",
    "CATEGORIES",
    "HAPPY_PATH",
    "MIN_PER_CATEGORY",
    "SUITE_VERSION",
    "Case",
    "Check",
    "CheckResult",
    "Observation",
    "SuiteRun",
    "build_report",
    "compare_reports",
    "evaluate_checks",
    "fixture_digest",
    "run_suite",
    "select_cases",
]