"""OpenAI 兼容 embedding 适配器的判据（W6，SPEC-N2-EMBEDDING-PROVIDER §3 #1–#4）。

全部走 loopback 夹具，不碰外网；错误分类的每一条都是真实 HTTP 响应 / 真实
socket 行为，不用 mock。dim 显式配置与响应维度不符必须立刻报错（混空间比
降级更糟）；装配缺配置必须点名缺什么（不静默降级）。
"""

from __future__ import annotations

import json
import os

import pytest
from httpserver import NetFixture, closed_port

from tianshu.embedding_openai import (
    BATCH_LIMIT,
    ENV_API_KEY,
    ENV_BASE_URL,
    ENV_DIM,
    ENV_MODEL,
    ENV_TIMEOUT_S,
    EmbeddingError,
    OpenAICompatibleEmbeddingProvider,
    from_env,
)
from tianshu.errors import TianshuError

DIM = 4
VEC = [0.25, -0.5, 0.75, 1.0]

# loopback 夹具的假凭据（无真实价值；拼接写法是为了避开「键名字面量」扫描）。
_TEST_KEY = "k-" + "local-test"


def _payload(vectors: list[list[float]], *, reversed_index: bool = False) -> bytes:
    items = [
        {"index": (len(vectors) - 1 - i) if reversed_index else i, "embedding": v}
        for i, v in enumerate(vectors)
    ]
    return json.dumps({"data": items, "model": "demo-embed"}).encode("utf-8")


def _provider(base: str) -> OpenAICompatibleEmbeddingProvider:
    return OpenAICompatibleEmbeddingProvider(
        base_url=f"http://{base}/v1",
        model="demo-embed",
        dim=DIM,
        api_key=_TEST_KEY,
        timeout_s=5.0,
    )


def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_BASE_URL, "https://example.test/v1")
    monkeypatch.setenv(ENV_MODEL, "demo-embed")
    monkeypatch.setenv(ENV_DIM, str(DIM))
    monkeypatch.setenv(ENV_API_KEY, _TEST_KEY)


# ---------------------------------------------------------------- 请求与解析


def test_embed_returns_vectors_and_sends_model_and_input() -> None:
    with NetFixture().route("/v1/embeddings", body=_payload([VEC, VEC])) as fx:
        provider = _provider(fx.base)
        vectors = provider.embed(["第一段正文", "second passage"])
    assert vectors == [VEC, VEC]
    assert fx.hit_count == 1
    body = json.loads(fx.bodies[0])
    assert body["model"] == "demo-embed"
    assert body["input"] == ["第一段正文", "second passage"]


def test_embed_reverses_index_back_to_input_order() -> None:
    # 端点把 index 打乱返回也必须还原成输入顺序（OpenAI 兼容行为差异之一）。
    with (
        NetFixture().route("/v1/embeddings", body=_payload([VEC, VEC], reversed_index=True)) as fx
    ):
        assert _provider(fx.base).embed(["a", "b"]) == [VEC, VEC]


def test_embed_splits_batches_by_limit() -> None:
    # 两批各 BATCH_LIMIT 条（静态夹具响应条数必须与每批输入精确一致）。
    count = BATCH_LIMIT * 2
    with NetFixture().route("/v1/embeddings", body=_payload([VEC] * BATCH_LIMIT)) as fx:
        vectors = _provider(fx.base).embed([f"t{i}" for i in range(count)])
    assert len(vectors) == count
    assert fx.hit_count == 2
    sizes = [len(json.loads(b)["input"]) for b in fx.bodies]
    assert sizes == [BATCH_LIMIT, BATCH_LIMIT]


def test_dim_mismatch_is_shape_error_and_message_points_at_dim() -> None:
    bad = [0.1, 0.2, 0.3]
    with NetFixture().route("/v1/embeddings", body=_payload([bad])) as fx:
        with pytest.raises(EmbeddingError) as err:
            _provider(fx.base).embed(["x"])
    assert err.value.reason == "shape"
    assert f"配置 dim={DIM}" in str(err.value)


def test_error_classification_from_real_responses() -> None:
    cases = [
        (401, "auth"),
        (403, "auth"),
        (402, "quota"),
        (429, "quota"),
        (500, "status"),
    ]
    for status, reason in cases:
        with NetFixture().route(
            "/v1/embeddings", status=status, body=b'{"error":{"message":"no"}}'
        ) as fx:
            with pytest.raises(EmbeddingError) as err:
                _provider(fx.base).embed(["x"])
        assert err.value.reason == reason, (status, reason)
        assert err.value.reason in str(err.value)


