"""Pytest-only guards for real source-copy Host/Daemon integration smoke.

Never packaged or used for release acceptance. Both layers verify the original
launch before adding test startup and a same-process guarded test entrypoint.
"""

from __future__ import annotations

import json
import os
import runpy
import subprocess
import sys
from pathlib import Path

ISOLATION_ENV = "PYTEST_TIANSHU_SMOKE_ISOLATION"
MODULE_NAME = "release_smoke_test_launcher"


class _SubprocessProxy:
    def __init__(self, popen):
        self.Popen = popen

    def __getattr__(self, name):
        return getattr(subprocess, name)


class SmokeTestIsolation:
    def __init__(self, root, *, app_dir, ui_dir, release, python_version, runtime_python):
        self.root = Path(root).resolve()
        self.startup = Path(__file__).resolve().parent
        self.app_dir = Path(app_dir).resolve()
        self.ui_dir = Path(ui_dir).resolve()
        self.release = dict(release)
        self.python_version = python_version
        self.runtime_python = runtime_python
        self.native_popen = subprocess.Popen
        self.runtime_pid = None
        self.environment = {
            "TIANSHU_TEST_LOCK_ROOT": str(self.root / "locks"),
            "PYTEST_TIANSHU_REGISTRY_HOME": str(self.root / "registry"),
            "TIANSHU_TEST_REGISTRY_HOME": str(self.root / "registry"),
            "TIANSHU_AUTHORITY_ROOT": str(self.root / "authority"),
        }

    def bind(self, monkeypatch):
        from tianshu import maintenance, restore_registry

        for value in set(self.environment.values()):
            Path(value).mkdir(parents=True)
        for key, value in self.environment.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setattr(maintenance, "_lock_root", lambda: self.root / "locks")
        monkeypatch.setattr(restore_registry, "_user_home", lambda: self.root / "registry")

    def payload(self):
        return {
            "root": str(self.root), "startup_dir": str(self.startup),
            "app_dir": str(self.app_dir), "ui_dir": str(self.ui_dir),
            "release": self.release, "python_version": self.python_version,
            "runtime_python": self.runtime_python,
        }

    def proxy(self, *, runtime=False):
        return _SubprocessProxy(self.popen_runtime if runtime else self.popen)

    def _environment(self, kwargs, *, cwd):
        from tianshu import authority, maintenance, restore_registry

        environment = dict(kwargs["env"])
        if environment.get("PYTHONPATH") != str(self.app_dir):
            raise RuntimeError("smoke isolation original package path mismatch")
        if Path(kwargs["cwd"]).resolve() != cwd:
            raise RuntimeError("smoke isolation original cwd mismatch")
        for key, value in self.environment.items():
            if environment.get(key) != value:
                raise RuntimeError(f"smoke isolation environment mismatch: {key}")
        actual = {
            "locks": maintenance._lock_root(),
            "registry": restore_registry._user_home(),
            "authority": authority.authority_root(),
        }
        if any(value.resolve() != self.root / kind for kind, value in actual.items()):
            raise RuntimeError("smoke isolation parent actual root mismatch")
        environment["PYTHONPATH"] = os.pathsep.join((str(self.startup), str(self.app_dir)))
        environment[ISOLATION_ENV] = json.dumps(self.payload())
        return environment

    def popen(self, command, **kwargs):
        command = list(command)
        if command[1:4] != ["-P", "-m", "tianshu"] or command[4:5] not in (
            ["daemon"], ["host"],
        ):
            raise RuntimeError("smoke isolation command mismatch")
        environment = self._environment(kwargs, cwd=self.app_dir)
        data_root = environment.get("TIANSHU_DATA_ROOT")
        prefix = [self.runtime_python, "-P", "-m", "tianshu"]
        if command[4] == "daemon":
            original = [*prefix, "daemon", "--data-root", data_root]
        else:
            original = [
                *prefix, "host", "run", "--no-supervise", "--port", "0",
                "--ui-dir", str(self.ui_dir), "--data-root", data_root,
                "--release-version", self.release["version"],
                "--release-commit", self.release["commit"],
                "--release-target", self.release["target"],
            ]
        if not data_root or not Path(data_root).is_absolute() or command != original:
            raise RuntimeError("smoke isolation original argv mismatch")
        service = command[4]
        kwargs["env"] = environment
        command[3:4] = [MODULE_NAME, "cli"]
        process = self.native_popen(command, **kwargs)
        (self.root / f"launched-{service}.pid").write_text(str(process.pid), encoding="ascii")
        return process

    def popen_runtime(self, command, **kwargs):
        command = list(command)
        original = [
            self.runtime_python, "-P", "-m", "tianshu.release_smoke",
            "--ui-dir", str(self.ui_dir),
            "--expected-version", self.release["version"],
            "--expected-commit", self.release["commit"],
            "--expected-target", self.release["target"],
            "--expected-python", self.python_version,
        ]
        if command != original:
            raise RuntimeError("smoke isolation original verifier argv mismatch")
        kwargs["env"] = self._environment(kwargs, cwd=self.app_dir.parent)
        command[3:4] = [MODULE_NAME, "runtime"]
        process = self.native_popen(command, **kwargs)
        self.runtime_pid = process.pid
        (self.root / "launched-runtime.pid").write_text(str(process.pid), encoding="ascii")
        return process

    def assert_receipt(self, service, pid):
        assert type(pid) is int and pid > 0
        launched_pid = int((self.root / f"launched-{service}.pid").read_text(encoding="ascii"))
        assert launched_pid == pid
        receipt = json.loads((self.root / f"{service}.json").read_text(encoding="utf-8"))
        assert receipt["pid"] == pid
        assert receipt["service"] == service
        assert receipt["package"] == str(self.app_dir / "tianshu" / "__init__.py")
        assert receipt["roots"] == {
            kind: str(self.root / kind) for kind in ("locks", "registry", "authority")
        }
        return receipt

    def assert_children(self, result, *, runtime=False):
        for service in ("daemon", "host"):
            self.assert_receipt(service, result[f"{service}_pid"])
        if runtime:
            assert self.runtime_pid is not None
            self.assert_receipt("runtime", self.runtime_pid)


