"""F12 凭据：接缝语义 / 三态区分 / 全链路脱敏。

这个文件里最重要的不是"能拿到 key"，而是两件事：

1. **unsupported 与"没配置"必须分得开**（一个是 `CredentialUnsupported`，
   一个是 `None`）。合并它们，用户会去找一个不存在的环境变量。
2. **明文 key 不得进入任何持久化路径**：事件库的字节、CLI 输出、repr。
   事件日志是永久证据，写进去就擦不掉。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tianshu import event_schema
from tianshu.credentials import (
    ENV_VARS,
    KEYCHAIN_CAPABILITIES,
    MASK,
    CredentialRef,
    EnvCredentialStore,
    KeychainCredentialStore,
    Secret,
    UnsupportedCredentialStore,
    default_store,
    redact,
    register_secret,
)
from tianshu.errors import CredentialUnsupported, CredentialWriteRefused
from tianshu.events import EventLog
from tianshu.loop import Limits, build_stack
from tianshu.model import ModelError, OpenAICompatibleModel
from tianshu.tasks import RUN_QUEUED
from tianshu.worker import Worker

#: 形如真实 OpenAI key 的假值。选这么长的原因：短于 8 字符的值不进替换表，
#: 太短的假值证明不了"能抹掉"。
FAKE_KEY = "sk-proj-FAKE0000deadbeef1234567890abcdef"


# ---------------------------------------------------------------- 分域键


def test_ref_formats_and_hashes():
    ref = CredentialRef("openai", "llm")
    assert str(ref) == "openai/llm"
    assert ref == CredentialRef("openai", "llm")
    # 要能当 dict 键用：后端映射表就是 dict
    assert {ref: "got"}[CredentialRef("openai", "llm")] == "got"
    # 分域是实质的：同 provider 不同能力不是同一个键
    assert CredentialRef("openai", "llm") != CredentialRef("openai", "embedding")


@pytest.mark.parametrize(
    "provider,capability",
    [("", "llm"), ("openai", ""), ("OpenAI", "llm"), ("openai ", "llm")],
)
def test_ref_rejects_bad_shape(provider, capability):
    """`OpenAI` 与 `openai` 若算两个域，表现是"明明配了却读不到"。"""
    with pytest.raises(ValueError):
        CredentialRef(provider, capability)


# ---------------------------------------------------------------- 环境变量后端


def test_env_store_reads_and_returns_secret():
    store = EnvCredentialStore({"TIANSHU_API_KEY": FAKE_KEY})
    got = store.get(CredentialRef("openai", "llm"))
    assert got is not None
    assert isinstance(got, Secret)
    # 取值路径必须是真值：请求头靠 f-string 拼，掩码会变成 401
    assert f"Bearer {got}" == f"Bearer {FAKE_KEY}"
    assert str(got) == FAKE_KEY


def test_env_store_absent_is_none_not_empty_string():
    """未配置 = None。空串会被 `f"Bearer {k}"` 拼成一个合法的坏请求。"""
    store = EnvCredentialStore({})
    assert store.get(CredentialRef("anthropic", "llm")) is None


def test_env_store_blank_value_counts_as_absent():
    """`KEY=` 是很常见的写法，它不是一个凭据。"""
    store = EnvCredentialStore({"TIANSHU_API_KEY": ""})
    assert store.get(CredentialRef("openai", "llm")) is None


def test_env_store_unknown_domain_is_unsupported_not_unconfigured():
    """没登记过变量名 ≠ 没配置：这是"我们没为这个域提供后端"。

    返回 None 会让用户去翻一个根本不存在的环境变量。
    """
    store = EnvCredentialStore({"SOMETHING": FAKE_KEY})
    with pytest.raises(CredentialUnsupported) as excinfo:
        store.get(CredentialRef("moonshot", "llm"))
    assert "moonshot/llm" in str(excinfo.value)


def test_env_store_is_read_only():
    """环境变量是进程环境的镜像：写它不会持久化，假装成功就是撒谎。"""
    store = EnvCredentialStore({})
    with pytest.raises(CredentialWriteRefused):
        store.set(CredentialRef("openai", "llm"), FAKE_KEY)
    with pytest.raises(CredentialWriteRefused):
        store.delete(CredentialRef("openai", "llm"))


def test_env_store_declares_it_is_not_secure_storage():
    caps = EnvCredentialStore({}).capabilities
    assert caps.implemented is True
    assert caps.supports_write is False
    assert caps.secure_storage is False, "环境变量不是安全存储，别在能力里撒谎"


# ---------------------------------------------------------------- Keychain（未实现）


def test_keychain_is_declared_unimplemented():
    caps = KeychainCredentialStore().capabilities
    assert caps.implemented is False
    assert caps.secure_storage is True
    assert caps == KEYCHAIN_CAPABILITIES


@pytest.mark.parametrize("method", ["get", "set", "delete"])
def test_keychain_raises_unsupported_everywhere(method):
    store = KeychainCredentialStore()
    ref = CredentialRef("openai", "llm")
    with pytest.raises(CredentialUnsupported) as excinfo:
        if method == "get":
            store.get(ref)
        elif method == "set":
            store.set(ref, FAKE_KEY)
        else:
            store.delete(ref)
    assert "未实现" in str(excinfo.value)


def test_keychain_does_not_silently_fall_back_to_env(monkeypatch):
    """最关键的一条：选了 keychain 就**不许**偷偷用环境变量。

    静默降级会把"以为在 Keychain 里"变成"其实在环境里"，
    两者的安全属性差一个数量级。
    """
    monkeypatch.setenv("TIANSHU_API_KEY", FAKE_KEY)
    monkeypatch.setenv("TIANSHU_CREDENTIAL_BACKEND", "keychain")
    with pytest.raises(CredentialUnsupported):
        OpenAICompatibleModel("gpt-4o-mini")


def test_unsupported_store_carries_its_reason():
    store = UnsupportedCredentialStore("wincred", "这条路径没有实现")
    with pytest.raises(CredentialUnsupported) as excinfo:
        store.get(CredentialRef("openai", "llm"))
    assert "wincred" in str(excinfo.value)
    assert "这条路径没有实现" in str(excinfo.value)


# ---------------------------------------------------------------- 后端选择


def test_default_store_is_env(monkeypatch):
    monkeypatch.delenv("TIANSHU_CREDENTIAL_BACKEND", raising=False)
    assert isinstance(default_store(), EnvCredentialStore)


def test_default_store_honours_keychain(monkeypatch):
    monkeypatch.setenv("TIANSHU_CREDENTIAL_BACKEND", "keychain")
    assert isinstance(default_store(), KeychainCredentialStore)


def test_default_store_rejects_unknown_backend(monkeypatch):
    monkeypatch.setenv("TIANSHU_CREDENTIAL_BACKEND", "magic")
    with pytest.raises(CredentialUnsupported):
        default_store()


# ---------------------------------------------------------------- 脱敏原语


def test_secret_repr_is_masked_but_value_is_real():
    secret = Secret(FAKE_KEY)
    assert repr(secret) == f"Secret({MASK})"
    assert FAKE_KEY not in repr(secret)
    assert secret == FAKE_KEY  # 取值仍然正确
    # 容器 repr / 对象 repr 里也必须是掩码
    assert FAKE_KEY not in repr({"key": secret})


def test_register_and_redact_exact_value():
    value = "plain-secret-value-1234"
    register_secret(value)
    assert redact(f"boom: {value} end") == "boom: *** end"


@pytest.mark.parametrize(
    "text",
    [
        f"Authorization: Bearer {FAKE_KEY}",
        f"x-api-key: {FAKE_KEY}",
        f"api_key={FAKE_KEY}",
        f"leaked {FAKE_KEY} here",
        "https://user:hunter2pass@api.example.com/v1",
    ],
)
def test_redact_patterns(text):
    out = redact(text)
    assert FAKE_KEY not in out
    assert "hunter2pass" not in out


def test_redact_is_idempotent():
    text = f"Authorization: Bearer {FAKE_KEY}"
    once = redact(text)
    assert redact(once) == once


def test_redact_leaves_ordinary_text_alone():
    text = "模型调用失败：HTTP 500，已重试 2 次"
    assert redact(text) == text


# ---------------------------------------------------------------- 错误对象与 repr


def test_model_error_message_is_redacted_at_construction():
    err = ModelError(f"provider rejected {FAKE_KEY}", kind="auth")
    assert FAKE_KEY not in str(err)
    assert FAKE_KEY not in repr(err)


def test_http_error_detail_echoing_the_key_is_redacted():
    """代理站把 Authorization 回显在响应体里，是最现实的泄露路径。"""
    body = json.dumps({"error": f"invalid api key: Bearer {FAKE_KEY}"})
    err = ModelError.from_http(401, body)
    assert FAKE_KEY not in str(err)
    assert FAKE_KEY not in repr(err)


def test_model_repr_masks_the_key():
    model = OpenAICompatibleModel("gpt-4o-mini", api_key=FAKE_KEY)
    assert FAKE_KEY not in repr(model)
    assert MASK in repr(model)
    assert FAKE_KEY not in repr(vars(model))


# ---------------------------------------------------------------- 不进 prompt


class _CapturingTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, dict]] = []

    def post(self, url, *, body, headers, timeout):  # noqa: ARG002
        self.calls.append((url, body, headers))
        return {
            "choices": [{"message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }


def test_key_goes_in_header_never_in_body():
    """C1 请求里不许带 key：它只在请求头，不在 messages/body。"""
    transport = _CapturingTransport()
    model = OpenAICompatibleModel("gpt-4o-mini", api_key=FAKE_KEY, transport=transport)
    model.complete([{"role": "user", "content": "你好"}], [])
    url, body, headers = transport.calls[0]
    assert FAKE_KEY not in json.dumps(body, ensure_ascii=False)
    assert FAKE_KEY not in url
    assert headers["Authorization"] == f"Bearer {FAKE_KEY}"


# ---------------------------------------------------------------- 端到端：事件库


class _EchoingTransport:
    """把 Authorization 原样回显进错误详情——最坏情况的端点。"""

    def post(self, url, *, body, headers, timeout):  # noqa: ARG002
        raise ModelError.from_http(
            401, json.dumps({"error": f"bad key: {headers['Authorization']}"})
        )


def _enqueue_one_run(stack, ws: Path) -> str:
    task = stack.tasks.create_task("跑一次", [str(ws)])
    run = stack.tasks.start_run(task.task_id, payload={"task": "跑一次", "out": ""})
    stack.claims.enqueue(run.run_id)
    stack.tasks.transition(run.run_id, RUN_QUEUED, reason="enqueued")
    return run.run_id


def test_plaintext_key_never_reaches_the_event_log(tmp_path):
    """真实的 run + 真实的 worker + 最坏的端点：事件库里搜不到明文。

    这是本工单真正的价值：现在如果 API 报错把 key 带进错误信息，
    它会**永久**写进事件日志。
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    data = tmp_path / "authority"
    data.mkdir()
    db = tmp_path / "events.db"
    log = EventLog(db, blob_dir=tmp_path / "blobs")
    stack = build_stack(log, data_root=data)
    try:
        run_id = _enqueue_one_run(stack, ws)
        model = OpenAICompatibleModel(
            "gpt-4o-mini", api_key=FAKE_KEY, transport=_EchoingTransport()
        )
        worker = Worker(stack, model=model, limits=Limits(4, 10_000))
        worker.run_once()
        worker.close()

        events = log.read_run(run_id)
        types = [e.type for e in events]
        assert "model.error" in types, "这条 run 应该以 model.error 结束（否则测不到）"

        # 1) 事件对象里没有明文
        dumped = json.dumps(
            [{"type": e.type, "payload": e.payload} for e in events],
            ensure_ascii=False,
        )
        assert FAKE_KEY not in dumped

        # 2) 打码确实发生过（不是"错误压根没记"）
        errors = [e.payload for e in events if e.type == "model.error"]
        assert any(MASK in str(p.get("error", "")) for p in errors), errors
        # 3) worker 自报的错误里也没有明文
        assert FAKE_KEY not in json.dumps(worker.errors, ensure_ascii=False)
    finally:
        log.close()

    # 4) 最硬的一条：直接扫整个 SQLite 文件的字节（含 payload_hash/索引）
    raw = db.read_bytes()
    assert FAKE_KEY.encode() not in raw, "明文 key 落进了事件库文件"


