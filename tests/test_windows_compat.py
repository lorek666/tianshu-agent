"""Windows 兼容：字节精确 + 控制台编码（issue #21）。

这一组测试有两个用途，缺一不可：

1. **在 POSIX 上证明"产品只按字节理解文件"**：证据键是 `sha256(原始字节)`
   （`observe_state` / 快照 preimage / 凭据 before|after_state），任何经过文本
   模式的读写都会让 Windows 上的哈希与 POSIX 不同，进而让审批的 `preconditions`
   失效。
2. **在 Windows CI 上把同一份断言再跑一遍**：期望值是**从字节算出来的**，不是
   从平台行为算出来的。所以 Windows job 绿 == "两个平台对同一份内容得到同一个
   sha256"，这不是靠 POSIX 上的绿灯推断出来的。

编码那组同理：Windows 控制台代码页（cp1252 / cp936）编不了中文，CLI 必须自己
把输出钉成 UTF-8；这里用 `PYTHONIOENCODING` 在**任何平台**都能复现那个代码页，
所以不必等 Windows job 才有反馈。
"""

from __future__ import annotations

import ast
import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest
from support import CHILD_TEXT, write_exact

from tianshu.events import EventLog
from tianshu.loop import Limits, run
from tianshu.model import ModelResponse, ScriptedModel, ToolCall
from tianshu.receipt import receipts_of
from tianshu.snapshot import SnapshotStore

SRC = Path(__file__).resolve().parents[1] / "src"
BODY = "第一行\n第二行\n没有换行的最后一行"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- 字节精确


def test_fixture_helper_writes_lf_bytes_on_every_platform(tmp_path):
    """夹具助手本身必须字节精确：Windows 上 `write_text` 会写 CRLF。"""
    target = tmp_path / "f.md"
    write_exact(target, "a\nb\n")
    raw = target.read_bytes()
    assert raw == b"a\nb\n", f"夹具写出了被翻译过的字节：{raw!r}"
    assert b"\r" not in raw


