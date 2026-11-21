"""Responses 在线配置入口与共享 openai 凭据域。"""

import argparse

import pytest

from tianshu import cli
from tianshu.credentials import CredentialRef, EnvCredentialStore
from tianshu.errors import TianshuError
from tianshu.model import ModelResponse, ScriptedModel
from tianshu.settings_control import ModelConfig, RuntimeModelSlot


def test_responses_requires_explicit_model_in_config():
    with pytest.raises(ValueError, match="显式"):
        ModelConfig.of({"mode": "openai-responses", "credential_backend": "env"})
    cfg = ModelConfig.of(
        {"mode": "openai-responses", "model_name": "test-model", "credential_backend": "env"}
    )
    assert cfg.mode == "openai-responses"


def test_responses_slot_uses_openai_credential_domain_and_scope(tmp_path):
    store = EnvCredentialStore({"TIANSHU_API_KEY": "fake-only"})
    slot = RuntimeModelSlot(
        ScriptedModel([ModelResponse("end_turn")]), data_root=tmp_path, stores={"env": store}
    )
    cfg = {
        "mode": "openai-responses",
        "model_name": "test-model",
        "base_url": "https://API.OPENAI.COM:443/v1/",
        "credential_backend": "env",
    }
    slot.configure(cfg)
    active = slot._resolve_active_model()
    assert type(active).__name__ == "OpenAIResponsesModel"
    assert store.get(CredentialRef("openai", "llm")) is not None
    assert slot.capability_profile().provider == "openai-responses"
    assert slot.price_identity().adapter == "openai-responses"
    assert slot.price_identity().vendor == "openai"
    assert slot.continuation_scope() == active.continuation_scope()
    reloaded = RuntimeModelSlot(None, data_root=tmp_path, stores={"env": store})
    assert reloaded.current_config() == slot.current_config()
    assert reloaded.continuation_scope() == slot.continuation_scope()


def test_fallback_responses_slot_roundtrips_mode():
    from tianshu.openai_responses import OpenAIResponsesModel

    original = OpenAIResponsesModel("explicit", api_key="fake")
    slot = RuntimeModelSlot(original, stores={"env": EnvCredentialStore({})})
    assert slot.current_config().mode == "openai-responses"
    assert slot.current_config().model_name == "explicit"
    assert slot.continuation_scope() == original.continuation_scope()


def test_cli_response_mode_dispatches_and_does_not_guess_model(monkeypatch):
    from tianshu.openai_responses import OpenAIResponsesModel

    monkeypatch.setenv("TIANSHU_API_KEY", "fake")
    monkeypatch.setenv("TIANSHU_CREDENTIAL_BACKEND", "env")
    args = argparse.Namespace(
        model="openai-compatible", provider="openai-responses", model_name=None, base_url=None
    )
    assert "openai-responses" in cli.MODEL_PROVIDERS
    with pytest.raises(TianshuError, match="显式"):
        cli._model_from(args)
    args.model_name = "explicit"
    assert isinstance(cli._model_from(args), OpenAIResponsesModel)


@pytest.mark.parametrize(
    "argv",
    [
        ["run", "--help"],
        ["worker", "--help"],
        ["daemon", "--help"],
        ["host", "--help"],
        ["evaluate", "--help"],
    ],
)
def test_all_cli_provider_help_advertises_responses(argv, capsys):
    with pytest.raises(SystemExit) as exit_info:
        cli.main(argv)
    assert exit_info.value.code == 0
    assert "openai-responses" in capsys.readouterr().out


def test_slot_snapshot_keeps_configuration_and_scope_stable():
    cfg = {"mode": "openai-responses", "model_name": "first", "credential_backend": "env"}
    store = EnvCredentialStore({"TIANSHU_API_KEY": "fake-only"})
    slot = RuntimeModelSlot(None, stores={"env": store})
    slot.configure(cfg)
    frozen = slot.snapshot()
    slot.configure({**cfg, "model_name": "second"})
    assert frozen.current_config().model_name == "first"
    assert frozen.continuation_scope()[2] == "first"
    assert slot.continuation_scope()[2] == "second"
    assert frozen.config_revision + 1 == slot.config_revision


@pytest.mark.parametrize("url", [
    "https://host.invalid/v1?key=secret", "https://host.invalid/v1#fragment",
    "https://host.invalid:bad/v1",
])
def test_responses_configuration_rejects_unsafe_scope_before_saving(tmp_path, url):
    slot = RuntimeModelSlot(None, data_root=tmp_path, stores={"env": EnvCredentialStore({})})
    with pytest.raises(ValueError):
        slot.configure({
            "mode": "openai-responses", "model_name": "explicit",
            "base_url": url, "credential_backend": "env",
        })
    assert not (tmp_path / "model_config.json").exists()