def test_event_db_actually_can_contain_text_so_the_grep_is_meaningful(tmp_path):
    """反向对照：确认上面那个"扫字节"的测法不是永远为真。

    如果 SQLite 把 payload 压缩/编码到搜不到，上面那条就会变成空断言——
    先用一个非密钥字符串证明它确实能搜到。
    """
    db = tmp_path / "probe.db"
    log = EventLog(db, blob_dir=tmp_path / "blobs")
    sentinel = "SENTINEL-NOT-A-SECRET-abcdef"
    # payload 必须满足 run.start 的逐类型契约（C3 §5.3.5）：拿最小合法样例承载
    # 哨兵字符串，断言（明文出现在 SQLite 字节里）保持不变。
    payload = {**event_schema.minimal_payload("run.start"), "task": sentinel}
    log.append("run_x", "run.start", payload, actor_kind="core")
    log.close()
    assert sentinel.encode() in db.read_bytes()


# ---------------------------------------------------------------- 端到端：CLI


def test_cli_error_output_is_redacted(monkeypatch, capsys, tmp_path):
    """CLI 边界打印异常时必须打码：终端输出经常被贴进 issue。"""
    from tianshu import cli

    register_secret(FAKE_KEY)

    def boom(_args):
        raise ModelError(f"认证失败，服务端回显：Bearer {FAKE_KEY}", kind="auth")

    monkeypatch.setattr(cli, "_cmd_run", boom)
    rc = cli.main([
        "run", "--task", "试一下",
        "--workspace", str(tmp_path),
        "--db", str(tmp_path / "e.db"),
    ])
    captured = capsys.readouterr()
    assert rc == 2, captured.err
    assert FAKE_KEY not in captured.err
    assert FAKE_KEY not in captured.out
    assert MASK in captured.err


def test_cli_reads_key_through_the_store_not_os_environ(monkeypatch):
    """adapter 经凭据接缝取 key：换成不支持的后端时，构造阶段就失败。

    这条保证"接缝是真的"——如果哪天有人图省事改回 `os.environ.get`，
    它会静默成功，这个测试就会红。
    """
    monkeypatch.setenv("TIANSHU_CREDENTIAL_BACKEND", "keychain")
    captured = _CapturingTransport()
    with pytest.raises(CredentialUnsupported):
        OpenAICompatibleModel("gpt-4o-mini", transport=captured)


# ---------------------------------------------------------------- 与环境变量表的一致性


def test_env_var_table_matches_documented_providers():
    """`ENV_VARS` 是唯一的环境变量名真相；别处不许再手写这些名字。"""
    assert ENV_VARS[("openai", "llm")] == "TIANSHU_API_KEY"
    assert ENV_VARS[("anthropic", "llm")] == "ANTHROPIC_API_KEY"
    # 每一条都要能被环境变量后端读到（表与实现不能漂移）
    refs = [CredentialRef(p, c) for p, c in ENV_VARS]
    store = EnvCredentialStore({})
    for ref in refs:
        assert store.get(ref) is None
    assert len(refs) == len(set(refs))