def test_product_writes_exact_bytes_and_hashes_the_same_on_both_platforms(tmp_path):
    """产品落盘不经换行翻译，且哈希就是 `sha256(LF 字节)`。

    Windows job 跑这条时，若产品任何一层走了文本模式，写入内容就会变 CRLF，
    `read_bytes()` 与 `after_state` 会同时偏移——两个断言都会红。
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    log = EventLog(tmp_path / "e.db", blob_dir=tmp_path / "blobs")
    store = SnapshotStore(tmp_path / "artifacts")
    try:
        result = run(
            task="写一份中文报告",
            workspace_dir=ws,
            out_name="out.md",
            model=ScriptedModel([
                ModelResponse("tool_use", tool_calls=[ToolCall(
                    "c1", "create_report", {"name": "out.md", "content": BODY})]),
                ModelResponse("end_turn", content="完成"),
            ]),
            log=log,
            limits=Limits(4, 5_000),
            snapshots=store,
        )

        raw = (ws / "out.md").read_bytes()
        assert raw == BODY.encode("utf-8"), (
            "落盘字节必须与内容逐字节相同："
            f"实际 {raw[:40]!r}（含 \\r 就说明经过了 newline 翻译）"
        )
        assert b"\r" not in raw

        expected = _sha(BODY.encode("utf-8"))
        receipt = receipts_of(log, result.run_id)[0]
        assert receipt["after_state"] == expected
        assert receipt["action"] == "create"
        # 快照 evidence 也必须是同一份字节的哈希（删除/覆盖路径用 before_state）
        for change in store.load(result.run_id):
            assert change.expected_after == expected
    finally:
        log.close()


def test_modify_and_undo_round_trip_keeps_bytes_exact(tmp_path):
    """改再撤，字节必须原样回来：证据链里任何文本模式都会破坏这个往返。"""
    original = "原始\n两行\n"
    ws = tmp_path / "ws"
    ws.mkdir()
    write_exact(ws / "note.md", original)
    log = EventLog(tmp_path / "e.db", blob_dir=tmp_path / "blobs")
    store = SnapshotStore(tmp_path / "artifacts")
    try:
        from support import approve_pending, engine_for, stack_for

        limits = Limits(4, 5_000)
        model = ScriptedModel([
            ModelResponse("tool_use", tool_calls=[ToolCall(
                "c1", "modify_text", {"path": "note.md", "content": "改后\n"})]),
            ModelResponse("end_turn", content="完成"),
        ])
        result = run(
            task="改文件", workspace_dir=ws, out_name="out.md", model=model,
            log=log, limits=limits, snapshots=store,
        )
        while result.state == "waiting_approval":
            stack = stack_for(log, snapshots=store)
            assert approve_pending(stack, result.run_id)
            result = engine_for(stack, model, limits=limits).resume_run(result.run_id)

        assert (ws / "note.md").read_bytes() == "改后\n".encode()

        report = store.undo(
            result.run_id,
            guard=lambda: None,
            allow_direct_write=True,
        )
        assert report.ok, report.problems
        assert (ws / "note.md").read_bytes() == original.encode("utf-8")
    finally:
        log.close()


def _http_open_calls(tree: ast.AST) -> set[ast.Call]:
    """只识别紧邻 build_opener 赋值的 HTTP with 调用，不按变量名豁免。"""
    calls: set[ast.Call] = set()
    for parent in ast.walk(tree):
        for _, statements in ast.iter_fields(parent):
            if not isinstance(statements, list):
                continue
            for previous, current in zip(statements, statements[1:], strict=False):
                if not (isinstance(previous, ast.Assign) and len(previous.targets) == 1
                        and isinstance(previous.targets[0], ast.Name)
                        and isinstance(previous.value, ast.Call)
                        and ast.unparse(previous.value.func) == "urllib.request.build_opener"
                        and isinstance(current, ast.With)):
                    continue
                call = current.items[0].context_expr
                if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                        and call.func.attr == "open" and isinstance(call.func.value, ast.Name)
                        and call.func.value.id == previous.targets[0].id):
                    calls.add(call)
    return calls


def _text_mode_file_violations(source: str, filename: str) -> list[str]:
    tree = ast.parse(source, filename=filename)
    http_calls = _http_open_calls(tree)
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in ("write_text", "read_text"):
            violations.append(f"{filename}:{node.lineno} .{node.attr}()")
        if not isinstance(node, ast.Call) or node in http_calls:
            continue
        func = node.func
        mode_index: int | None = None
        label = "open"
        if isinstance(func, ast.Name) and func.id == "open":
            mode_index = 1
        elif isinstance(func, ast.Attribute) and func.attr == "fdopen":
            mode_index, label = 1, "os.fdopen"
        elif isinstance(func, ast.Attribute) and func.attr == "open":
            receiver = func.value
            if isinstance(receiver, ast.Name) and receiver.id == "os":
                continue  # fd 层接口，没有文本模式
            if isinstance(receiver, ast.Name) and receiver.id == "io":
                mode_index, label = 1, "io.open"
            else:
                mode_index, label = 0, "Path.open"
        if mode_index is None:
            continue
        mode = None
        if len(node.args) > mode_index and isinstance(node.args[mode_index], ast.Constant):
            mode = node.args[mode_index].value
        for kw in node.keywords:
            if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                mode = kw.value.value
        if not isinstance(mode, str) or "b" not in mode:
            violations.append(f"{filename}:{node.lineno} {label}(mode={mode!r})")
    return violations


@pytest.mark.parametrize("source, count", [
    ("opener = urllib.request.build_opener(handler)\n"
     "with opener.open(req, timeout=1) as response:\n    response.read()", 0),
    ("opener = Path('file')\nwith opener.open() as stream:\n    pass", 1),
    ("opener = urllib.request.build_opener(handler)\n"
     "opener = Path('file')\nwith opener.open() as stream:\n    pass", 1),
    ("opener = urllib.request.build_opener(handler)\n"
     "with opener.open(req) as response:\n    Path('file').open()", 1),
    ("opener = urllib.request.build_opener(handler)\n"
     "with opener.open(req) as response, Path('file').open() as stream:\n    pass", 1),
    ("opener = urllib.request.build_opener(handler)\n"
     "def other(opener):\n    return opener.open()", 1),
    ("open('file')\nPath('file').open('w')\nio.open('file')\n"
     "os.fdopen(fd)\nPath('file').write_text('x')\nPath('file').read_text()", 6),
    ("open('file', 'rb')\nPath('file').open('wb')\nio.open('file', 'rb')\n"
     "os.fdopen(fd, 'wb')\nos.open('file', os.O_RDONLY)", 0),
])
def test_file_io_guard_distinguishes_http_and_keeps_file_negative_controls(source, count):
    assert len(_text_mode_file_violations(source, "fixture.py")) == count


def test_product_never_uses_text_mode_file_io():
    """产品代码里不许出现文本模式的文件读写（新增一处就会让哈希随平台漂移）。

    `Path.write_text` / `Path.read_text` 一律禁止；任何文本模式的文件读写都禁止。
    逐个形状识别（mode 参数的位置不一样）：

    - 内建 `open(path, mode, ...)` → 第 2 个参数是 mode；
    - `Path.open(mode, ...)` → 第 1 个参数；
    - `io.open(path, mode, ...)` → 第 2 个参数；
    - `os.fdopen(fd, mode, ...)` → 第 2 个参数；
    - `os.open(path, flags, ...)` 不在此列：它是 fd 接口，本来就没有文本模式。
    """
    violations: list[str] = []
    for path in sorted((SRC / "tianshu").glob("*.py")):
        violations.extend(_text_mode_file_violations(
            path.read_text(encoding="utf-8"), path.name,
        ))
    assert violations == [], (
        "产品侧文件读写必须字节进字节出（文本模式会在 Windows 上翻 CRLF，"
        f"导致内容哈希与 POSIX 不一致）：{violations}"
    )


# ---------------------------------------------------------------- 控制台编码


def _run_cli(argv: list[str], *, codepage: str):
    """在"Windows 代码页"下跑一次 CLI，返回 (returncode, stdout, stderr)。

    用 `PYTHONIOENCODING` 在任意平台复现 Windows 的 charmap 行为：它就是
    Python 决定 stdout/stderr 编码的那个开关。CP936 的机器上 `print("中文")`
    会抛 `UnicodeEncodeError`，而 CLI 的输出全是中文。
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = codepage
    env.pop("TIANSHU_TEST_MODEL", None)
    env["PYTHONPATH"] = str(SRC)
    proc = subprocess.run(
        [sys.executable, "-m", "tianshu", *argv],
        capture_output=True,
        timeout=120,
        env=env,
    )
    return proc


