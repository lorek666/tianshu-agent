"""环境凭据与文本配置：使用私有配置和假密钥，不访问系统钥匙串。"""

import json
import os
import subprocess
import sys
from contextlib import closing
from types import ModuleType

import pytest

import tianshu
from tianshu.credentials import CredentialRef, EnvCredentialStore, default_store
from tianshu.errors import CredentialUnsupported
from tianshu.model import ModelError, ModelResponse, ScriptedModel, UrllibTransport
from tianshu.model_route import ModelRouteV1
from tianshu.settings_control import ModelConfig, RuntimeModelSlot, credential_views


@pytest.fixture(autouse=True)
def forbid_native_credentials(monkeypatch):
    """旧实现的红测也不能访问宿主凭据服务。"""
    native = ModuleType("tianshu.keychain_macos")

    def forbidden(*args, **kwargs):
        pytest.fail("不应访问系统钥匙串")

    for name in ("get_item", "set_item", "delete_item"):
        monkeypatch.setattr(native, name, forbidden, raising=False)
    monkeypatch.setitem(sys.modules, native.__name__, native)
    monkeypatch.setattr(tianshu, "keychain_macos", native, raising=False)


def slot_at(root):
    root.mkdir(exist_ok=True)
    return RuntimeModelSlot(
        ScriptedModel([ModelResponse(finish_reason="end_turn", content="offline")]),
        data_root=root,
    )


@pytest.mark.parametrize("platform", ["darwin", "win32", "linux"])
def test_all_platforms_default_to_env(monkeypatch, tmp_path, platform):
    monkeypatch.delenv("TIANSHU_CREDENTIAL_BACKEND", raising=False)
    monkeypatch.setattr(sys, "platform", platform)
    slot = slot_at(tmp_path / "data")
    assert slot.current_config().credential_backend == "env"
    assert {item["backend"] for item in credential_views(slot)} == {"env"}
    assert all(not item["writable"] for item in credential_views(slot))