def main() -> None:
    expected = json.loads(os.environ[ISOLATION_ENV])
    if not expected.get("root") or not Path(expected["root"]).is_absolute():
        raise RuntimeError("smoke isolation root must be explicit and absolute")
    root = Path(expected["root"]).resolve(strict=True)
    startup = Path(__file__).resolve().parent
    loaded_startup = sys.modules.get("sitecustomize")
    if (
        loaded_startup is None
        or not getattr(loaded_startup, "__file__", None)
        or Path(loaded_startup.__file__).resolve() != startup / "sitecustomize.py"
        or Path(expected["startup_dir"]).resolve(strict=True) != startup
    ):
        raise RuntimeError("smoke isolation startup hook was not loaded from this checkout")
    app_dir = Path(expected["app_dir"]).resolve(strict=True)
    mode = sys.argv[1:2]
    if mode == ["cli"] and sys.argv[2:3] in (["daemon"], ["host"]):
        service = sys.argv[2]
    elif mode == ["runtime"]:
        service = "runtime"
    else:
        raise RuntimeError("smoke isolation command mismatch")
    isolation = SmokeTestIsolation(
        root, app_dir=app_dir, ui_dir=expected["ui_dir"], release=expected["release"],
        python_version=expected["python_version"], runtime_python=expected["runtime_python"],
    )
    for key, value in isolation.environment.items():
        if os.environ.get(key) != value:
            raise RuntimeError(f"smoke isolation environment mismatch: {key}")

    # These imports do not open a database or acquire a production lock. Never
    # repair a missing hook: verify actual function results before any CLI.
    import tianshu
    from tianshu import authority, maintenance, restore_registry

    actual = {}
    for kind, module, getter in (
        ("locks", maintenance, maintenance._lock_root),
        ("registry", restore_registry, restore_registry._user_home),
        ("authority", authority, authority.authority_root),
    ):
        if Path(module.__file__).resolve().parent != app_dir / "tianshu":
            raise RuntimeError(f"smoke isolation module source mismatch: {kind}")
        value = getter().resolve()
        if value != root / kind:
            raise RuntimeError(f"smoke isolation actual root mismatch: {kind}")
        actual[kind] = value
    package = Path(tianshu.__file__).resolve()
    if package.parent.parent != app_dir:
        raise RuntimeError("smoke isolation package mismatch")

    receipt = {
        "pid": os.getpid(), "service": service, "package": str(package),
        "roots": {kind: str(value) for kind, value in actual.items()},
    }
    with (root / f"{service}.json").open("x", encoding="utf-8") as stream:
        json.dump(receipt, stream, sort_keys=True)
    del sys.argv[1]
    if service == "runtime":
        from tianshu import release_smoke

        if Path(release_smoke.__file__).resolve().parent != app_dir / "tianshu":
            raise RuntimeError("smoke isolation runtime source mismatch")
        release_smoke.subprocess = isolation.proxy()
        sys.argv[0] = "tianshu.release_smoke"
        raise SystemExit(release_smoke.main())
    sys.argv[0] = "tianshu"
    runpy.run_module("tianshu", run_name="__main__")


if __name__ == "__main__":
    main()
