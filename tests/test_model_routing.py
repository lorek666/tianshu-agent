"""模型档案只在本地选路；一次 Run 固定实际选择，不能因密钥变化而换供应商。"""

import json

import pytest

from tianshu.credentials import EnvCredentialStore
from tianshu.model import ModelError, ScriptedModel
from tianshu.settings_control import RuntimeModelSlot


def _profile(mode, name, base_url=""):
    return {
        "mode": mode, "model_name": name, "base_url": base_url, "credential_backend": "env",
    }


def _slot(root, env):
    return RuntimeModelSlot(
        ScriptedModel([]), data_root=root, stores={"env": EnvCredentialStore(env)},
    )


def test_route_falls_back_only_at_admission_and_freezes_provider_across_restart(tmp_path):
    env = {"ANTHROPIC_API_KEY": "test-only-anthropic"}
    slot = _slot(tmp_path, env)
    slot.set_model_profile("main", _profile("openai-responses", "primary"))
    slot.set_model_profile("backup", _profile("anthropic", "backup-v1", "https://backup.invalid/v1"))
    slot = _slot(tmp_path, env)
    assert set(slot.model_profiles()) == {"main", "backup"}

    frozen = slot.select_model_route("main", ["backup"])
    assert frozen.requested == ("main", "backup")
    assert frozen.selected == "backup"
    payload = json.loads(json.dumps(frozen.to_payload()))
    assert "test-only-anthropic" not in json.dumps(payload)

    # 此时主模型又有凭据，且备模型的档案被改名/删除。历史 Run 不重选。
    env["TIANSHU_API_KEY"] = "test-only-openai"
    slot.set_model_profile("backup", _profile("anthropic", "backup-v2"))
    slot.remove_model_profile("backup")
    slot = _slot(tmp_path, env)
    bound = slot.bind_model_route(payload)
    assert bound.current_config().model_name == "backup-v1"
    assert bound.current_config().base_url == "https://backup.invalid/v1"
    assert bound.price_identity().adapter == "anthropic"
    assert bound.price_identity().model == "backup-v1"
    assert bound.continuation_scope() is None

    # 恢复时凭据消失也不会偷偷去主模型，且 HTTP 调用前就报明确错误。
    del env["ANTHROPIC_API_KEY"]
    with pytest.raises(ModelError) as exc:
        bound.complete([], [])
    assert exc.value.kind == "auth"
    assert not exc.value.retryable


def test_route_refuses_unknown_or_duplicate_fallback_even_if_primary_is_ready(tmp_path):
    slot = _slot(tmp_path, {"TIANSHU_API_KEY": "test-only"})
    slot.set_model_profile("primary", _profile("openai-responses", "text"))
    with pytest.raises(ValueError, match="不存在"):
        slot.select_model_route("primary", ["unknown"])
    with pytest.raises(ValueError, match="重复"):
        slot.select_model_route("primary", ["primary"])


def test_route_refuses_secret_bearing_endpoint_and_pins_env_endpoint(tmp_path, monkeypatch):
    slot = _slot(tmp_path, {"TIANSHU_API_KEY": "test-only"})
    with pytest.raises(ValueError):
        slot.set_model_profile("oops", _profile("openai", "text", "https://example.invalid/v1?key=oops"))
    slot.set_model_profile("fixed", _profile("openai-responses", "text"))
    monkeypatch.setenv("TIANSHU_BASE_URL", "https://first.invalid/v1")
    route = slot.select_model_route("fixed", [])
    monkeypatch.setenv("TIANSHU_BASE_URL", "https://second.invalid/v1")
    bound = slot.bind_model_route(route.to_payload())
    assert bound.continuation_scope() == (
        "openai-responses", "https://first.invalid/v1/responses", "text",
    )
    assert bound.snapshot().continuation_scope() == bound.continuation_scope()


def test_route_does_not_accept_secret_or_unknown_fields_on_resume(tmp_path):
    slot = _slot(tmp_path, {"TIANSHU_API_KEY": "test-only"})
    slot.set_model_profile("fixed", _profile("openai", "text"))
    payload = slot.select_model_route("fixed", []).to_payload()
    payload["config"]["api_key"] = "do-not-log-this"
    with pytest.raises(ValueError) as exc:
        slot.bind_model_route(payload)
    assert "do-not-log-this" not in str(exc.value)


def test_real_run_uses_persisted_backup_after_profiles_change(tmp_path, monkeypatch):
    from contextlib import closing

    from support import engine_for, stack_for

    from tianshu.control import ControlPlane
    from tianshu.events import EventLog
    from tianshu.model import UrllibTransport

    seen = []

    def complete(_transport, url, *, body, headers, timeout):
        seen.append((url, body["model"]))
        return {"content": [{"type": "text", "text": "done"}], "stop_reason": "end_turn"}

    monkeypatch.setattr(UrllibTransport, "post", complete)
    env = {"ANTHROPIC_API_KEY": "test-only-anthropic"}
    slot = _slot(tmp_path, env)
    slot.set_model_profile("primary", _profile("openai-responses", "first"))
    slot.set_model_profile("backup", _profile("anthropic", "backup-v1", "https://backup.invalid/v1"))
    with closing(EventLog(tmp_path / "events.db", blob_dir=tmp_path / "blobs")) as log:
        stack = stack_for(log, data_root=tmp_path)
        plane = ControlPlane(stack, model=slot)
        task = stack.tasks.create_task("做一次只读回答")
        rid = plane.handle("run.start", {"task_id": task.task_id, "model_profile": "primary",
                                         "fallback_profiles": ["backup"]})["run"]["run_id"]
        [saved] = [e.payload for e in log.read_run(rid) if e.type == "run.model_route"]
        assert saved["selected"] == "backup"
        assert env["ANTHROPIC_API_KEY"] not in json.dumps(saved)
        env["TIANSHU_API_KEY"] = "test-now-ready"
        slot.remove_model_profile("backup")
        adapter = _slot(tmp_path, env)
        result = engine_for(stack, adapter).resume_run(rid)
        assert result.state == "succeeded"
        assert seen == [("https://backup.invalid/v1/messages", "backup-v1")]