@pytest.mark.parametrize("provider,variable", [
    ("openai", "TIANSHU_API_KEY"), ("anthropic", "ANTHROPIC_API_KEY"),
    ("openai-responses", "TIANSHU_API_KEY"),
])
def test_text_config_reloads_and_reads_env_only(monkeypatch, tmp_path, provider, variable):
    monkeypatch.setenv("TIANSHU_CREDENTIAL_BACKEND", "env")
    key = "test-environment-secret-123456789"
    monkeypatch.setenv(variable, key)
    root = tmp_path / "data"
    slot = slot_at(root)
    slot.configure({"mode": provider, "model_name": "test-model", "base_url": "https://example.test/v1"})
    slot.set_model_profile("Main", slot.current_config().to_dict())
    reloaded = slot_at(root)
    assert reloaded.current_config() == slot.current_config()
    assert reloaded.model_profiles() == slot.model_profiles()
    model = reloaded._resolve_active_model()
    assert model.api_key == key
    domain = "openai" if provider == "openai-responses" else provider
    assert reloaded.store_for("env").get(CredentialRef(domain, "llm")) == key
    for path in root.iterdir():
        assert key not in path.read_text()
    monkeypatch.delenv(variable)
    assert EnvCredentialStore().get(CredentialRef(domain, "llm")) is None
    # 真子进程只接收导入/平台所需的非敏感变量，绝不继承生产 provider key。
    child_home = tmp_path / "child-home"
    child_tmp = tmp_path / "child-tmp"
    child_home.mkdir()
    child_tmp.mkdir()
    child_env = {
        name: os.environ[name]
        for name in (
            "PATH", "PYTHONPATH", "SYSTEMROOT", "SystemRoot",
            "TIANSHU_TEST_LOCK_ROOT", "PYTEST_TIANSHU_REGISTRY_HOME",
            "TIANSHU_AUTHORITY_ROOT",
        )
        if name in os.environ
    }
    child_env.update(
        HOME=str(child_home), USERPROFILE=str(child_home),
        TMPDIR=str(child_tmp), TEMP=str(child_tmp), TMP=str(child_tmp),
    )
    child_env[variable] = key
    assert "TIANSHU_API_KEY" not in child_env or variable == "TIANSHU_API_KEY"
    assert "ANTHROPIC_API_KEY" not in child_env or variable == "ANTHROPIC_API_KEY"
    child_code = (
        "from pathlib import Path; import os, sys; "
        "from tianshu import maintenance, restore_registry; "
        "from tianshu.authority import authority_root; "
        "from tianshu.settings_control import RuntimeModelSlot; "
        "assert maintenance._lock_root() == Path(os.environ['TIANSHU_TEST_LOCK_ROOT']); "
        "assert restore_registry._user_home() == Path(os.environ['PYTEST_TIANSHU_REGISTRY_HOME']); "
        "assert authority_root() == Path(os.environ['TIANSHU_AUTHORITY_ROOT']).resolve(); "
        "slot = RuntimeModelSlot(None, data_root=Path(sys.argv[1])); "
        "assert slot._resolve_active_model().api_key == os.environ[sys.argv[2]]; "
        "assert slot.current_config().credential_backend == 'env'"
    )
    child_argv = [sys.executable, "-c", child_code, str(root), variable]
    result = subprocess.run(
        child_argv, env=child_env, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    for missing in (
        "TIANSHU_TEST_LOCK_ROOT", "PYTEST_TIANSHU_REGISTRY_HOME",
        "TIANSHU_AUTHORITY_ROOT",
    ):
        broken_env = dict(child_env)
        broken_env.pop(missing)
        result = subprocess.run(
            child_argv, env=broken_env, capture_output=True, text=True, timeout=20,
        )
        assert result.returncode != 0, missing


@pytest.mark.parametrize("mode", ["openai", "anthropic", "openai-responses"])
def test_missing_key_fails_before_network(monkeypatch, tmp_path, mode):
    monkeypatch.delenv("TIANSHU_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    def forbidden(*args, **kwargs):
        pytest.fail("未配置凭据时不应发送 HTTP 请求")

    monkeypatch.setattr(UrllibTransport, "post", forbidden)
    monkeypatch.setattr(UrllibTransport, "post_stream", forbidden)
    slot = slot_at(tmp_path / "data")
    slot.configure({"mode": mode, "model_name": "test", "credential_backend": "env"})
    with pytest.raises(ModelError, match="缺少 API key") as error:
        slot.complete([], [])
    assert error.value.kind == "auth"
    with pytest.raises(ModelError, match="缺少 API key"):
        slot.stream_complete([], [], lambda delta: None)


def test_legacy_settings_and_profiles_migrate_without_rewriting_or_os_access(monkeypatch, tmp_path):
    monkeypatch.setenv("TIANSHU_CREDENTIAL_BACKEND", "env")
    root = tmp_path / "data"
    root.mkdir()
    config = {"mode": "openai", "model_name": "test", "base_url": "https://example.test/v1",
              "credential_backend": "keychain"}
    paths = {
        root / "model_config.json": config,
        root / "model_profiles.json": {"format_version": 1, "profiles": {"Main": config}},
    }
    for path, payload in paths.items():
        path.write_text(json.dumps(payload))
    before = {path: path.read_bytes() for path in paths}
    slot = slot_at(root)
    assert slot.current_config().credential_backend == "env"
    assert slot.model_profiles()["Main"]["credential_backend"] == "env"
    assert {path: path.read_bytes() for path in paths} == before
    slot.configure(slot.current_config().to_dict())
    assert json.loads((root / "model_config.json").read_text())["credential_backend"] == "env"
    slot.set_model_profile("Main", slot.model_profiles()["Main"])
    profiles = json.loads((root / "model_profiles.json").read_text())["profiles"]
    assert profiles["Main"]["credential_backend"] == "env"


@pytest.mark.parametrize("filename", ["model_config.json", "model_profiles.json"])
@pytest.mark.parametrize("backend", ["typo", "", None, False, 0, {}, [], ["env"], 42])
def test_unknown_saved_backend_is_not_migrated(tmp_path, filename, backend):
    root = tmp_path / "data"
    root.mkdir()
    cfg = {"mode": "openai", "credential_backend": backend}
    payload = cfg if filename == "model_config.json" else {
        "format_version": 1, "profiles": {"Main": cfg},
    }
    path = root / filename
    path.write_text(json.dumps(payload))
    before = path.read_bytes()
    from tianshu.errors import TianshuError

    with pytest.raises((ValueError, TianshuError)):
        slot_at(root)
    assert path.read_bytes() == before


def test_new_keychain_config_and_historical_execution_are_explicitly_refused(tmp_path):
    config = {"mode": "openai", "model_name": "test", "base_url": "https://example.test/v1",
              "credential_backend": "keychain"}
    with pytest.raises(ValueError, match="停用"):
        ModelConfig.of(config)
    route = ModelRouteV1(("Main",), "Main", **config).to_payload()
    assert ModelRouteV1.of(route).to_payload() == route
    from tianshu.events import EventLog

    with closing(EventLog(tmp_path / "historical.db", blob_dir=tmp_path / "blobs")) as log:
        log.append("run_history", "run.model_route", route, actor_kind="core")
        assert log.read_run("run_history")[0].payload == route
    slot = slot_at(tmp_path / "data")
    with pytest.raises(ValueError, match="停用"):
        slot.bind_model_route(route)
    assert route["config"]["credential_backend"] == "keychain"


def test_retired_backend_selector_fails_without_reading_keychain(monkeypatch):
    monkeypatch.setenv("TIANSHU_CREDENTIAL_BACKEND", "keychain")
    with pytest.raises(CredentialUnsupported, match="停用"):
        default_store()


def test_retired_selector_cannot_be_hidden_by_saved_env_config(monkeypatch, tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    (root / "model_config.json").write_text(json.dumps({
        "mode": "openai", "credential_backend": "env",
    }))
    monkeypatch.setenv("TIANSHU_CREDENTIAL_BACKEND", "keychain")
    with pytest.raises(CredentialUnsupported, match="停用"):
        slot_at(root)


@pytest.mark.parametrize("field", ["api_key", "secret", "token"])
def test_plaintext_config_rejects_secret_fields(tmp_path, field):
    slot = slot_at(tmp_path / "data")
    with pytest.raises(ValueError):
        slot.configure({"mode": "openai", field: "test-secret-never-persist"})
    assert not (tmp_path / "data/model_config.json").exists()
