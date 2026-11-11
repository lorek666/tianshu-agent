"""内容引用 + 分页读取 + 任务可见范围（G1e 的接线判据）。

PRD §5.2.3 的验收句：

> 大结果以有界摘要 + 内容引用返回，分页读取引用同样验证任务可见范围。

这一组盯的是**端到端**：工具输出被截断时，模型看到的占位里必须有一个**能用**的
引用；模型用这个引用调 `read_content` 必须能取回原文（分页可续）；**另一个任务**
拿同一个引用必须读不到。存储层的单元判据在 `test_content_refs.py`。
"""

from __future__ import annotations

import json
import re

from tianshu.contents import ContentStore, make_ref, parse_ref
from tianshu.events import EventLog
from tianshu.loop import run
from tianshu.model import ModelResponse, ScriptedModel, ToolCall

BIG_BYTES = 20 * 1024
_REF = re.compile(r"content://sha256:[0-9a-f]{64}")


def _blob_dir(tmp_path):
    """`build_stack` 的 blob 目录：`data_root/artifacts/blobs`（快照与 spill 共用）。"""
    return tmp_path / "artifacts" / "blobs"


def _ws(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    # 20 KiB 的纯 ASCII 文本：稳定、比 spill 阈值（8 KiB）大、便于逐字节核对。
    (ws / "big.md").write_bytes(("a" * 79 + "\n").encode() * 259)
    return ws


class _RefReadingModel(ScriptedModel):
    """脚本模型：第一次读大文件，第二次从占位里取出 ref 读它，第三次续读一页。"""

    def __init__(self) -> None:
        super().__init__([])
        self.seen: list[str] = []
        self.ref: str | None = None
        self.step = 0

    def complete(self, messages, tools):  # noqa: ANN001, ARG002
        self.calls += 1
        self.seen.append(json.dumps(messages, ensure_ascii=False))
        if self.step == 0:
            self.step = 1
            return ModelResponse(
                "tool_use",
                tool_calls=[ToolCall("c1", "read_text", {"path": "big.md"})],
                usage={"input_tokens": 10, "output_tokens": 5},
            )
        if self.step == 1:
            found = _REF.search(self.seen[-1])
            assert found is not None, "占位里没有可用引用，模型取不回原文"
            self.ref = found.group(0)
            self.step = 2
            return ModelResponse(
                "tool_use",
                tool_calls=[
                    ToolCall("c2", "read_content", {"ref": self.ref, "length": 100})
                ],
                usage={"input_tokens": 10, "output_tokens": 5},
            )
        if self.step == 2:
            self.step = 3
            return ModelResponse(
                "tool_use",
                tool_calls=[
                    ToolCall(
                        "c3",
                        "read_content",
                        {"ref": self.ref, "offset": 100, "length": 100},
                    )
                ],
                usage={"input_tokens": 10, "output_tokens": 5},
            )
        return ModelResponse("end_turn", content="读完了", usage={})


def _tool_results(log: EventLog, run_id: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for event in log.read_run(run_id, verify=False):
        if event.type == "tool.result":
            out[event.payload["call_id"]] = event.payload
    return out


def test_spill_placeholder_carries_the_digest_of_the_original_bytes(tmp_path):
    """占位里的 ref 必须指向**原文**的摘要，而不是指向占位文本自己。"""
    ws = _ws(tmp_path)
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    model = _RefReadingModel()
    try:
        result = run(
            task="读大文件", workspace_dir=ws, out_name="out.md", model=model, log=log
        )
        assert model.ref is not None
        digest = parse_ref(model.ref)
        blobs = _blob_dir(tmp_path) / digest
        assert blobs.is_file(), "占位说已落盘，就必须真的有这个 blob"
        import hashlib

        assert hashlib.sha256(blobs.read_bytes()).hexdigest() == digest
        # 这个摘要也确实登记成了本任务可见的内容
        contents = ContentStore(log, blob_dir=_blob_dir(tmp_path))
        task = next(
            task for task in log.conn.execute("SELECT task_id FROM runs WHERE run_id=?",
                                              (result.run_id,)).fetchall()
        )
        assert contents.visible(digest, task["task_id"])
    finally:
        log.close()


def test_model_can_page_through_the_reference(tmp_path):
    ws = _ws(tmp_path)
    log = EventLog(tmp_path / "events.db", blob_dir=_blob_dir(tmp_path))
    model = _RefReadingModel()
    try:
        result = run(
            task="读大文件", workspace_dir=ws, out_name="out.md", model=model, log=log
        )
        assert result.outcome in {"succeeded", "noop"}, result.outcome
        results = _tool_results(log, result.run_id)
        assert results["c1"]["status"] == "succeeded"
        first = results["c2"]
        assert first["status"] == "succeeded", first
        second = results["c3"]
        assert second["status"] == "succeeded", second

        page1 = first["output"]
        page2 = second["output"]
        assert page1["offset"] == 0 and page1["next_offset"] == 100
        assert page2["offset"] == 100 and page2["next_offset"] == 200
        assert page1["total_size"] == page1["total_size"]  # 同一份内容
        assert len(page1["data"]) == 100 and len(page2["data"]) == 100
        assert page1["data"] != page2["data"], "两页不能是同一段"
        # 两页拼起来就是原文的开头 200 字节
        digest = parse_ref(model.ref)
        original = (_blob_dir(tmp_path) / digest).read_bytes()
        assert (page1["data"] + page2["data"]).encode() == original[:200]
    finally:
        log.close()


def test_another_task_cannot_read_the_reference(tmp_path):
    """「分页读取引用同样验证任务可见范围」：跨任务必须读不到。"""
    ws = _ws(tmp_path)
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    first = _RefReadingModel()
    try:
        result_a = run(
            task="读大文件", workspace_dir=ws, out_name="out.md", model=first, log=log
        )
        ref = first.ref
        assert ref is not None
        digest = parse_ref(ref)

        # 第二个任务（同一个库、同一个数据根）拿到同一个 ref 去读
        intruder = ScriptedModel(
            [
                ModelResponse(
                    "tool_use",
                    tool_calls=[ToolCall("x1", "read_content", {"ref": ref})],
                    usage={"input_tokens": 10, "output_tokens": 5},
                ),
                ModelResponse("end_turn", content="读不到", usage={}),
            ]
        )
        result_b = run(
            task="另一个任务", workspace_dir=ws, out_name="out2.md",
            model=intruder, log=log,
        )
        results = _tool_results(log, result_b.run_id)
        assert "x1" in results, results
        assert results["x1"]["status"] == "rejected"
        assert "ContentNotVisible" in results["x1"]["output"]["error"]

        # 但内容本身还在，任务 A 仍然读得到（拒绝的是授权，不是把内容删了）
        contents = ContentStore(log, blob_dir=_blob_dir(tmp_path))
        page = contents.read(
            ref, task_id=log.conn.execute(
                "SELECT task_id FROM runs WHERE run_id=?", (result_a.run_id,)
            ).fetchone()["task_id"]
        )
        assert page.total_size > 0
        assert digest in page.to_payload()["ref"]
    finally:
        log.close()


def test_read_content_rejects_malformed_reference(tmp_path):
    ws = _ws(tmp_path)
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    model = ScriptedModel(
        [
            ModelResponse(
                "tool_use",
                tool_calls=[
                    ToolCall("c1", "read_content", {"ref": "content://sha256:deadbeef"})
                ],
                usage={"input_tokens": 1, "output_tokens": 1},
            ),
            ModelResponse("end_turn", content="", usage={}),
        ]
    )
    try:
        result = run(
            task="坏引用", workspace_dir=ws, out_name="out.md", model=model, log=log
        )
        events = list(log.read_run(result.run_id, verify=False))
        # 非法引用在**准入**阶段就被 schema 拒（G1a 的口径：非法参数不进派发）
        rejected = [
            event for event in events
            if event.type == "tool.not_dispatched"
            and event.payload.get("call_id") == "c1"
        ]
        assert rejected, [e.type for e in events]
        assert "ref" in json.dumps(rejected[0].payload, ensure_ascii=False)
        assert not any(
            event.type == "tool.result" and event.payload.get("call_id") == "c1"
            for event in events
        ), "非法参数不该被执行"
    finally:
        log.close()


def test_read_content_works_without_a_workspace(tmp_path):
    """零目录任务也能读内容引用：它不是文件，硬要它先绑目录等于把"没有目录"
    误判成"没有内容"（与记忆只读工具同一条理由）。"""
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")
    blobs = tmp_path / "blobs"
    blobs.mkdir(exist_ok=True)
    payload = b"hello content"
    import hashlib

    digest = hashlib.sha256(payload).hexdigest()
    (blobs / digest).write_bytes(payload)
    contents = ContentStore(log, blob_dir=blobs)
    contents.record(digest, payload, task_id="task-x", run_id="run-x")
    try:
        page = contents.read(make_ref(digest), task_id="task-x", length=5)
        assert page.data == "hello"
        assert page.next_offset == 5
    finally:
        log.close()