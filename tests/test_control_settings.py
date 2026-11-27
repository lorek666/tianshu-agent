"""首日配置、凭据控制面、定时任务 RPC、账户预算视图、MCP 状态与 CLI --desktop 桥接测试。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tianshu import cli
from tianshu.control import ControlError, ControlPlane
from tianshu.credentials import (
    EnvCredentialStore,
    redact,
)
from tianshu.events import EventLog
from tianshu.loop import build_stack
from tianshu.model import ModelResponse, ScriptedModel


def _make_plane(tmp_path: Path, *, env_map=None) -> ControlPlane:
    data_root = tmp_path / "data_root"
    data_root.mkdir(parents=True, exist_ok=True)
    log = EventLog(data_root / "events.db", blob_dir=data_root / "blobs")
    stack = build_stack(log, data_root=data_root)
    stores = {
        "env": EnvCredentialStore(env_map if env_map is not None else {}),
    }
    model = ScriptedModel([ModelResponse(finish_reason="end_turn", content="ok")])
    return ControlPlane(
        stack,
        model=model,
        credential_stores=stores,
        mcp_configs={},
    )


@pytest.mark.parametrize("method", ["credential.set", "credential.delete"])
@pytest.mark.parametrize("backend", [None, "env", "keychain"])
def test_credential_mutation_is_refused_without_leaking_or_writing(tmp_path, method, backend):
    key = "test-env-rpc-secret-99887766"
    env = {"TIANSHU_API_KEY": key}
    plane = _make_plane(tmp_path, env_map=env)
    try:
        root = tmp_path / "data_root"
        before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
        params = {"provider": "openai", "secret": key}
        if backend is not None:
            params["backend"] = backend
        with pytest.raises(ControlError) as error:
            plane.handle(method, params)
        assert error.value.code == "unsupported"
        assert key not in str(error.value)
        assert env == {"TIANSHU_API_KEY": key}
        assert {p: p.read_bytes() for p in root.rglob("*") if p.is_file()} == before
        view = plane.handle("settings.get", {})
        assert key not in json.dumps(view)
        assert view["credentials"][0]["configured"] is True
        assert not (root / "model_config.json").exists()
        assert redact(f"leaked {key}") == "leaked ***"
    finally:
        plane.close()


def test_model_configure_persists_without_secrets_and_hot_switches_slot(
    tmp_path: Path,
) -> None:
    env = {"TIANSHU_API_KEY": "sk-test-persist-key-123456"}
    plane = _make_plane(tmp_path, env_map=env)
    try:
        initial = plane.handle("settings.get", {})
        assert initial["model"]["mode"] == "scripted"

        # anthropic 不传 model_name 必须拒绝
        with pytest.raises(ControlError) as bad_anth:
            plane.handle(
                "model.configure",
                {"mode": "anthropic", "credential_backend": "env"},
            )
        assert bad_anth.value.code == "bad_params"

        # base_url 含凭据必须拒绝
        with pytest.raises(ControlError) as bad_url:
            plane.handle(
                "model.configure",
                {
                    "mode": "openai",
                    "model_name": "gpt-4o-mini",
                    "base_url": "https://user:pass@api.openai.com/v1",
                    "credential_backend": "env",
                },
            )
        assert bad_url.value.code == "bad_params"

        configured = plane.handle(
            "model.configure",
            {
                "mode": "openai",
                "model_name": "gpt-4o-mini",
                "base_url": "https://api.openai.com/v1",
                "credential_backend": "env",
            },
        )
        assert configured["model"]["mode"] == "openai"
        assert configured["model"]["model_name"] == "gpt-4o-mini"

        cfg_file = tmp_path / "data_root" / "model_config.json"
        assert cfg_file.is_file()
        raw_disk = cfg_file.read_text(encoding="utf-8")
        assert "sk-test-persist-key-123456" not in raw_disk
        if sys.platform != "win32":
            assert (cfg_file.stat().st_mode & 0o777) == 0o600

        ident = plane._model_slot.price_identity()
        assert ident is not None
        assert ident.adapter == "openai-compatible"
        assert ident.model == "gpt-4o-mini"
    finally:
        plane.close()

    # 重建 ControlPlane 验证配置跨重启保留
    plane2 = _make_plane(tmp_path, env_map=env)
    try:
        reloaded = plane2.handle("settings.get", {})
        assert reloaded["model"]["mode"] == "openai"
        assert reloaded["model"]["model_name"] == "gpt-4o-mini"
    finally:
        plane2.close()


def test_model_configure_refuses_retired_credential_backend(tmp_path):
    plane = _make_plane(tmp_path)
    try:
        with pytest.raises(ControlError) as error:
            plane.handle("model.configure", {
                "mode": "openai", "model_name": "test", "credential_backend": "keychain",
            })
        assert error.value.code == "bad_params"
        assert "停用" in str(error.value)
    finally:
        plane.close()


def test_image_attachments_require_explicit_responses_opt_in_and_survive_restart(tmp_path: Path):
    config = {"mode": "openai-responses", "model_name": "vision-test", "credential_backend": "env"}
    plane = _make_plane(tmp_path)
    try:
        initial = plane.handle("model.configure", config)["model"]
        assert initial.get("allow_image_attachments") is not True
        for refused in [
            {**config, "mode": "openai", "allow_image_attachments": True},
            {**config, "allow_image_attachments": "true"},
        ]:
            with pytest.raises(ControlError) as error:
                plane.handle("model.configure", refused)
            assert error.value.code == "bad_params"
        saved = plane.handle("model.configure", {**config, "allow_image_attachments": True})
        assert saved["model"]["allow_image_attachments"] is True
    finally:
        plane.close()

    reopened = _make_plane(tmp_path, env_map={"TIANSHU_API_KEY": "sk-local-image-test-only"})
    try:
        assert reopened.handle("settings.get", {})["model"]["allow_image_attachments"] is True
        frozen = reopened._model_slot.snapshot()
        assert frozen.supports_image_attachments is True
        assert frozen._resolve_active_model().supports_image_attachments is True
        reopened.handle("model.configure", config)
        assert reopened._model_slot.supports_image_attachments is False
        assert frozen.supports_image_attachments is True
    finally:
        reopened.close()


def test_schedule_budget_and_mcp_control_plane_rpcs(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    plane = _make_plane(tmp_path)
    try:
        # 1. schedule CRUD
        assert plane.handle("schedule.list", {}) == {"schedules": []}
        created = plane.handle(
            "schedule.create",
            {
                "goal": "每小时汇总工作目录",
                "workspaces": [{"root_path": str(ws), "mode": "readonly"}],
                "interval_seconds": 3600,
            },
        )["schedule"]
        sched_id = created["schedule_id"]
        assert created["enabled"] is True

        disabled = plane.handle(
            "schedule.set_enabled", {"schedule_id": sched_id, "enabled": False}
        )["schedule"]
        assert disabled["enabled"] is False

        listed = plane.handle("schedule.list", {})["schedules"]
        assert len(listed) == 1 and listed[0]["schedule_id"] == sched_id

        removed = plane.handle("schedule.remove", {"schedule_id": sched_id})
        assert removed == {"removed": sched_id}
        assert plane.handle("schedule.list", {}) == {"schedules": []}

        with pytest.raises(ControlError) as nf:
            plane.handle("schedule.remove", {"schedule_id": sched_id})
        assert nf.value.code == "not_found"

        # 2. budget.view（含账户上限与未决预占）
        empty_run_budget = plane.handle("budget.view", {"run_id": "run_missing"})
        assert empty_run_budget["run_account"]["exists"] is False

        ledger = plane.stack.budget
        ledger.create_account(
            "acct_root", limits={"tokens": 10_000, "micros": 500_000}, label="root"
        )
        ledger.create_account(
            "run:run_demo", parent_id="acct_root", run_id="run_demo", label="run"
        )
        rsv = ledger.reserve(
            "run:run_demo",
            attempt_id="att_1",
            need={"tokens": 200, "requests": 1},
            run_id="run_demo",
            call_id="call_1",
        )
        ledger.settle(rsv.reservation_id, usage=None, known=False)

        bview = plane.handle("budget.view", {"run_id": "run_demo"})
        assert bview["run_account"]["exists"] is True
        assert bview["run_account"]["unknown_reservations"] == 1
        assert len(bview["run_account"]["unsettled"]) == 1
        assert bview["unsettled_count"] == 1
        assert len(bview["accounts"]) == 1
        assert bview["accounts"][0]["account_id"] == "acct_root"

        # 3. mcp.list（未配置时诚实说明原因）
        mcp_view = plane.handle("mcp.list", {})
        assert mcp_view["configured"] is False
        assert mcp_view["servers"] == []
    finally:
        plane.close()


def test_cli_desktop_flag_and_interactive_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    desktop_root = tmp_path / "desktop_agent_os"
    cwd = tmp_path / "empty_project"
    cwd.mkdir()
    monkeypatch.setenv("TIANSHU_DATA_ROOT", str(desktop_root))
    monkeypatch.chdir(cwd)

    # 1. --desktop 直接写入/读取桌面端默认数据根
    rc = cli.main(
        [
            "--desktop",
            "schedule",
            "create",
            "--goal",
            "桌面定时检查",
            "--interval-seconds",
            "600",
            "--json",
        ]
    )
    assert rc == 0
    created_out = json.loads(capsys.readouterr().out)
    assert created_out[0]["goal"] == "桌面定时检查"
    assert (desktop_root / "events.db").is_file()
    assert not (cwd / ".tianshu" / "events.db").exists()

    # 子命令后置 --desktop 同样生效
    rc_sub = cli.main(["schedule", "list", "--desktop", "--json"])
    assert rc_sub == 0
    listed_out = json.loads(capsys.readouterr().out)
    assert len(listed_out) == 1

    # 2. --desktop 与显式 --db（即使值等于默认 .tianshu/events.db）或 --data-root 同时传必须拒绝
    for bad_argv in (
        ["--desktop", "approvals", "list", "--db", str(tmp_path / "other.db")],
        ["--desktop", "schedule", "list", "--db", ".tianshu/events.db"],
        ["--desktop", "approvals", "list", "--data-root", str(tmp_path / "dr")],
    ):
        rc_conflict = cli.main(bad_argv)
        assert rc_conflict == 2
        assert "不能与显式 --db 或 --data-root 同时指定" in capsys.readouterr().err

    # worker / replay 等子命令后置 --desktop 同样被识别
    rc_worker = cli.main(["worker", "--once", "--desktop", "--json"])
    assert rc_worker == 0
    capsys.readouterr()

    # 3. 当前目录无 .tianshu/events.db 而桌面端有库时，交互模式输出 --desktop 提示
    monkeypatch.setenv("TIANSHU_FORCE_CLI_HINT", "1")
    rc_hint = cli.main(["approvals", "list"])
    assert rc_hint == 0
    captured = capsys.readouterr()
    assert "--desktop" in captured.err


def test_second_reader_probes_secret_redaction_admission_and_stateless_scripted(
    tmp_path: Path,
) -> None:
    """回归第二读者探针：误填密钥不回显、缺 key 准入不吞异常、热切 scripted 不死循环。"""
    import threading

    from tianshu.loop import AgentLoop, Limits
    from tianshu.model import OpenAICompatibleModel
    from tianshu.settings_control import RuntimeModelSlot

    plane = _make_plane(tmp_path)
    try:
        accidental_key = "sk-accidental-paste-into-wrong-field-9911"
        # 1a. base_url 误粘入密钥或 ftp userinfo 绝不在异常里回显密钥
        for bad_url in (accidental_key, f"ftp://user:{accidental_key}@evil.example/v1"):
            with pytest.raises(ControlError) as exc_url:
                plane.handle(
                    "model.configure",
                    {"mode": "openai", "model_name": "gpt-4o-mini", "base_url": bad_url},
                )
            assert accidental_key not in str(exc_url.value)

        # 1b. model.configure 拒绝未知或凭据键，且不回显值
        with pytest.raises(ControlError) as exc_unk:
            plane.handle(
                "model.configure",
                {"mode": "openai", "api_key": accidental_key},
            )
        assert exc_unk.value.code == "bad_params"
        assert accidental_key not in str(exc_unk.value)

        # 1c. credential.set / delete 限制 KNOWN_PROVIDERS 与 capability_class='llm'
        for bad_cred in (
            {"provider": accidental_key, "secret": "sk-valid-12345678", "backend": "env"},
            {"provider": "openai", "capability_class": "embed", "secret": "sk-valid-12345678"},
        ):
            with pytest.raises(ControlError) as exc_cred:
                plane.handle("credential.set", bad_cred)
            assert exc_cred.value.code == "bad_params"
            assert accidental_key not in str(exc_cred.value)
    finally:
        plane.close()

    # 2. Probe 1 & 2：缺 API Key 时准入放行并在调用期报真实缺 key；热切 scripted 可重复收敛
    root = tmp_path / "loop_probe"
    ws = root / "ws"
    ws.mkdir(parents=True)
    log = EventLog(root / "events.db", blob_dir=root / "blobs")
    stack = build_stack(log, data_root=root)
    try:
        slot = RuntimeModelSlot(
            OpenAICompatibleModel("gpt-4o-mini", api_key="sk-init-12345678"),
            data_root=root,
            stores={"env": EnvCredentialStore({})},
        )
        slot.configure({"mode": "openai", "model_name": "gpt-4o-mini", "credential_backend": "env"})
        loop = AgentLoop(
            tasks=stack.tasks,
            leases=stack.leases,
            claims=stack.claims,
            dispatcher=stack.dispatcher,
            approvals=stack.approvals,
            snapshots=stack.snapshots,
            contents=stack.contents,
            memory=stack.memory,
            summaries=stack.summaries,
            budget=stack.budget,
            model=slot,
            limits=Limits(max_turns=3),
        )
        r1 = loop.start(
            stack.tasks.create_task("t1", [{"root_path": str(ws), "mode": "writable"}]).task_id
        )
        assert r1.outcome == "failed"
        assert "缺少 API key" in (r1.error or "")
        assert "未实现 capability_profile" not in (r1.error or "")

        slot.configure({"mode": "scripted", "credential_backend": "env"})
        for idx in range(2):
            r_sc = loop.start(
                stack.tasks.create_task(
                    f"t_script_{idx}", [{"root_path": str(ws), "mode": "writable"}]
                ).task_id
            )
            assert r_sc.state == "succeeded"
            assert r_sc.outcome == "noop"
            assert r_sc.error is None
            assert r_sc.turns == 2

        # 3. 并发 configure 磁盘与内存最终一致，不抛临时文件冲突
        errors: list[Exception] = []

        def worker(i: int) -> None:
            try:
                mode = "openai" if i % 2 == 0 else "scripted"
                slot.configure(
                    {
                        "mode": mode,
                        "model_name": f"gpt-4o-mini-{i}" if mode == "openai" else "",
                        "credential_backend": "env",
                    }
                )
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        assert errors == []
        saved_disk = json.loads((root / "model_config.json").read_text(encoding="utf-8"))
        assert saved_disk == slot.current_config().to_dict()
    finally:
        log.close()
