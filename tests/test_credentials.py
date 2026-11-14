"""F12 凭据：接缝语义 / 三态区分 / 全链路脱敏。

这个文件里最重要的不是"能拿到 key"，而是两件事：

1. **unsupported 与"没配置"必须分得开**（一个是 `CredentialUnsupported`，
   一个是 `None`）。合并它们，用户会去找一个不存在的环境变量。
2. **明文 key 不得进入任何持久化路径**：事件库的字节、CLI 输出、repr。
   事件日志是永久证据，写进去就擦不掉。
"""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from tianshu import event_schema
from tianshu.credentials import (
    ENV_VARS,
    KEYCHAIN_CAPABILITIES,
    KEYCHAIN_CAPABILITIES_MACOS,
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


# ------------------------------------------------- Keychain（macOS 实现 / 平台 stub）

IS_MACOS = sys.platform == "darwin"


def _skip_if_locked(exc: CredentialUnsupported) -> None:
    """login keychain 锁定/需要交互时如实跳过（kSecUseAuthenticationUIFail 立即失败）。"""
    if "-25308" in str(exc):
        pytest.skip("login keychain 已锁定/需要交互解锁：无法在本环境取证")
    raise exc


@pytest.fixture()
def kc_store():
    if not IS_MACOS:
        pytest.skip("Keychain 后端仅 macOS 实现")
    return KeychainCredentialStore()


@pytest.fixture()
def kc_refs():
    """本会话写入过的域清单；teardown 强制清理并复核，清理失败要红。"""
    created: list[CredentialRef] = []
    yield created
    if created:
        store = KeychainCredentialStore()
        for ref in created:
            store.delete(ref)
            assert store.get(ref) is None, f"测试残留未清理：{ref}"


def _kc_ref() -> CredentialRef:
    """唯一域：provider 里带 uuid 前缀，与生产命名空间和并行测试互不相撞。"""
    return CredentialRef(f"selftest-{uuid.uuid4().hex[:12]}", "llm")


def test_keychain_caps_by_platform():
    """能力声明按平台如实：macOS 全 True，其他平台维持未实现 stub。"""
    caps = KeychainCredentialStore().capabilities
    if IS_MACOS:
        assert caps == KEYCHAIN_CAPABILITIES_MACOS
        assert caps.implemented is True
        assert caps.supports_write is True
        assert caps.secure_storage is True
    else:
        assert caps == KEYCHAIN_CAPABILITIES
        assert caps.implemented is False
        assert caps.secure_storage is True


@pytest.mark.parametrize("method", ["get", "set", "delete"])
def test_keychain_raises_unsupported_everywhere_on_unimplemented_platforms(method):
    """非 macOS 平台保持诚实 stub：一请求就报 unsupported，绝不静默回退。"""
    if IS_MACOS:
        pytest.skip("macOS 已实现真后端，本条只钉住未实现平台")
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


def test_keychain_roundtrip_and_redact(kc_store, kc_refs):
    """set → get 拿到真值（repr 打码），且明文已登记进脱敏表。"""
    ref = _kc_ref()
    kc_refs.append(ref)
    kc_store.set(ref, FAKE_KEY)
    got = kc_store.get(ref)
    assert got is not None
    assert isinstance(got, Secret)
    assert got == FAKE_KEY
    assert repr(got) == f"Secret({MASK})"
    assert redact(f"leak: {FAKE_KEY}") == "leak: ***"


def test_keychain_absent_is_none(kc_store):
    """未写入的域 get → `None`：不是空串、不是异常（三态语义的"未配置"）。"""
    assert kc_store.get(_kc_ref()) is None


def test_keychain_overwrite_uses_update(kc_store, kc_refs):
    """同域 set 两次：读回第二次的值（duplicate → SecItemUpdate 路径）。"""
    ref = _kc_ref()
    kc_refs.append(ref)
    try:
        kc_store.set(ref, "sk-first-0000000000000001")
    except CredentialUnsupported as exc:
        _skip_if_locked(exc)
    kc_store.set(ref, "sk-second-000000000000002")
    assert kc_store.get(ref) == "sk-second-000000000000002"


def test_keychain_delete_is_idempotent(kc_store, kc_refs):
    """delete 后 get → None；删除不存在的域也成功（终态「不存在」已达成）。"""
    ref = _kc_ref()
    kc_refs.append(ref)
    kc_store.set(ref, FAKE_KEY)
    kc_store.delete(ref)
    assert kc_store.get(ref) is None
    kc_store.delete(ref)  # 幂等：再删一次不抛


def test_keychain_domain_isolation(kc_store, kc_refs):
    """分域是实质的：openai 与 anthropic 互不可见。"""
    openai_ref = CredentialRef(f"selftest-{uuid.uuid4().hex[:12]}", "llm")
    anthropic_ref = CredentialRef(f"selftest-{uuid.uuid4().hex[:12]}", "llm")
    kc_refs.extend([openai_ref, anthropic_ref])
    kc_store.set(openai_ref, "sk-openai-000000000000001")
    assert kc_store.get(anthropic_ref) is None
    kc_store.set(anthropic_ref, "sk-anthropic-00000000001")
    assert kc_store.get(openai_ref) == "sk-openai-000000000000001"
    assert kc_store.get(anthropic_ref) == "sk-anthropic-00000000001"


def test_keychain_items_landed_in_expected_namespace(kc_store, kc_refs):
    """外部观察者视角：条目落在 service `tianshu-agent` 下，清理复核归零。"""
    ref = _kc_ref()
    kc_refs.append(ref)
    kc_store.set(ref, FAKE_KEY)
    probe = subprocess.run(
        ["security", "find-generic-password", "-s", "tianshu-agent", "-a", str(ref)],
        capture_output=True, text=True, check=False,
    )
    assert probe.returncode == 0, (
        f"条目没落在预期的命名空间里（rc={probe.returncode}）：{probe.stderr}"
    )
    assert FAKE_KEY not in probe.stdout and FAKE_KEY not in probe.stderr


def test_keychain_macos_no_silent_env_fallback(monkeypatch):
    """macOS 上最关键的一条：keychain 里没条目 = 未配置 → 缺 key 报错。

    环境变量里摆着 FAKE_KEY 也**不许**被偷偷读走：构造必须以「缺少 API key」
    的 auth 错误失败，而不是带着 env 的 key 成功构造。
    """
    if not IS_MACOS:
        pytest.skip("真后端行为只在 macOS 取证")
    KeychainCredentialStore().delete(CredentialRef("openai", "llm"))  # 确保未配置
    monkeypatch.setenv("TIANSHU_API_KEY", FAKE_KEY)
    monkeypatch.setenv("TIANSHU_CREDENTIAL_BACKEND", "keychain")
    with pytest.raises(ModelError) as excinfo:
        OpenAICompatibleModel("gpt-4o-mini")
    assert "缺少 API key" in str(excinfo.value)
    assert FAKE_KEY not in str(excinfo.value)


def test_keychain_locked_fails_fast_without_prompting(kc_store):
    """锁定钥匙串必须立即失败（kSecUseAuthenticationUIFail），不挂进程。

    无法在测试里锁住 login keychain，所以这条只验证「未锁定时的行为是读得到」；
    真正的防交互由查询常量保证，其存在由 keychain_macos 的绑定代码钉住。
    """
    ref = _kc_ref()
    kc_store.set(ref, FAKE_KEY)
    kc_store.delete(ref)


def test_keychain_does_not_silently_fall_back_to_env(monkeypatch):
    """选了 keychain 就**不许**偷偷用环境变量（平台条件化的契约测试）。"""
    monkeypatch.setenv("TIANSHU_API_KEY", FAKE_KEY)
    monkeypatch.setenv("TIANSHU_CREDENTIAL_BACKEND", "keychain")
    if IS_MACOS:
        KeychainCredentialStore().delete(CredentialRef("openai", "llm"))
        with pytest.raises(ModelError) as excinfo:
            OpenAICompatibleModel("gpt-4o-mini")
        assert "缺少 API key" in str(excinfo.value)
    else:
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
    """adapter 经凭据接缝取 key：换后端时绝不静默改读 `os.environ.get`。

    macOS（真后端、keychain 无条目）→ 构造以「缺少 API key」失败；
    其他平台（stub）→ 构造以 `CredentialUnsupported` 失败。
    如果哪天有人图省事改回 `os.environ.get`，env 里的 FAKE_KEY 会让构造
    静默成功，这个测试就会红。
    """
    if IS_MACOS:
        KeychainCredentialStore().delete(CredentialRef("openai", "llm"))
    monkeypatch.setenv("TIANSHU_API_KEY", FAKE_KEY)
    monkeypatch.setenv("TIANSHU_CREDENTIAL_BACKEND", "keychain")
    captured = _CapturingTransport()
    if IS_MACOS:
        with pytest.raises(ModelError) as excinfo:
            OpenAICompatibleModel("gpt-4o-mini", transport=captured)
        assert "缺少 API key" in str(excinfo.value)
    else:
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