@pytest.mark.parametrize("codepage", ["cp1252", "cp936"])
def test_cli_prints_chinese_under_legacy_codepage(tmp_path, codepage):
    """Windows 代码页下 CLI 打印中文不许崩，输出必须是可解码的 UTF-8。"""
    proc = _run_cli(
        ["task", "create", "--goal", "整理发票", "--json",
         "--db", str(tmp_path / "e.db")],
        codepage=codepage,
    )
    stdout = proc.stdout.decode("utf-8")
    stderr = proc.stderr.decode("utf-8")
    assert proc.returncode == 0, (
        f"{codepage} 下 CLI 退出码 {proc.returncode}；stderr={stderr!r}"
    )
    assert "整理发票" in stdout, stdout

    # 失败路径同样会打中文（stderr/stdout 都要检查）
    proc = _run_cli(
        ["undo", "--run-id", "run_missing", "--db", str(tmp_path / "e.db")],
        codepage=codepage,
    )
    out = (proc.stdout + proc.stderr).decode("utf-8")
    assert proc.returncode == 1, f"{codepage} 下退出码 {proc.returncode}：{out!r}"
    assert "没有可撤销的变更" in out, out


@pytest.mark.parametrize("codepage", ["cp1252", "cp936"])
def test_cli_help_is_utf8_under_legacy_codepage(tmp_path, codepage):
    """`--help` 也是中文：它在 `main()` 里最早被打印，最容易漏掉。"""
    proc = _run_cli(["--help"], codepage=codepage)
    stdout = proc.stdout.decode("utf-8")
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    assert "天枢" in stdout or "用法" in stdout, stdout[:400]


def test_child_process_helper_decodes_utf8():
    """测试自己的子进程助手按 UTF-8 解码，而不是按 locale。

    Windows 上 `text=True` 不带 encoding 会用 cp1252 解码：中文输出要么变成
    mojibake，要么在 cp1252 的未定义字节上抛 UnicodeDecodeError。这条钉住
    `support.CHILD_TEXT` 的行为（它是所有子进程断言的共同底座）。
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, "-c", "print('中文输出')"],
        capture_output=True,
        env=env,
        **CHILD_TEXT,  # type: ignore[arg-type]
    )
    assert proc.returncode == 0
    assert proc.stdout == "中文输出\n", repr(proc.stdout)
