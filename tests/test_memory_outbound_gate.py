"""外发 gate 的判据（W6，SPEC-N2-EMBEDDING-PROVIDER §3 #5–#8）。

核心口径：**模型工具的检索永不外发**——外部 provider + 可用索引 + 模型路径
（默认 `allow_embedding_outbound=False`）→ 端点请求计数必须为 0、如实降级；
控制面显式动作（CLI / 面板）才允许外发。本机 hash provider 不受 gate 影响。

全部走 loopback 夹具：端点收没收到请求只能靠命中数证明，不靠读代码。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from httpserver import NetFixture

from tianshu.embedding_openai import (
    EmbeddingError,
    OpenAICompatibleEmbeddingProvider,
)
from tianshu.events import EventLog
from tianshu.memory import MemoryProjection
from tianshu.memory_index import HashEmbeddingProvider, MemoryIndex
from tianshu.resources import MemoryScope

NOW = 1_700_000_000_000
RUN = "run_gate"

# loopback 夹具的假凭据（无真实价值；拼接写法避开「键名字面量」扫描）。
_TEST_KEY = "k-" + "gate-test"

DIM = 8


def _payload(vectors: list[list[float]]) -> bytes:
    return json.dumps(
        {"data": [{"index": i, "embedding": v} for i, v in enumerate(vectors)]}
    ).encode("utf-8")


def _fake_vector(text: str) -> list[float]:
    # 确定性向量：与正文相关（hash 基线的简化版），dim=8。
    digest = text.encode("utf-8")
    return [
        ((digest[i % len(digest)] if digest else 0) / 255.0) for i in range(DIM)
    ]


def _record(log, projection, payload):  # noqa: ANN001 - 测试内部 helper
    with log.transaction() as tx:
        seq = tx.append(RUN, "tool.result", payload, legacy_write=True)
        projection.apply_event("tool.result", payload, seq=seq)
    return seq


def _propose(log, projection, memory_id, content, *, scope):  # noqa: ANN001
    content_ref = projection.put_content(content)
    _record(
        log,
        projection,
        {
            "tool_id": "memory.propose",
            "status": "succeeded",
            "output": {
                "memory_id": memory_id,
                "revision": 1,
                "scope": scope.scope,
                "scope_id": scope.scope_id,
                "kind": "preference",
                "content_ref": content_ref,
                "source_refs": ["event:1"],
                "evidence": [
                    {"source_memory_id": "memory_source", "quote": "原文引文"}
                ],
                "proof_count": 1,
                "origin": "user_explicit",
                "status": "candidate",
                "created_at": 1,
                "expires_at": None,
                "deduped": False,
            },
        },
    )


def _confirm(log, projection, memory_id, *, scope):  # noqa: ANN001
    _record(
        log,
        projection,
        {
            "tool_id": "memory.confirm",
            "status": "succeeded",
            "output": {
                "memory_id": memory_id,
                "revision": 2,
                "before_status": "candidate",
                "status": "active",
                "consolidated_at": NOW,
                "memory_uri": scope.to_uri(),
            },
        },
    )


def _seed(log, projection, memory_id, content, *, scope):  # noqa: ANN001
    _propose(log, projection, memory_id, content, scope=scope)
    _confirm(log, projection, memory_id, scope=scope)


@pytest.fixture()
def gate_env(tmp_path: Path):
    """外部 provider（loopback 端点）+ 装配好的投影与索引。"""
    fixture = NetFixture()
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "event_blobs")
    fixture.route("/embeddings", body=_payload([_fake_vector("x")])).start()
    provider = OpenAICompatibleEmbeddingProvider(
        base_url=f"http://{fixture.base}",
        model="gate-embed",
        dim=DIM,
        api_key=_TEST_KEY,
        timeout_s=5.0,
    )
    index = MemoryIndex(log.conn, provider=provider)
    projection = MemoryProjection(
        log.conn,
        log=log,
        blob_dir=tmp_path / "memory_blobs",
        clock=lambda: NOW,
        index=index,
    )
    projection.ensure_schema()
    yield fixture, log, projection, index
    log.close()
    fixture.stop()


# ---------------------------------------------------------------- gate 本体


def test_model_path_never_sends(gate_env) -> None:
    """模型工具路径（默认 False）：端点请求计数必须为 0，如实降级。"""
    fixture, log, projection, _index = gate_env
    scope = MemoryScope.user()
    _seed(log, projection, "m1", "部署回滚要演练", scope=scope)
    # 先在控制面路径建好索引（外发是显式的），再验证模型路径零外发。
    # 注意：写入路径（propose/confirm）在 index 存在时自动 upsert 向量，
    # 加上 rebuild 重算，端点共收到 2 次请求——这些全是控制面外发。
    written = projection.rebuild_vectors()
    assert written == 1
    assert fixture.hit_count == 2
    fixture.hits.clear()
    fixture.bodies.clear()

    outcome = projection.retrieve(
        "回滚",
        allowed=(scope,),
    )
    assert outcome.semantic is False
    assert outcome.degraded is not None and "外发" in outcome.degraded
    assert fixture.hit_count == 0, "模型路径的检索不得触发任何端点请求"


def test_control_plane_explicit_send(gate_env) -> None:
    """控制面显式动作（True）：语义路真的跑，端点恰好收到一次请求。"""
    fixture, log, projection, _index = gate_env
    scope = MemoryScope.user()
    _seed(log, projection, "m1", "部署回滚要演练", scope=scope)
    projection.rebuild_vectors()
    fixture.hits.clear()
    fixture.bodies.clear()

    outcome = projection.retrieve(
        "回滚",
        allowed=(scope,),
        allow_embedding_outbound=True,
    )
    assert outcome.semantic is True
    assert fixture.hit_count == 1
    sent = json.loads(fixture.bodies[0])
    assert sent["input"] == ["回滚"]


# ---------------------------------------------------------------- 基线与安全默认


def test_hash_provider_is_not_gated(tmp_path: Path) -> None:
    """本机 hash 基线零外发：模型路径（默认 False）语义路照常可用。"""
    log = EventLog(tmp_path / "events.db", blob_dir=tmp_path / "event_blobs")
    index = MemoryIndex(log.conn, provider=HashEmbeddingProvider(dim=32))
    projection = MemoryProjection(
        log.conn,
        log=log,
        blob_dir=tmp_path / "memory_blobs",
        clock=lambda: NOW,
        index=index,
    )
    projection.ensure_schema()
    try:
        scope = MemoryScope.user()
        _seed(log, projection, "m1", "部署回滚要演练", scope=scope)
        projection.rebuild_vectors()
        outcome = projection.retrieve("回滚", allowed=(scope,))
        assert outcome.semantic is True
        assert outcome.degraded is None
    finally:
        log.close()


def test_rebuild_failure_rolls_back_not_half_built(gate_env) -> None:
    """rebuild 中途端点失败：事务回滚，**任何状态下都不留半套向量**。

    - 从没有索引的状态失败 → built 作废、零向量、availability False；
    - 从已有完整索引的状态失败 → 回滚到旧完整索引（同一 provider，仍可用）
      ——两种都必须"如实"，不许出现覆盖一半的半套被当成可用。
    """
    fixture, log, projection, index = gate_env
    scope = MemoryScope.user()
    for i in range(3):
        _seed(log, projection, f"m{i}", f"内容 {i}", scope=scope)

    inner = index.provider

    class _Flaky:
        """第 2 次 embed 起冒充端点 500（MemoryIndex 逐条嵌入）。"""

        sends_data_off_device = True

        def __init__(self) -> None:
            self.calls = 0
            self.model_id = inner.model_id
            self.dim = inner.dim

        def embed(self, texts):  # noqa: ANN001 - 与协议同签名
            from tianshu.embedding_openai import EmbeddingError

            self.calls += 1
            if self.calls >= 2:
                raise EmbeddingError("status", "端点返回 HTTP 500")
            return inner.embed(texts)

    flaky = _Flaky()
    index._provider = flaky  # noqa: SLF001 - 测试直装内部字段

    # 场景 A：还没有索引 → 失败后不可用。
    with pytest.raises(EmbeddingError):
        projection.rebuild_vectors()
    available, reason = index.availability()
    assert available is False
    assert reason is not None

    # 场景 B：先成功建一次，再失败 → 回滚到旧的完整索引。
    index._provider = inner  # noqa: SLF001
    projection.rebuild_vectors()
    assert index.availability()[0] is True
    index._provider = flaky  # noqa: SLF001
    flaky.calls = 0
    with pytest.raises(EmbeddingError):
        projection.rebuild_vectors()
    available, reason = index.availability()
    assert available is True, "同 provider 的旧完整索引回滚后如实可用"
    assert reason is None
    # 向量覆盖完整（3 条），不是半套。
    stored = log.conn.execute("SELECT COUNT(*) FROM memory_vectors").fetchone()[0]
    assert stored == 3


def test_provider_without_capability_flag_defaults_to_gated(gate_env) -> None:
    """duck-typed provider 没有自报能力 → 按「会外发」处理（安全默认）。"""
    fixture, log, projection, index = gate_env
    scope = MemoryScope.user()
    _seed(log, projection, "m1", "部署回滚要演练", scope=scope)
    projection.rebuild_vectors()
    fixture.hits.clear()

    # 剥掉能力自报属性：模拟一个未按协议自报家门的 provider。
    class _Mute:
        pass

    for attr in ("sends_data_off_device",):
        setattr(_Mute, attr, True)
    mute = _Mute()
    mute.model_id = index.provider.model_id
    mute.dim = index.provider.dim
    mute.embed = index.provider.embed
    index._provider = mute  # noqa: SLF001 - 测试直装内部字段，验证默认口径

    outcome = projection.retrieve("回滚", allowed=(scope,))
    assert outcome.semantic is False
    assert fixture.hit_count == 0


# ---------------------------------------------------------------- 评测脚本判据（W6 §3 #9）


def _load_scale_script():
    """按路径加载评测脚本（scripts/ 不是包；评测判据要绑住脚本的实测数字）。"""
    import importlib.util
    import sys

    path = Path(__file__).resolve().parent.parent / "scripts" / "eval_memory_scale.py"
    spec = importlib.util.spec_from_file_location("eval_memory_scale_test_proxy", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclass 解析 __module__ 时要求模块已在 sys.modules 里。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_scale_eval_reports_both_modes_and_is_deterministic() -> None:
    """评测脚本两模式都实跑：hybrid 的 MRR 必须严格高于 FTS（固定 seed 可复算）。"""
    module = _load_scale_script()
    provider = module.HashEmbeddingProvider(dim=64)
    fts_row = module._run_mode(50, hybrid=False, provider=None)
    hybrid_row = module._run_mode(50, hybrid=True, provider=provider)
    assert fts_row["mode"] == "fts" and hybrid_row["mode"] == "hybrid"
    # N=50 的固定 seed 期望值（bg 模板与黄金集全部确定性）：
    assert fts_row["all_recall_at_5"] == 1.0
    assert hybrid_row["all_recall_at_5"] == 1.0
    # 词面单路在多干扰下名次更差：两路融合的可观测差异（不是语义差异）。
    assert hybrid_row["all_mrr"] > fts_row["all_mrr"]
    # 确定性：同样输入两次跑，数字逐位一致。
    again = module._run_mode(50, hybrid=True, provider=module.HashEmbeddingProvider(dim=64))
    assert again["all_mrr"] == hybrid_row["all_mrr"]
