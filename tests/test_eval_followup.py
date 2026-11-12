"""G2 记忆阶段 M2：同一个样本里跑**两次 Run**（`Case.followup`）的评测器验收。

这几条测的是**评测器机制**，不是记忆本身：

1. 两次 Run 共享 `data_root`（同一个事件库 / 记忆投影 / 用户作用域），
   workspace **必须不同**；
2. 证据**分栏**：第二段的事件/产物/回放报告进 `Observation.followup`，
   不混进第一段；
3. 失败归因不能串：第二段没收敛只记 `followup.error`，不污染第一段的 `error`；
4. 硬规矩 fail closed：第二段 workspace 复用一个目录 → 样本判无效，不硬跑；
5. `fixture_digest` 把第二段的输入算进分母（只在非默认时加键）。

记忆任务本身（`RU-M1`）的判据与负控在 `tests/test_eval_real_tasks.py`。
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from tianshu.eval import fixture_digest, run_suite
from tianshu.eval.cases import Case, FollowupRun
from tianshu.eval.checks import artifact_exists, artifact_mentions
from tianshu.model import ModelResponse, ScriptedModel, ToolCall


def _tool(call_id: str, tool_id: str, **arguments: object) -> ModelResponse:
    return ModelResponse(
        finish_reason="tool_use",
        tool_calls=[ToolCall(call_id, tool_id, dict(arguments))],
    )


def _end(content: str = "") -> ModelResponse:
    return ModelResponse(finish_reason="end_turn", content=content)


FIRST_OUT = "summary.md"
SECOND_OUT = "final.md"


def _pair_case(**overrides: object) -> Case:
    """最小可用的一对 Run：第一段写产物，第二段读自己的夹具再写产物。"""
    fields: dict[str, object] = {
        "case_id": "X1",
        "category": "normal",
        "title": "两段 Run 的最小样本",
        "task": "把资料整理成 summary.md",
        "out_name": FIRST_OUT,
        "fixture": {"资料/来源.md": "第一段：CSV 导出，3 秒内完成。\n"},
        "script": (
            _tool("p_read", "read_text", path="资料/来源.md"),
            _tool("p_report", "create_report", name=FIRST_OUT, content="结论：CSV 导出。\n"),
            _end("第一段完成。"),
        ),
        "checks": (artifact_exists(), artifact_mentions("CSV")),
        "max_turns": 4,
    }
    fields.update(overrides)
    return Case(**fields)  # type: ignore[arg-type]


def _followup(**overrides: object) -> FollowupRun:
    fields: dict[str, object] = {
        "task": "把第二份资料整理成 final.md",
        "out_name": SECOND_OUT,
        "fixture": {"资料/会议纪要.md": "第二段：打印延后到 v2。\n"},
        "script": (
            _tool("s_read", "read_text", path="资料/会议纪要.md"),
            _tool("s_report", "create_report", name=SECOND_OUT, content="结论：打印延后。\n"),
            _end("第二段完成。"),
        ),
        "checks": (artifact_exists(), artifact_mentions("打印延后")),
        "max_turns": 4,
    }
    fields.update(overrides)
    return FollowupRun(**fields)  # type: ignore[arg-type]


def _run_one(workdir: Path, case: Case, *, keep: bool = False):
    suite = run_suite(
        (case,),
        model_for=lambda item: ScriptedModel(list(item.script)),
        repeat=1,
        workdir=workdir,
        keep=keep,
    )
    return suite.observations[0]


def test_followup_runs_a_second_run_with_its_own_workspace(tmp_path: Path) -> None:
    case = _pair_case(followup=_followup())
    obs = _run_one(tmp_path, case)
    assert obs.error is None, obs.error
    assert obs.invalid_reason is None, obs.invalid_reason
    assert obs.result is not None and obs.result.terminal
    assert obs.artifact_text.startswith("结论：CSV")
    # 第一段的 workspace 里不该出现第二段的产物（反过来也是）：两次 Run 的
    # 目录必须真的分开，否则"新任务"是假的。
    assert SECOND_OUT not in obs.files
    followup = obs.followup
    assert followup is not None
    assert followup.error is None, followup.error
    assert followup.run_id and followup.run_id != obs.run_id
    assert followup.result is not None and followup.result.terminal
    assert followup.artifact_text.startswith("结论：打印延后")
    assert FIRST_OUT not in followup.files
    # 第二段自己的回放报告也要有（两段各自可回放，不是只看第一段）
    assert followup.replay_report is not None and followup.replay_report.ok


def test_both_runs_share_the_same_data_root(tmp_path: Path) -> None:
    """两段用同一个数据根：事件库里能找到**两条** Run，不是两个库。"""
    from tianshu.events import EventLog

    case = _pair_case(followup=_followup())
    # keep=True：事件库留在盘上才能证明"两段共用一个库"（默认跑完会清目录）。
    obs = _run_one(tmp_path, case, keep=True)
    followup = obs.followup
    assert followup is not None
    log = EventLog(tmp_path / "X1" / "run0" / "events.db")
    try:
        run_ids = {event.run_id for event in log.read_after(0)}
    finally:
        log.close()
    assert {obs.run_id, followup.run_id} <= run_ids
    # 第二段的事件不在第一段的证据里（分栏不是装饰）
    first_ids = {event.seq for event in obs.events}
    second_seqs = {event.seq for event in followup.events if event.run_id == followup.run_id}
    assert second_seqs, "第二段没有事件"
    assert not second_seqs <= first_ids


def test_followup_failure_is_attributed_to_the_second_run_only(tmp_path: Path) -> None:
    """第二段没收敛：记在 `followup.error`，不污染第一段与样本本身。"""
    # 第二段去删一个文件 → 需要审批；approval="none" 时评测器判「case 与实现不一致」。
    broken = _followup(
        fixture={"资料/会议纪要.md": "第二段：打印延后到 v2。\n", "资料/临时.txt": "临时\n"},
        script=(
            _tool("s_del", "delete_file", path="资料/临时.txt"),
            _end("想删临时文件。"),
        ),
    )
    case = _pair_case(followup=broken)
    obs = _run_one(tmp_path, case)
    assert obs.error is None, "第一段没问题，不该被第二段的失败牵连"
    assert obs.result is not None and obs.result.terminal
    followup = obs.followup
    assert followup is not None
    assert followup.error is not None and "审批" in followup.error, followup.error


def test_followup_refuses_to_reuse_the_first_workspace(tmp_path: Path) -> None:
    """硬规矩：第二段 workspace 不能等于第一段——同一个目录跑两次不是新任务。"""
    case = _pair_case(followup=_followup(workspace="ws"))
    obs = _run_one(tmp_path, case)
    assert obs.followup is None
    assert obs.invalid_reason is not None
    assert "第二段取证失败" in obs.invalid_reason
    assert "workspace" in obs.invalid_reason


def test_followup_is_not_run_when_the_first_run_never_finishes(tmp_path: Path) -> None:
    """第一段停在审批且审批策略是 hold：第二段不跑，并把原因写清楚。"""
    held = _pair_case(
        fixture={"资料/来源.md": "内容\n", "资料/要删.txt": "删我\n"},
        script=(
            _tool("p_del", "delete_file", path="资料/要删.txt"),
            _end("想删文件。"),
        ),
        approval="hold",
        checks=(),
        followup=_followup(),
    )
    obs = _run_one(tmp_path, held)
    assert obs.result is not None and not obs.result.terminal
    followup = obs.followup
    assert followup is not None
    assert followup.first_run_terminal is False
    assert followup.run_id == ""
    assert followup.error is not None and "第一段没有跑到终态" in followup.error


def test_followup_inputs_are_part_of_the_denominator(tmp_path: Path) -> None:
    """第二段的任务/夹具/脚本/判据变了 → 这条 case 不可比。"""
    base = _pair_case(followup=_followup())
    same = _pair_case(followup=_followup())
    assert fixture_digest(base) == fixture_digest(same)

    variants = [
        _pair_case(followup=_followup(task="换一个第二段任务")),
        _pair_case(followup=_followup(fixture={"资料/会议纪要.md": "换一份纪要\n"})),
        _pair_case(followup=_followup(out_name="other.md")),
        _pair_case(
            followup=_followup(
                script=(_tool("s_read", "read_text", path="资料/会议纪要.md"),)
            )
        ),
        _pair_case(followup=_followup(checks=(artifact_exists(),))),
    ]
    digests = {fixture_digest(variant) for variant in variants}
    assert fixture_digest(base) not in digests
    assert len(digests) == len(variants), "每一项变体都必须产生不同的 digest"

    # 没有 followup 的 case 保持旧口径：digest 里不出现 followup 键。
    without = dataclasses.replace(base, followup=None)
    assert fixture_digest(without) != fixture_digest(base)
