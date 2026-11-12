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
BEFORE = "tests/test_mcp_crash.py::test_crash_before_send_is_indistinguishable_and_still_blocks"
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
        old="        if tool_id.startswith(MCP_TOOL_PREFIX):\n            _settle(",
        new="        if False:\n            _settle(",
        tests=(STATIC, CRASH),
        why="撤掉恢复的 MCP 分支（同时静态表里放了 MCP id）→ 空清单会被读成"
        "「确定没写」，unknown 变成 none_applied",
    ),
    Case(
        name="NB28",
        relpath="src/tianshu/recovery.py",
        old=(
            "        if tool_id.startswith(MCP_TOOL_PREFIX):\n"
            "            # MCP 没有本地 preimage：只读对账无从核实，一律拒绝接手（与"
        ),
        new=(
            "        if False:\n"
            "            # MCP 没有本地 preimage：只读对账无从核实，一律拒绝接手（与"
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
        new='        " WHERE run_id=? AND call_id=?",',
        tests=(TERMINAL,),
        why="去掉「只从 intent/executing 推进」的保护：已 revoked/rejected 的派发行"
        "会被改写，审计里丢掉「被撤销/被拒绝过」这条事实",
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

    # 变红还不够：要确认红在**那条判据**上，不是 import/夹具炸了。
    relevant = [
        line
        for line in output.splitlines()
        if line.startswith("FAILED") or "AssertionError" in line
    ]
    tail = "\n".join(relevant[-3:]) if relevant else "\n".join(
        output.strip().splitlines()[-3:]
    )
    return code != 0, f"rc={code} {seconds}s\n      {tail.replace(chr(10), chr(10) + '      ')}"


def main(argv: list[str]) -> int:
    wanted = set(argv) or {case.name for case in CASES}
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