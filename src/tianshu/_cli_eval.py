"""CLI 回放与评测子命令（内部模块，对外入口见 `tianshu.cli`）。

承载 `replay`（审计，不调模型）与 `evaluate`（换模，干净 fixture）两条独立路径。
注意：`_cmd_evaluate` 在运行期经 `cli._model_from(args)` 动态解析模型，以保持
子进程测试对 `cli._scripted_model` 的 monkeypatch 兼容。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC
from pathlib import Path

from .errors import TianshuError
from .events import EventLog
from .model import Model, ScriptedModel
from .replay import replay


def add_replay_parser(sub: argparse._SubParsersAction) -> None:
    from . import cli

    v = sub.add_parser("replay", help="从事件日志回放并校验")
    v.add_argument("--run-id", required=True)
    v.add_argument("--db", type=Path, default=cli.DEFAULT_DB)
    v.add_argument("--desktop", action="store_true", default=argparse.SUPPRESS,
                   help="使用桌面端默认数据目录")
    v.add_argument("--json", action="store_true")


def add_evaluate_parser(sub: argparse._SubParsersAction) -> None:
    from . import cli

    ev = sub.add_parser(
        "evaluate",
        help="评测：干净 fixture 上换模型跑固定 case，产出可比分数与失败归因；"
             "与 replay（审计、不调模型）是两件事",
    )
    ev.add_argument("--suite", default="default", choices=["default"],
                    help="case 集；当前只有 default（20 条，五类）")
    ev.add_argument("--case", action="append", default=None, dest="cases",
                    help="只跑指定 case id（可重复）；未知 id 直接报错，不静默跳过")
    ev.add_argument("--repeat", type=int, default=3,
                    help="每条 case 运行次数；PRD §10.3 要求 >= 3")
    ev.add_argument("--model", default="scripted",
                    choices=["scripted", "openai-compatible"])
    ev.add_argument("--provider", default=None, choices=list(cli.MODEL_PROVIDERS))
    ev.add_argument("--model-name", default=None)
    ev.add_argument("--base-url", default=None)
    ev.add_argument("--workdir", type=Path, default=None,
                    help="评测工作根；必须不存在或为空（干净 fixture 是自己的前提）")
    ev.add_argument("--keep", action="store_true",
                    help="保留每次运行的工作区、事件库与快照（排查用；默认删掉）")
    ev.add_argument("--baseline", type=Path, default=None,
                    help="基线报告 JSON；给了就做可比性检查与退化/改进归因")
    ev.add_argument("--out", type=Path, default=None, help="把报告 JSON 写到这个路径")
    ev.add_argument("--json", action="store_true")


def _cmd_replay(args: argparse.Namespace) -> int:
    from .restore_access import default_material_paths, preflight_legacy_material_layout

    data_root = args.db.parent
    preflight_legacy_material_layout(args.db, data_root)
    log = EventLog(args.db, blob_dir=default_material_paths(args.db, data_root)[1])
    try:
        report = replay(log, args.run_id)
    finally:
        log.close()

    if args.json:
        print(json.dumps(report.__dict__, ensure_ascii=False, indent=2))
    else:
        print(f"run_id : {report.run_id}")
        print(f"事件数 : {report.events}")
        print(f"轮数   : {report.turns}")
        print(f"校验   : {report.messages_verified} 次 messages_hash")
        print(f"产物   : {report.artifact_sha256 or '（无）'}")
        print(f"结论   : {'通过' if report.ok else '失败'}")
        for p in report.problems:
            print(f"  ! {p}")
    return 0 if report.ok else 1


def _default_eval_workdir() -> Path:
    from datetime import datetime

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return Path(".tianshu/eval") / stamp


def _cmd_evaluate(args: argparse.Namespace) -> int:
    """评测入口。与 replay 的分工写在 `tianshu.eval` 包的 docstring 里。

    退出码：0 = 全绿 / 1 = 有 case 失败 / 2 = 前置条件错 / 4 = unsafe
    （安全门槛一票否决，新增的码就是为了让"不安全"不被读成普通失败）。
    """
    from . import cli
    from .eval import SUITE_VERSION, build_report, compare_reports, run_suite, select_cases
    from .eval.checks import evaluate_checks, run_passed

    if args.repeat < 3:
        raise TianshuError(
            f"PRD §10.3 要求每条 case ≥3 次运行；当前 --repeat={args.repeat}。"
            "要冒烟请直接调库函数，别用评测命令产出「可比分数」。"
        )
    cases = select_cases(args.cases)
    if args.cases and (args.out is not None or args.baseline is not None):
        raise TianshuError(
            "--case 子集只用于冒烟：不接受 --out / --baseline。"
            "子集报告的安全门槛只覆盖选中的几条，拿它当'可比分数'是假证据；"
            "正式评测请跑完整 20 条。"
        )
    workdir = args.workdir or _default_eval_workdir()
    if workdir.exists() and any(workdir.iterdir()):
        raise TianshuError(
            f"评测工作根必须不存在或为空（干净 fixture 是结果可信的前提）：{workdir}"
        )

    if args.model != "scripted" and not any(case.scope == "model" for case in cases):
        # 崩溃 case 恒用离线脚本跑（崩溃点必须确定），拿它给真实模型记分是假证据。
        raise TianshuError(
            "所选 case 全部是 scope=harness（崩溃恢复类），它们始终用离线脚本跑，"
            "不构成对真实模型的评测；请至少选择一条 scope=model 的 case。"
        )

    real_model = cli._model_from(args) if args.model != "scripted" else None

    def model_for(case):
        # 崩溃 case 恒用 case 自带脚本：崩溃点必须确定，见 eval/runner._run_crash。
        if real_model is None or case.crash:
            return ScriptedModel(list(case.script))
        return real_model

    def on_case(case, observations) -> None:
        passes = sum(
            1
            for obs in observations
            if run_passed(evaluate_checks(obs, case.checks))
        )
        print(
            f"  {case.case_id:<3} {passes}/{len(observations)}  {case.title}",
            file=sys.stderr,
        )

    suite = run_suite(
        cases,
        model_for=model_for,
        repeat=args.repeat,
        workdir=workdir,
        keep=args.keep,
        # 身份写"被评测的那个模型"：崩溃 case 恒用离线脚本，不能拿它冒充。
        identity=(
            {"adapter": "scripted", "vendor": None, "model": ""}
            if real_model is None
            else _identity_payload(real_model)
        ),
        on_case=on_case,
    )
    report = build_report(
        suite,
        cases,
        suite_version=SUITE_VERSION,
        selection={
            "complete": args.cases is None,
            "requested": list(args.cases) if args.cases else None,
            "case_count": len(cases),
        },
    )
    report["workspace_retained"] = bool(args.keep)
    comparison = None
    if args.baseline is not None:
        baseline = json.loads(args.baseline.read_bytes().decode("utf-8"))
        comparison = compare_reports(baseline, report)

    payload = {"report": report, "compare": comparison}
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        # 落盘的是**报告本体**，不是上面的信封：这样它可以直接当下一次的
        # `--baseline`，不会被信封的 schema 判成"不可比"。
        args.out.write_bytes(
            (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        )
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_eval_summary(report, comparison)

    if report["verdict"] == "unsafe":
        return 4
    return 0 if report["verdict"] == "pass" else 1


def _identity_payload(model: Model) -> dict[str, str | None]:
    from .model import identity_of

    identity = identity_of(model)
    if identity is None:
        return {"adapter": "unknown", "vendor": None, "model": ""}
    return {
        "adapter": identity.adapter,
        "vendor": identity.vendor,
        "model": identity.model,
    }


def _print_eval_summary(report: dict, comparison: dict | None) -> None:
    model = report["model"]
    vendor = model.get("vendor") or "未知（价格不可认领）"
    print(
        f"M4 评测 · suite {report['suite_version']} · repeat {report['repeat']} · "
        f"模型 {model['adapter']} / {vendor} / {model['model'] or '（未指定）'}"
    )
    print("")
    for category, stats in report["categories"].items():
        print(
            f"  {category:<18} case {stats['cases']:>2}  "
            f"runs {stats['runs']:>3}  "
            f"case 通过率 {stats['pass_rate']:.0%}  "
            f"run 通过率 {stats['run_pass_rate']:.0%}"
        )
    overall = report["overall"]
    print("")
    print(
        f"总分   : {overall['cases_all_green']}/{overall['cases']} 条 case 全绿，"
        f"run 通过率 {overall['run_pass_rate']:.0%}"
    )
    gate = report["safety_gate"]
    print(f"安全门槛：{'通过' if gate['passed'] else '失败（一票否决，不可被其它分数抵消）'}")
    for violation in gate["violations"][:10]:
        print(
            f"  ! {violation['case_id']} run{violation['run_index']} "
            f"{violation['check_id']}：{violation['detail']}"
        )
    if report["attribution"]:
        print("失败归因：" + "，".join(f"{k}={v}" for k, v in report["attribution"].items()))
    for case in report["cases"]:
        if case["pass_rate"] >= 1.0:
            continue
        print(f"  {case['case_id']}（{case['category']}）通过率 {case['pass_rate']:.0%}")
        for run in case["run_details"]:
            if run["passed"]:
                continue
            for failed in run["failed_checks"]:
                print(f"    run{run['run_index']} {failed['check_id']}：{failed['detail']}")
    print(f"结论   : {'pass' if report['verdict'] == 'pass' else report['verdict']}")
    if comparison is not None:
        print("")
        _print_eval_comparison(comparison)


def _print_eval_comparison(comparison: dict) -> None:
    base = comparison["baseline"]
    cand = comparison["candidate"]
    print(
        f"对比   : 基线 {base['model']['adapter']}/{base['model']['model'] or '（未指定）'} "
        f"{base['verdict']} → 本次 {cand['model']['adapter']}/"
        f"{cand['model']['model'] or '（未指定）'} {cand['verdict']}"
    )
    if not comparison["comparable"]:
        print("可比性 : 不可比（不把差异读成进步/退步）")
        for guard in comparison["guards"]:
            print(f"  ! {guard}")
        return
    summary = comparison["summary"]
    print(
        f"可比性 : 可比（suite/repeat/case 定义一致）；"
        f"case 平均通过率 {summary['baseline_case_pass_rate']:.0%} → "
        f"{summary['candidate_case_pass_rate']:.0%}"
    )
    for entry in summary["regressions"]:
        print(
            f"  退化 {entry['case_id']}（{entry['category']}）"
            f"{entry['baseline_pass_rate']:.0%} → {entry['candidate_pass_rate']:.0%}，"
            f"失败判据={entry['candidate_failures']}，归因={entry['candidate_attribution']}"
        )
    for entry in summary["improvements"]:
        print(
            f"  改进 {entry['case_id']}（{entry['category']}）"
            f"{entry['baseline_pass_rate']:.0%} → {entry['candidate_pass_rate']:.0%}"
        )
    if not summary["regressions"] and not summary["improvements"]:
        print("  逐条 case 无变化")
