"""The seven live release identity sites agree; historical fixtures are independent."""
from __future__ import annotations

import json
import tomllib
from pathlib import Path

from tianshu import __version__
from tianshu.control import DAEMON_VERSION

ROOT = Path(__file__).resolve().parents[1]


def test_all_seven_live_release_identity_sites_agree():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["name"] == "tianshu"
    expected = project["project"]["version"]
    assert __version__ == expected
    assert DAEMON_VERSION == expected

    uv_lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    own_python = [p for p in uv_lock["package"] if p["name"] == "tianshu"]
    assert len(own_python) == 1
    assert own_python[0]["source"] == {"editable": "."}
    assert own_python[0]["version"] == expected

    desktop = ROOT / "desktop" / "src-tauri"
    cargo = tomllib.loads((desktop / "Cargo.toml").read_text(encoding="utf-8"))
    assert cargo["package"]["name"] == "tianshu-desktop"
    assert cargo["package"]["version"] == expected
    cargo_lock = tomllib.loads((desktop / "Cargo.lock").read_text(encoding="utf-8"))
    own_desktop = [p for p in cargo_lock["package"] if p["name"] == "tianshu-desktop"]
    assert len(own_desktop) == 1
    assert own_desktop[0]["version"] == expected
    tauri = json.loads((desktop / "tauri.conf.json").read_text(encoding="utf-8"))
    assert tauri["version"] == expected
