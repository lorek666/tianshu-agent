"""MCP 崩溃/恢复判据的负控（可复现脚本）。

用法（仓库根目录）：

    uv run python scripts/negative_controls_mcp_crash.py          # 全部
    uv run python scripts/negative_controls_mcp_crash.py NB21 NB29  # 指定几条

每条负控的做法固定为五步：

1. 把要变异的源文件**快照**到临时目录；
2. 变异（唯一的错一处，其余不动）；
3. 清 `__pycache__` 并设 `PYTHONDONTWRITEBYTECODE=1`（同尺寸同 mtime 秒的改写会
   让 Python 复用旧 pyc → 假绿，PR B 实测踩过）；
4. 跑目标用例，记录返回码 / 耗时 / 失败摘要；
5. 从**快照**还原。

为什么不用 `git checkout -- <file>` 还原：它回到的是 HEAD，会把工作区里**尚未
提交**的改动一起抹掉（2026-11-12 实测抹过一次，靠重放补丁修回）。负控的还原必须
是"变异前自己拍的那份"。

退出码：全部按预期变红返回 0；有任何一条**没有**变红返回 1（这条脚本自己也要能
失败，否则它只是一段打印）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: 目标用例的缩写（负控表与文档里的名字保持一致）。
CRASH = "tests/test_mcp_crash.py::test_crash_after_server_executed_settles_unknown_and_blocks"
BEFORE = (
    "tests/test_mcp_crash.py::"
    "test_crash_before_send_settles_none_applied_and_does_not_block"
)
MARKED = "tests/test_mcp_crash.py::test_crash_after_marker_conservatively_blocks_and_refuses_revoke"
REFUSE = "tests/test_mcp_crash.py::test_refused_send_boundary_does_not_call_server"
CORRUPT = (
    "tests/test_mcp_crash.py::"
    "test_corrupt_manifest_does_not_drag_never_sent_mcp_into_unknown"
)
FALLBACK = (
    "tests/test_mcp_crash.py::"
    "test_mcp_without_a_dispatch_row_stays_conservative"
)
STATIC = (
    "tests/test_mcp_crash.py::"
    "test_mcp_tool_in_static_specs_is_still_not_treated_as_a_file_tool"
)
FILE_ROW = "tests/test_mcp_crash.py::test_recovery_marks_dispatch_row_terminal_for_file_writes"
TERMINAL = "tests/test_mcp_crash.py::test_recovery_does_not_rewrite_terminal_dispatch_rows"


@dataclass(frozen=True)
class Case:
    name: str
    relpath: str
    old: str
    new: str
    tests: tuple[str, ...]
    why: str


CASES: tuple[Case, ...] = (
    Case(
        name="NB21",
        relpath="src/tianshu/recovery.py",
        old="        if tool_id.startswith(MCP_TOOL_PREFIX):\n"
        "            _settle_mcp_pending(",
        new="        if False:\n            _settle_mcp_pending(",
        tests=(STATIC, CRASH),
        why="撤掉恢复的 MCP 分支（同时静态表里放了 MCP id）→ 空清单会被读成"
        "「确定没写」，unknown 变成 none_applied",
    ),
    Case(
        name="NB28",
        relpath="src/tianshu/recovery.py",
        old=(
            "        if tool_id.startswith(MCP_TOOL_PREFIX):\n"
            "            # MCP 没有本地 preimage：唯一判据是"
            "\"请求有没有越过外发边界\"（发送前"
        ),
        new=(
            "        if False:\n"
            "            # MCP 没有本地 preimage：唯一判据是"
            "\"请求有没有越过外发边界\"（发送前"
        ),
        tests=(STATIC,),
        why="只撤 assess_run 的 MCP 分支：正常崩溃用例里 id 不在静态表，仍 unresolved，"
        "测不出来——只有 assess_run 那条断言会红",
    ),
    Case(
        name="NB25",
        relpath="src/tianshu/dispatcher.py",
        old='        failpoint("mcp_after_call")\n',
        new="",
        tests=(CRASH,),
        why="拿掉「响应已收到、结果未落盘」的注入点：崩溃窗口不复存在，rc≠77 当场暴露",
    ),
    Case(
        name="NB26",
        relpath="src/tianshu/recovery.py",
        old='    return DISPATCH_FINISHED if result.effect_state == "applied" else DISPATCH_ERROR',
        new="    return DISPATCH_FINISHED",
        tests=(CRASH, FILE_ROW),
        why="恢复把派发行一律写 done：unknown 也写成成功",
    ),
    Case(
        name="NB27",
        relpath="src/tianshu/recovery.py",
        old="        if not _has_dispatch_table(log):\n            log.append_many(batch)",
        new="        if True:\n            log.append_many(batch)",
        tests=(CRASH, FILE_ROW),
        why="恢复只写 tool.result、不推进派发行（R2 之前的口径）："
        "dispatch_record 会继续显示「仍在执行」",
    ),
    Case(
        name="NB29",
        relpath="src/tianshu/recovery.py",
        old='        " WHERE run_id=? AND call_id=? AND state IN (?, ?)",',
        # 保留两个占位符（否则绑定数不对，炸的是 sqlite 而不是判据），把终态也
        # 放进允许集合 = 等于没有保护。
        new=(
            '        " WHERE run_id=? AND call_id=? AND state IN (?, ?,'
            " 'revoked', 'rejected', 'done', 'error')\","
        ),
        tests=(TERMINAL,),
        why="去掉「只从 intent/executing 推进」的保护：已 revoked/rejected 的派发行"
        "会被改写，审计里丢掉「被撤销/被拒绝过」这条事实",
    ),
    Case(
        name="NB30",
        relpath="src/tianshu/recovery.py",
        old="    proven = {DISPATCH_INTENT, DISPATCH_ROW_REJECTED, DISPATCH_ROW_REVOKED}",
        new="    proven: set[str] = set()",
        tests=(BEFORE,),
        why="把三个「没越过外发边界」的状态清空：恢复拿不到任何本机证据，"
        "窗口一又变回 unknown + 阻断（过度阻断回归）",
    ),
    Case(
        name="NB31",
        relpath="src/tianshu/recovery.py",
        old="    if not states:\n        return False\n",
        new="    return True\n",
        tests=(MARKED, FALLBACK),
        why="把「证明不了没发就按可能已发」翻成「一律没发」：标记已落盘的窗口、"
        "以及没有派发表/没有行的老库，都会被洗成 none_applied——最危险的方向",
    ),
    Case(
        name="NB32",
        relpath="src/tianshu/dispatcher.py",
        old="        if not self._mark_mcp_send_boundary(dispatch_id):\n",
        new="        if False:\n",
        tests=(MARKED, REFUSE),
        why="拿掉发包前的外发边界标记：崩溃在标记之后仍是 intent，恢复会判没发；"
        "「撤销先到就不发」的分界也一起消失（服务器照样被打）",
    ),
    Case(
        name="NB33",
        relpath="src/tianshu/recovery.py",
        old="    proven = {DISPATCH_INTENT, DISPATCH_ROW_REJECTED, DISPATCH_ROW_REVOKED}",
        new="    proven = {DISPATCH_INTENT}",
        tests=(TERMINAL,),
        why="只认 intent、不认 revoked/rejected：已撤销的调用又变回 unknown + 阻断"
        "（拿不出证据却硬要挡）",
    ),
    Case(
        name="NB34",
        relpath="src/tianshu/recovery.py",
        old=(
            "                if tool_id.startswith(MCP_TOOL_PREFIX):\n"
            "                    # MCP 不读这份快照清单：它的判据是外发边界标记。"
        ),
        new=(
            "                if False:\n"
            "                    # MCP 不读这份快照清单：它的判据是外发边界标记。"
        ),
        tests=(CORRUPT,),
        why="清单损坏分支不按 MCP 判：一份无关证据的损毁会把「请求都没写出」的"
        "调用推成 unknown 并挡住目录",
    ),
)


def _clean_pyc() -> None:
    for cache in ROOT.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)


def _run(tests: tuple[str, ...]) -> tuple[int, float, str]:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    _clean_pyc()
    started = time.time()
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            *tests,
            "-o",
            "addopts=",
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=env,
        timeout=900,
    )
    return proc.returncode, round(time.time() - started, 2), proc.stdout + proc.stderr


def run_case(case: Case, workdir: Path) -> tuple[bool, str]:
    target = ROOT / case.relpath
    backup = workdir / f"{case.name}-{target.name}"
    shutil.copyfile(target, backup)
    source = target.read_text(encoding="utf-8")
    hits = source.count(case.old)
    if hits != 1:
        return False, f"变异锚点命中 {hits} 次（要求恰好 1 次）——代码变了，先更新脚本"
    target.write_text(source.replace(case.old, case.new, 1), encoding="utf-8")
    try:
        code, seconds, output = _run(case.tests)
    finally:
        shutil.copyfile(backup, target)

    # "变红"有三条硬条件，缺一条都不算（复审第三轮指出的可骗人处）：
    #   1. pytest 退出码是 1（断言失败）。2/3/4/5 是用法错误、内部错误、
    #      没收集到用例——那些不是"判据变红"，是脚本或夹具坏了；
    #   2. 输出的 FAILED 行命中**本用例声明的目标节点**（不是别的用例替它红）；
    #   3. 有 AssertionError（真断言失败，不是 import/collection 炸）。
    failed = [line for line in output.splitlines() if line.startswith("FAILED ")]
    names = {node.rsplit("::", 1)[-1] for node in case.tests}
    hit = [line for line in failed if any(name in line for name in names)]
    has_assert = "AssertionError" in output
    ok = code == 1 and bool(hit) and has_assert
    if ok:
        reason = "命中目标用例的断言失败"
    elif code != 1:
        reason = f"pytest rc={code}（不是断言失败；2/3/4/5 说明脚本或夹具坏了）"
    elif not hit:
        reason = f"FAILED 行没命中目标节点（实际：{failed[:2]}）"
    else:
        reason = "没有 AssertionError（可能是 import/collection 炸）"
    tail = "\n".join((hit or failed or output.strip().splitlines())[-3:])
    return ok, (
        f"rc={code} {seconds}s（{reason}）\n      "
        + tail.replace(chr(10), chr(10) + "      ")
    )


def _select(argv: list[str]) -> tuple[set[str], list[str]]:
    """挑出要跑的编号；顺带把"不认识的编号"单独交出来（空 = 都认识）。"""
    known = {case.name for case in CASES}
    wanted = set(argv) or set(known)
    return wanted, sorted(wanted - known)


def main(argv: list[str]) -> int:
    wanted, unknown = _select(argv)
    if unknown:
        # 复审第三轮抓到的第二个"静默跳过"入口：写错编号时一个用例都没跑，
        # 却打印"全部按预期变红"并返回 0。这类失败必须自己响。
        print(f"不认识的负控编号：{unknown}（可用：{sorted(c.name for c in CASES)}）")
        return 1
    failed_to_go_red: list[str] = []
    print(f"负控 {len(CASES)} 条（仓库根：{ROOT}）\n")
    with tempfile.TemporaryDirectory(prefix="nc-mcp-crash-") as tmp:
        workdir = Path(tmp)
        for case in CASES:
            if case.name not in wanted:
                continue
            print(f"=== {case.name}：{case.why}")
            red, detail = run_case(case, workdir)
            print(f"      {detail}")
            print(f"      → {'RED（符合预期）' if red else '没有变红（负控失效）'}\n")
            if not red:
                failed_to_go_red.append(case.name)
    if failed_to_go_red:
        print(f"负控失效：{failed_to_go_red}")
        return 1
    print("全部按预期变红。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))