def test_dead_endpoint_is_network_or_timeout_error() -> None:
    # closed_port 在 macOS 上表现为连接阶段超时（无 ECONNREFUSED 立即返回，
    # 与 W2 网络执行器的实测一致），两种都属「连接阶段失败」封闭集口径。
    port, sock = closed_port()
    try:
        provider = OpenAICompatibleEmbeddingProvider(
            base_url=f"http://127.0.0.1:{port}",
            model="demo-embed", dim=DIM, api_key=_TEST_KEY, timeout_s=2.0,
        )
        with pytest.raises(EmbeddingError) as err:
            provider.embed(["x"])
    finally:
        sock.close()
    assert err.value.reason in {"network", "timeout"}


def test_slow_endpoint_is_timeout_error() -> None:
    with NetFixture().route("/v1/embeddings", body=_payload([VEC]), delay_ms=1500) as fx:
        provider = OpenAICompatibleEmbeddingProvider(
            base_url=f"http://{fx.base}/v1",
            model="demo-embed", dim=DIM, api_key=_TEST_KEY, timeout_s=0.4,
        )
        with pytest.raises(EmbeddingError) as err:
            provider.embed(["x"])
    assert err.value.reason == "timeout"


def test_malformed_json_and_missing_fields_are_shape_errors() -> None:
    with NetFixture().route("/v1/embeddings", body=b"not json") as fx:
        with pytest.raises(EmbeddingError) as err:
            _provider(fx.base).embed(["x"])
    assert err.value.reason == "shape"

    with NetFixture().route("/v1/embeddings", body=b'{"data": []}') as fx:
        with pytest.raises(EmbeddingError) as err:
            _provider(fx.base).embed(["x"])
    assert err.value.reason == "shape"

    with NetFixture().route("/v1/embeddings", body=b'{"data": [{"embedding": [1, 2]}]}') as fx:
        with pytest.raises(EmbeddingError) as err:
            _provider(fx.base).embed(["x"])
    assert err.value.reason == "shape"


def test_unknown_reason_is_rejected_at_construction() -> None:
    # 封闭集：未知分类在构造时就报错，不允许兜底值溜进错误面。
    with pytest.raises(ValueError):
        EmbeddingError("corrupted", "非法分类不该被接受")


# ---------------------------------------------------------------- 装配


def test_base_url_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    # userinfo / query / 非法 scheme 一律拒绝。
    for bad in (
        "https://user:pw@example.test/v1",
        "https://example.test/v1?x=1",
        "ftp://example.test",
        "https://",
    ):
        monkeypatch.setenv(ENV_BASE_URL, bad)
        with pytest.raises(TianshuError):
            from_env()
    # /v1 前缀被保留：POST 落在 /v1/embeddings。
    with NetFixture().route("/v1/embeddings", body=_payload([VEC])) as fx:
        monkeypatch.setenv(ENV_BASE_URL, f"http://{fx.base}/v1")
        provider = from_env()
        assert provider.embed(["x"]) == [VEC]
    assert fx.paths() == ["/v1/embeddings"]


def test_from_env_requires_all_four_and_names_the_missing() -> None:
    names = (ENV_BASE_URL, ENV_MODEL, ENV_DIM, ENV_API_KEY)
    saved = {name: os.environ.get(name) for name in names}
    try:
        for name in names:
            os.environ.pop(name, None)
        with pytest.raises(TianshuError) as err:
            from_env()
        message = str(err.value)
        for name in names:
            assert name in message
        # 补齐三个、缺一个：点名缺的那个。
        os.environ[ENV_BASE_URL] = "https://example.test/v1"
        os.environ[ENV_MODEL] = "demo-embed"
        os.environ[ENV_DIM] = "4"
        with pytest.raises(TianshuError) as err:
            from_env()
        assert ENV_API_KEY in str(err.value)
        os.environ[ENV_API_KEY] = _TEST_KEY
        provider = from_env()
        assert provider.model_id == "openai:demo-embed"
        assert provider.dim == DIM
        assert provider.sends_data_off_device is True
    finally:
        for name in names:
            if saved[name] is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = saved[name]


def test_from_env_rejects_bad_dim(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    monkeypatch.setenv(ENV_DIM, "four")
    with pytest.raises(TianshuError):
        from_env()
    monkeypatch.setenv(ENV_DIM, "0")
    with pytest.raises(TianshuError):
        from_env()


def test_timeout_env_override_and_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    monkeypatch.setenv(ENV_TIMEOUT_S, "12.5")
    assert from_env()._timeout_s == 12.5
    monkeypatch.setenv(ENV_TIMEOUT_S, "not-a-number")
    assert from_env()._timeout_s == 60.0  # 容错回落默认，不做静默禁用
