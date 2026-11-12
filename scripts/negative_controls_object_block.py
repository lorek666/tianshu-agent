"""MCP 对象级阻断的负控（可复现脚本）。

用法（仓库根目录）：

    uv run python scripts/negative_controls_object_block.py            # 全部
    uv run python scripts/negative_controls_object_block.py NC-O3 NC-O5  # 指定几条

做法与 `negative_controls_mcp_crash.py` **完全一致**（那是同一套纪律，不是另一
套）：快照源文件 → 只错一处 → 清 `__pycache__` + `PYTHONDONTWRITEBYTECODE=1`
→ 跑目标用例 → **从快照**还原（绝不用 `git checkout -- <file>`：它回到 HEAD，
会把工作区里未提交的改动一起抹掉）。

为什么要给"对象阻断"单独一条负控脚本：这一层新增了 4 条独立的安全线，每一条
失效都不会被"功能正常"的用例发现——

1. 判定阶段的拒绝（弹批准之前）；
2. 外发边界前的复核（执行守卫内，批准期间落下的阻断）；
3. 恢复侧"对象键只从派发行读、绝不反解"；
4. 解除粒度（解目录 ≠ 解对象）。

退出码：全部按预期变红返回 0；有任一没变红返回 1（这个脚本自己也要能失败）。
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
OBJ = "tests/test_mcp_object_block.py::"

RESTORE = OBJ + "test_recovery_registers_object_from_dispatch_target_not_from_tool_id"
NEVER = OBJ + "test_recovery_never_sent_does_not_register_object_block"
NO_ROW = OBJ + "test_recovery_without_dispatch_row_blocks_dir_but_cannot_name_the_object"
BEFORE = OBJ + "test_blocked_object_is_refused_before_any_approval_is_requested"
SERVER = OBJ + "test_server_level_block_covers_every_tool_of_that_server"
PRECISION = OBJ + "test_block_of_another_object_does_not_block_this_one"
APPROVAL = OBJ + "test_block_registered_while_waiting_for_approval_is_refused_before_send"
GUARD = OBJ + (
    "test_send_boundary_guard_recheck_holds_even_if_dispatch_phase_check_is_bypassed"
)
UNBLOCK_WS = OBJ + "test_unblock_workspace_does_not_release_the_object"
CORRUPT = OBJ + "test_corrupt_authority_makes_object_checks_block_not_pass"
OLD_DB = OBJ + "test_old_authority_database_without_objects_table_is_empty_not_broken"
MALFORMED = OBJ + "test_log_replay_block_and_unblock"
IN_RUN = OBJ + "test_in_run_unknown_registers_object_block"
XROOT = OBJ + "test_object_block_is_cross_data_root"
NOT_UNKNOWN = OBJ + "test_blocked_object_refusal_is_a_controlled_rejection_not_unknown"


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
        name="NC-O1",
        relpath="src/tianshu/dispatcher.py",
        old="            self._refuse_blocked_mcp_object(target.server, target.tool)\n",
        new="            pass\n",
        tests=(BEFORE,),
        why="撤掉判定阶段的对象阻断拒绝（弹批准之前那道）：被阻断的调用会弹批准、"
        "批准后真发出去",
    ),
    Case(
        name="NC-O2",
        relpath="src/tianshu/dispatcher.py",
        old="        keys = mcp_block_keys(server, tool)\n"
        "        root = self._data_root",
        new="        keys = ()\n"
        "        root = self._data_root",
        tests=(GUARD,),
        why="让外发边界前的复核拿到空键（等于不检查）：批准期间落下的阻断会被"
        "放过去，请求真的写进 server 的 stdin",
    ),
    Case(
        name="NC-O3",
        relpath="src/tianshu/resources.py",
        old="    return mcp_object_key(server, tool), mcp_server_key(server)",
        new="    return (mcp_object_key(server, tool),)",
        tests=(SERVER,),
        why="只查工具级、不查 server 级：一条 mcp://<server> 的阻断记录对它的"
        "所有工具都不生效（永远读不到的阻断）",
    ),
    Case(
        name="NC-O4",
        relpath="src/tianshu/recovery.py",
        old='        raw = str(row["target"] or "")\n'
        "        if not raw:\n"
        "            continue",
        new='        raw = str(row["target"] or "")\n'
        "        if not raw:\n"
        "            continue\n"
        '        raw = raw.replace("-", "_").replace(".", "_")',
        tests=(RESTORE,),
        why="把派发行 target 里的 -/. 也折叠成 _（等于反解工具 id 的那条错口径）："
        "阻断对象名字变成 srv_a/do_thing，不再是用户配的 srv.a/do-thing",
    ),
    Case(
        name="NC-O5",
        relpath="src/tianshu/recovery.py",
        old="            bucket=report.none_applied,\n"
        "        )\n"
        "        return False",
        new="            bucket=report.none_applied,\n"
        "        )\n"
        "        for object_key in objects:\n"
        "            _block_mcp_object(log, report, run_id, object_key, [call_id])\n"
        "        return False",
        tests=(NEVER,),
        why="「请求没越过外发边界」也登记对象阻断：确定没发的调用被记成「可能"
        "已执行」，过度阻断",
    ),
    Case(
        name="NC-O6",
        relpath="src/tianshu/recovery.py",
        old='    keys: list[str] = []\n    for row in rows:',
        new='    keys: list[str] = [] if rows else ["mcp://srv.a/do-thing"]\n    for row in rows:',
        tests=(NO_ROW,),
        why="没有派发行时编一个对象键：证明不了是哪个对象却登记了阻断（假精确）",
    ),
    Case(
        name="NC-O7",
        relpath="src/tianshu/guard.py",
        old="        if not isinstance(obj, str) or not obj:\n"
        "            malformed.append(int(row[\"seq\"]))\n"
        "            continue",
        new="        if not isinstance(obj, str) or not obj:\n"
        "            continue",
        tests=(MALFORMED,),
        why="畸形对象阻断记录静默跳过（`except: continue` 的同一类错误）："
        "无法判定的记录被当成「没阻断」放行",
    ),
    Case(
        name="NC-O8",
        relpath="src/tianshu/guard.py",
        old="        authority.release_blocked(ws, reason=reason)",
        new="        authority.release_blocked(ws, reason=reason)\n"
        "        for block in authority.active_object_blocks():\n"
        "            authority.release_object_blocked(block.object_key, reason=reason)",
        tests=(UNBLOCK_WS,),
        why="解除 workspace 阻断时顺手把所有对象阻断也解了：核对过工作区文件"
        "就等于核对过 server 侧（本次要消掉的错误口径）",
    ),
    Case(
        name="NC-O9",
        relpath="src/tianshu/guard.py",
        old="    except AuthorityUnavailable as exc:\n"
        "        raise McpObjectBlocked(\n"
        '            f"权威对象阻断状态读不出来（{exc}）：无法确定哪些 MCP 对象可调用，"',
        new="    except AuthorityUnavailable:\n"
        "        return set(scan.blocked)\n"
        "        raise McpObjectBlocked(  # noqa\n"
        '            f"权威对象阻断状态读不出来：无法确定哪些 MCP 对象可调用，"',
        tests=(CORRUPT,),
        why="权威根读不出来时 fail-open（当成「没阻断」）：坏库 / 锁死会让所有"
        "对象阻断凭空消失",
    ),
    Case(
        name="NC-O10",
        relpath="src/tianshu/authority.py",
        old="        if not _table_exists(conn, \"blocked_objects\"):\n            return False",
        new="        if not _table_exists(conn, \"blocked_objects\"):\n            return True",
        tests=(OLD_DB,),
        why="旧版本库没有对象表 → 当成「已阻断」：升级用户第一次用就被自己挡住"
        "（缺表能被证明是空集，不是「不知道」）",
    ),
    Case(
        name="NC-O11",
        relpath="src/tianshu/dispatcher.py",
        old="                self._log.append(**object_event)\n",
        new="                pass\n",
        tests=(IN_RUN,),
        why="运行期 unknown（-32603）不落对象阻断事件：同一个可能已执行的工具"
        "下次还能再发一次",
    ),
    Case(
        name="NC-O12",
        relpath="src/tianshu/guard.py",
        old="        problems += authority.backfill_object_blocked(\n"
        "            sorted(obj_scan.blocked),\n"
        '            reason="启动补登记：本数据根日志里的有效对象阻断",\n'
        "        )",
        new="        pass",
        tests=(XROOT,),
        why="启动补登记不带对象：换个 --db 数据根就看不见「这个工具不许再调」"
        "（issue #61.1 的同类逃逸）",
    ),
    Case(
        name="NC-O13",
        relpath="src/tianshu/dispatcher.py",
        old="        except McpObjectBlocked as exc:\n"
        "            # 等批准/等租约期间这个对象被别的 Run 标成阻断：同样一个字节都没发。\n"
        "            # 按受控拒绝结算（`dispatch.mcp_object_blocked`），不能记成 unknown ——\n"
        '            # "确定没发"被写成"可能发了"会凭空多挡一个目录。\n'
        "            return self._abort_dispatch(request, dispatch_id, exc)\n",
        new="        except McpObjectBlocked as exc:\n"
        "            return self._fail_unknown(request, dispatch_id, target, exc)\n",
        tests=(GUARD,),
        why="守卫内对象阻断被错记成 unknown：确定没发的调用变成「效果未知」"
        "（过度阻断，且结论不诚实；目标用例先绕开第一道门，所以打得到这条处理）",
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

    # 与崩溃负控同一套"变红"判据（缺一条都不算）：rc==1 + FAILED 命中本用例
    # 声明的目标节点 + 有 AssertionError（排除用法错误 / 收集错误）。
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
    known = {case.name for case in CASES}
    wanted = set(argv) or set(known)
    return wanted, sorted(wanted - known)


def main(argv: list[str]) -> int:
    wanted, unknown = _select(argv)
    if unknown:
        print(f"不认识的负控编号：{unknown}（可用：{sorted(c.name for c in CASES)}）")
        return 1
    failed_to_go_red: list[str] = []
    print(f"负控 {len(CASES)} 条（仓库根：{ROOT}）\n")
    with tempfile.TemporaryDirectory(prefix="nc-object-block-") as tmp:
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