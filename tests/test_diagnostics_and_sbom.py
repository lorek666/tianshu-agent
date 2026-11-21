"""F15 许可证合规与 CycloneDX SBOM 校验 + D7/F14 默认脱敏本地诊断包导出测试。"""

from __future__ import annotations

import ast
import importlib.util
import json
import zipfile
from pathlib import Path

import pytest

from tianshu import cli
from tianshu.credentials import register_secret
from tianshu.diagnostics_export import (
    DIAGNOSTIC_SECTIONS,
    export_diagnostics,
    preview_diagnostics,
)
from tianshu.events import EventLog
from tianshu.loop import build_stack
from tianshu.scheduler import Scheduler

REPO_ROOT = Path(__file__).resolve().parents[1]
SBOM_SCRIPT = REPO_ROOT / "scripts" / "check_licenses_and_sbom.py"


def _load_sbom_module():
    spec = importlib.util.spec_from_file_location("check_licenses_and_sbom", SBOM_SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_f15_license_compliance_and_cyclonedx_sbom_generation(tmp_path: Path) -> None:
    mod = _load_sbom_module()
    out_file = tmp_path / "sbom.cdx.json"
    rc = mod.main(["--root", str(REPO_ROOT), "--out", str(out_file)])
    assert rc == 0
    sbom = json.loads(out_file.read_text(encoding="utf-8"))
    assert sbom["bomFormat"] == "CycloneDX"
    assert sbom["specVersion"] == "1.5"
    assert sbom["metadata"]["component"]["licenses"] == [{"license": {"id": "Apache-2.0"}}]
    purls = [c["purl"] for c in sbom["components"]]
    assert any(p.startswith("pkg:pypi/pytest@") for p in purls)
    assert any(p.startswith("pkg:npm/react@") for p in purls)
    assert any(p.startswith("pkg:cargo/tauri@") for p in purls)


def test_f15_negative_controls_reject_runtime_deps_and_unvetted_packages(
    tmp_path: Path,
) -> None:
    """负控：一旦引入 Python 运行时依赖或未登记的依赖包，合规检查必须立即失败。"""
    mod = _load_sbom_module()
    fake_repo = tmp_path / "fake_repo"
    fake_repo.mkdir()
    (fake_repo / "LICENSE").write_text("Apache License, Version 2.0\n", encoding="utf-8")
    (fake_repo / "uv.lock").write_text(
        (REPO_ROOT / "uv.lock").read_text(encoding="utf-8"), encoding="utf-8"
    )

    # 负控 1：pyproject.toml 声明非空运行时依赖
    (fake_repo / "pyproject.toml").write_text(
        '[project]\nname = "tianshu"\nversion = "0.1.0"\ndependencies = ["requests"]\n',
        encoding="utf-8",
    )
    with pytest.raises(mod.ComplianceError, match="零第三方运行时依赖"):
        mod.verify_and_build_sbom(fake_repo)

    # 负控 2：dependencies 为空，但 uv.lock 混入未经许可证准入的新包
    (fake_repo / "pyproject.toml").write_text(
        '[project]\nname = "tianshu"\nversion = "0.1.0"\ndependencies = []\n',
        encoding="utf-8",
    )
    (fake_repo / "uv.lock").write_text(
        (fake_repo / "uv.lock").read_text(encoding="utf-8")
        + '\n[[package]]\nname = "unreviewed-agpl-pkg"\nversion = "1.0.0"\n',
        encoding="utf-8",
    )
    with pytest.raises(mod.ComplianceError, match="unreviewed-agpl-pkg"):
        mod.verify_and_build_sbom(fake_repo)

    # 负控 3：GPL / LGPL / AGPL / Proprietary 许可证一律拒绝
    for forbidden_lic in ("GPL-3.0-only", "LGPL-2.1-or-later", "AGPL-3.0", "Proprietary"):
        with pytest.raises(mod.ComplianceError, match="禁止的许可证"):
            mod._check_not_forbidden("probe-pkg", forbidden_lic)

    # 负控 4：ui/package.json 未知依赖与 Cargo.toml [target.*.dependencies] 未知 crate 均拒绝
    (fake_repo / "uv.lock").write_text(
        (REPO_ROOT / "uv.lock").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (fake_repo / "ui").mkdir()
    (fake_repo / "ui" / "package.json").write_text(
        json.dumps(
            {
                "name": "ui",
                "license": "Apache-2.0",
                "dependencies": {"unvetted-js": "1.0.0"},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(mod.ComplianceError, match="unvetted-js"):
        mod.verify_and_build_sbom(fake_repo)

    (fake_repo / "ui" / "package.json").write_text(
        json.dumps({"name": "ui", "license": "Apache-2.0", "dependencies": {"react": "19.3.1"}}),
        encoding="utf-8",
    )
    tauri_dir = fake_repo / "desktop" / "src-tauri"
    tauri_dir.mkdir(parents=True)
    (tauri_dir / "Cargo.toml").write_text(
        '[package]\nname = "tianshu-desktop"\nversion = "0.1.0"\n'
        '[target.\'cfg(windows)\'.dependencies]\nunvetted_win_crate = "0.1"\n',
        encoding="utf-8",
    )
    (tauri_dir / "Cargo.lock").write_text("version = 3\npackage = []\n", encoding="utf-8")
    with pytest.raises(mod.ComplianceError, match="unvetted_win_crate"):
        mod.verify_and_build_sbom(fake_repo)


def test_diagnostics_export_has_zero_network_imports() -> None:
    """机械证明 D7（只在本地导出，绝不上传）：模块源码不导入任何网络客户端模块。"""
    src = (REPO_ROOT / "src" / "tianshu" / "diagnostics_export.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(src)
    forbidden = {"socket", "urllib", "http", "ssl", "ftplib", "smtplib"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root_mod = alias.name.split(".")[0]
                assert root_mod not in forbidden, f"诊断导出模块严禁导入网络库：{alias.name}"
        elif isinstance(node, ast.ImportFrom) and node.module:
            root_mod = node.module.split(".")[0]
            assert root_mod not in forbidden, f"诊断导出模块严禁导入网络库：{node.module}"


def test_diagnostics_preview_and_redacted_local_export(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_root = tmp_path / "secret_user_root"
    ws_dir = tmp_path / "private_client_docs"
    ws_dir.mkdir(parents=True)
    log_dir = data_root / "logs"
    log_dir.mkdir(parents=True)

    leaked_key = "sk-diag-secret-token-abcdef123456"
    register_secret(leaked_key)
    (log_dir / "daemon.log").write_text(
        f"ERROR auth={leaked_key} workspace=/Users/alice/PrivateDoc/contract.md\n",
        encoding="utf-8",
    )
    (data_root / "metrics.jsonl").write_text(
        json.dumps({"metric": "startup_ready_ms", "root": str(data_root)}) + "\n",
        encoding="utf-8",
    )

    log = EventLog(data_root / "events.db", blob_dir=data_root / "blobs")
    stack = build_stack(log, data_root=data_root)
    try:
        task = stack.tasks.create_task(
            "整理客户张三的绝密合同草稿",
            [{"root_path": str(ws_dir), "mode": "writable"}],
        )
        stack.tasks.start_run(
            task.task_id,
            payload={
                "task": task.goal,
                "out": "secret_report.md",
                "limits": {"max_turns": 4, "max_total_tokens": 1000},
                "tools": [],
            },
        )
        Scheduler(log).create(
            "每天读取客户张三目录",
            [{"root_path": str(ws_dir), "mode": "readonly"}],
            interval_seconds=3600,
        )
        (ws_dir / "private_inbox_filename.txt").write_text("body", encoding="utf-8")
        Scheduler(log).create(
            "收到文件时执行私人任务",
            [{"root_path": str(ws_dir), "mode": "readonly"}],
            trigger={"kind": "file_arrival", "directory": str(ws_dir)},
        )
    finally:
        log.close()

    # 1. preview 只读，不产生 zip
    out_dir = tmp_path / "exported_bundle"
    manifest = preview_diagnostics(data_root)
    assert manifest["redacted"] is True
    assert manifest["local_only"] is True
    assert not out_dir.exists()
    assert [s["section"] for s in manifest["sections"]] == list(DIAGNOSTIC_SECTIONS)

    # 2. 按勾选子集导出并验证所有敏感内容均已打码/剥离
    exported = export_diagnostics(
        data_root,
        out_dir,
        sections=["system", "runs_summary", "events_timeline", "schedules", "daemon_log"],
    )
    archive = Path(exported["archive_path"])
    assert archive.is_file()

    with zipfile.ZipFile(archive, "r") as zf:
        names = set(zf.namelist())
        assert "manifest.json" in names
        assert "metrics.json" not in names  # 未勾选的分节不写出
        schedules = json.loads(zf.read("schedules.json"))
        assert {row["trigger"]["kind"] for row in schedules} == {"interval", "file_arrival"}
        assert all("directory" not in row["trigger"] for row in schedules)
        combined_text = "\n".join(
            zf.read(name).decode("utf-8") for name in sorted(names)
        )

    assert leaked_key not in combined_text
    assert "整理客户张三的绝密合同草稿" not in combined_text
    assert "每天读取客户张三目录" not in combined_text
    assert "收到文件时执行私人任务" not in combined_text
    assert "private_inbox_filename.txt" not in combined_text
    assert "trigger_state" not in combined_text
    assert "/Users/alice/PrivateDoc/contract.md" not in combined_text
    assert "private_client_docs" not in combined_text
    assert "<PATH_REDACTED>" in combined_text
    assert "<GOAL_REDACTED>" in combined_text

    # 3. CLI `tianshu data export-diagnostics --preview --json`
    rc = cli.main(
        ["data", "export-diagnostics", "--data-root", str(data_root), "--preview", "--json"]
    )
    assert rc == 0
    cli_out = json.loads(capsys.readouterr().out)
    assert cli_out["preview_only"] is True
    assert cli_out["manifest"]["redacted"] is True

    # 4. 回归第二读者高危探针：真实 Path.home() 含空格子路径整体打码、防覆盖与禁关脱敏
    from tianshu.control import ControlError, ControlPlane
    from tianshu.diagnostics_export import redact_diagnostic_text
    from tianshu.errors import TianshuError

    home = str(Path.home())
    probed = redact_diagnostic_text(
        f"ERROR reading {home}/PrivateClientZhangSan/secret contract.md: denied; "
        r"win=C:\Users\Alice Smith\Documents\payroll.xlsx",
        extra_paths=(str(data_root), home),
    )
    assert "PrivateClientZhangSan" not in probed
    assert "secret contract.md" not in probed
    assert "Alice Smith" not in probed
    assert "payroll.xlsx" not in probed
    assert "<PATH_REDACTED>: denied" in probed

    # Windows JSON/repr 双反斜杠转义路径与全分节（含 metrics.jsonl）导出均须打码
    win_root = r"C:\Users\Alice Smith\AppData\Local\AgentOS"
    json_line = json.dumps(
        {"metric": "startup_ready_ms", "root": win_root + r"\logs\daemon.log"}
    )
    redacted_json_line = redact_diagnostic_text(json_line, extra_paths=(win_root,))
    assert "Alice Smith" not in redacted_json_line
    assert "AgentOS" not in redacted_json_line
    assert "daemon.log" not in redacted_json_line
    assert "<PATH_REDACTED>" in redacted_json_line

    full_export = export_diagnostics(data_root, out_dir)
    with zipfile.ZipFile(full_export["archive_path"], "r") as zf_full:
        assert "metrics.json" in set(zf_full.namelist())
        metrics_text = zf_full.read("metrics.json").decode("utf-8")
        assert "secret_user_root" not in metrics_text
        assert "<PATH_REDACTED>" in metrics_text

    # 禁止写进 data_root 内部
    with pytest.raises(TianshuError, match="不得位于 data_root 内部"):
        export_diagnostics(data_root, data_root / "sub_export")

    # 同一目录连续快速导出绝不覆盖已有 zip
    exp_a = Path(export_diagnostics(data_root, out_dir, sections=["system"])["archive_path"])
    exp_b = Path(export_diagnostics(data_root, out_dir, sections=["system"])["archive_path"])
    assert exp_a != exp_b and exp_a.is_file() and exp_b.is_file()

    # 控制面 RPC 拒绝关闭默认脱敏
    from tianshu.model import ScriptedModel

    log2 = EventLog(data_root / "events.db", blob_dir=data_root / "blobs")
    plane = ControlPlane(
        build_stack(log2, data_root=data_root),
        model=ScriptedModel([]),
        mcp_configs={},
    )
    try:
        with pytest.raises(ControlError) as exc_rpc:
            plane.handle(
                "diagnostics.export",
                {"out_dir": str(out_dir), "redact_sensitive": False},
            )
        assert exc_rpc.value.code == "unsupported"
    finally:
        plane.close()
