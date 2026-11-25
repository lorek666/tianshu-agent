"""Real wrapper pairs preserve worker isolation, process outcomes and raw reports."""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path

import pytest
from test_pytest_ci_shards import ATTEMPT, RUN_ID, VERIFIER, WRAPPER, project

ROOT = WRAPPER.parent.parent
FILES = {
    f"tests/test_{letter}.py": "def test_first(): pass\ndef test_second(): pass\n"
    for letter in "abcd"
}


def pair_args(index, root):
    return [
        sys.executable, str(WRAPPER), f"--ci-pair-index={index}",
        f"--ci-pair-root={root}", "-o", "addopts=", "-q", "--stack-timeout=60",
    ]


def test_original_pairs_collect_real_ci_evidence_fixture_without_reordering(tmp_path):
    # Collect the actual CI test module, including its evidence fixture scopes.
    # Stop before runtest execution: this probe proves collection compatibility,
    # and its deliberately incomplete reports must still fail strict verification.
    files = {
        **FILES,
        "tests/test_pytest_ci_shards.py": (
            ROOT / "tests/test_pytest_ci_shards.py").read_text(encoding="utf-8"),
        "conftest.py": (
            "import pytest\n@pytest.hookimpl(tryfirst=True)\n"
            "def pytest_runtestloop(session): return True\n"
        ),
    }
    reports = tmp_path / "downloaded"
    documents = []
    for pair_index in (0, 1):
        checkout = tmp_path / f"checkout-{pair_index}"
        env = project(checkout, files)
        root = tmp_path / f"pair-{pair_index}"
        result = subprocess.run(
            pair_args(pair_index, root), cwd=checkout, env=env, capture_output=True,
            text=True, encoding="utf-8", timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        for index in range(pair_index * 2, pair_index * 2 + 2):
            original = root / str(index) / "shard.json"
            documents.append(json.loads(original.read_text(encoding="utf-8")))
            target = reports / str(pair_index) / str(index) / "shard.json"
            target.parent.mkdir(parents=True)
            shutil.copyfile(original, target)
    assert all(doc["full_collection"] == documents[0]["full_collection"]
               for doc in documents)
    assert any(node.startswith("tests/test_pytest_ci_shards.py::")
               for node, _ in documents[0]["full_collection"])
    assert all(doc["selected"] and not doc["reports"] and not doc["finished"]
               for doc in documents)
    rejected = subprocess.run(
        [sys.executable, str(VERIFIER), "verify", "--reports-root", str(reports),
         "--sha", env["GITHUB_SHA"], "--run-id", RUN_ID,
         "--run-attempt", ATTEMPT, "--count", "4", "--indices", "0", "1", "2", "3",
         "--needs-result", "success"],
        cwd=checkout, env=env, capture_output=True, text=True,
        encoding="utf-8", timeout=15,
    )
    assert rejected.returncode == 1
    assert "selected or finished nodes differ" in rejected.stdout + rejected.stderr


def test_original_wrapper_pairs_preserve_four_complete_raw_reports(tmp_path):
    reports = tmp_path / "downloaded"
    envs = []
    documents = []
    for pair_index in range(2):
        checkout = tmp_path / f"checkout-{pair_index}"
        env = project(checkout, FILES)
        envs.append(env)
        root = tmp_path / f"pair-{pair_index}"
        result = subprocess.run(
            pair_args(pair_index, root), cwd=checkout, env=env,
            capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        expected = {root / str(index) / "shard.json"
                    for index in range(pair_index * 2, pair_index * 2 + 2)}
        assert set(root.rglob("shard.json")) == expected
        for path in sorted(expected):
            documents.append(json.loads(path.read_text(encoding="utf-8")))
            target = reports / str(pair_index) / path.parent.name / "shard.json"
            target.parent.mkdir(parents=True)
            shutil.copyfile(path, target)
    assert envs[0]["GITHUB_SHA"] == envs[1]["GITHUB_SHA"]
    assert [doc["shard_index"] for doc in documents] == [0, 1, 2, 3]
    assert all(doc["shard_count"] == 4 for doc in documents)
    assert all(doc["full_collection"] == documents[0]["full_collection"]
               for doc in documents)
    full = [node for node, _ in documents[0]["full_collection"]]
    assert sorted(node for doc in documents for node in doc["selected"]) == sorted(full)
    assert len(full) == 8
    verified = subprocess.run(
        [sys.executable, str(VERIFIER), "verify", "--reports-root", str(reports),
         "--sha", envs[0]["GITHUB_SHA"], "--run-id", RUN_ID,
         "--run-attempt", ATTEMPT, "--count", "4", "--indices", "0", "1", "2", "3",
         "--needs-result", "success"],
        cwd=tmp_path, env=envs[0], capture_output=True, text=True,
        encoding="utf-8", timeout=15,
    )
    assert verified.returncode == 0, verified.stdout + verified.stderr


@pytest.mark.parametrize("launch_layer", [False, True])
def test_four_real_workers_overlap_and_inherit_private_runtime_roots(tmp_path, launch_layer):
    # The actual project fixtures and startup hook, rather than a hand-written
    # fixture substitute, must isolate locks/authority/registry and descendants.
    source = '''
import json, os, socket, subprocess, sys
from pathlib import Path
from tianshu import maintenance, restore_registry

def test_barrier(tmp_path, pytestconfig):
    child = subprocess.run([sys.executable, "-c",
        "import json; from tianshu import maintenance, restore_registry; "
        "print(json.dumps([str(maintenance._lock_root()), str(restore_registry._user_home())]))"],
        capture_output=True, text=True, check=True)
    data = dict(pid=os.getpid(), parent=os.getppid(),
        shard=pytestconfig.getoption("--ci-shard-index"),
        shard_env=os.environ.get("TIANSHU_CI_SHARD_INDEX"),
        checkout=str(pytestconfig.rootpath),
        basetemp=str(pytestconfig._tmp_path_factory.getbasetemp()),
        cache=str(pytestconfig.cache._cachedir),
        lock=str(maintenance._lock_root()), registry=str(restore_registry._user_home()),
        authority=os.environ["TIANSHU_AUTHORITY_ROOT"], child=json.loads(child.stdout))
    address = ("127.0.0.1", int(os.environ["PAIR_TEST_PORT"]))
    with socket.create_connection(address, timeout=15) as s:
        s.sendall(json.dumps(data).encode() + b"\\n")
        assert s.recv(1) == b"G"
'''
    files = {f"tests/test_{letter}.py": source for letter in "abcd"}
    files["tests/conftest.py"] = (ROOT / "tests/conftest.py").read_text(encoding="utf-8")
    files["tests/_startup/sitecustomize.py"] = (
        ROOT / "tests/_startup/sitecustomize.py").read_text(encoding="utf-8")
    with ExitStack() as stack:
        server = stack.enter_context(socket.socket())
        server.bind(("127.0.0.1", 0))
        server.listen(4)
        server.settimeout(20)
        parents = []
        for index in range(2):
            checkout = tmp_path / f"checkout-{index}"
            env = project(checkout, files)
            # Existing project fixtures resolve src relative to this private checkout.
            shutil.copytree(ROOT / "src", checkout / "src",
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            env["PAIR_TEST_PORT"] = str(server.getsockname()[1])
            output = stack.enter_context((tmp_path / f"output-{index}").open("w+"))
            command = pair_args(index, tmp_path / f"reports-{index}")
            if launch_layer:
                command = [
                    sys.executable, "-c",
                    "import subprocess,sys; raise SystemExit(subprocess.call(sys.argv[1:]))",
                    *command,
                ]
            parent = subprocess.Popen(command,
                                      cwd=checkout, env=env, stdout=output,
                                      stderr=subprocess.STDOUT)
            parents.append((parent, output))
            stack.callback(reap, parent)
        observations, connections = [], []
        for _ in range(4):
            connection, _ = server.accept()
            stack.callback(connection.close)
            connection.settimeout(15)
            with connection.makefile("rb") as reader:
                observations.append(json.loads(reader.readline()))
            connections.append(connection)
        parents_running = all(p.poll() is None for p, _ in parents)
        for connection in connections:
            connection.sendall(b"G")
        for parent, output in parents:
            code = parent.wait(timeout=20)
            output.seek(0)
            assert code == 0, output.read()

        assert len({item["pid"] for item in observations}) == 4
        assert {item["shard"] for item in observations} == {0, 1, 2, 3}
        assert all(item["shard_env"] == str(item["shard"]) for item in observations)
        assert parents_running
        for field in ("basetemp", "cache", "lock", "registry", "authority"):
            paths = [Path(item[field]).resolve() for item in observations]
            assert len(set(paths)) == 4, (field, paths)
            assert all(path.is_relative_to(tmp_path) for path in paths), (field, paths)
        for item in observations:
            assert item["child"] == [item["lock"], item["registry"]]
            # A Windows venv executable can add a launcher process at either
            # boundary. Bind ownership to the real plugin index and exact roots,
            # while the four workers are simultaneously held at the barrier.
            pair = item["shard"] // 2
            assert Path(item["checkout"]).resolve() == (tmp_path / f"checkout-{pair}").resolve()
            runtime = tmp_path / f"reports-{pair}-runtime"
            assert Path(item["basetemp"]).resolve() == (runtime / str(item["shard"])).resolve()
            assert Path(item["cache"]).resolve() == (runtime / f"cache-{item['shard']}").resolve()


def reap(process):
    if process.poll() is None:
        process.kill()
    process.wait(timeout=10)


@pytest.mark.parametrize("fault", ["spawn", "interrupt", "wait", "stubborn", "nonzero"])
def test_pair_failure_reaps_owned_roots_and_cannot_use_green_json(tmp_path, fault):
    # Inject at the process boundary in a real supervisor process; the managed
    # roots are real children. The nonzero case keeps genuinely green reports.
    bootstrap = r'''
import json, subprocess, sys
from pathlib import Path
import pytest_ci_pair as pair
original = pair.subprocess.Popen
children = []
signals = []
fault, audit = sys.argv[1:3]
def spawn(*args, **kwargs):
    if fault == "spawn" and children:
        raise OSError("expected second spawn failure")
    child = original(*args, **kwargs)
    children.append(child)
    terminate, kill = child.terminate, child.kill
    def stop():
        signals.append([child.pid, "terminate", child.returncode])
        if fault != "stubborn" or child is not children[0]:
            terminate()
    def force():
        signals.append([child.pid, "kill", child.returncode])
        kill()
    child.terminate, child.kill = stop, force
    return child
pair.subprocess.Popen = spawn
if fault in ("interrupt", "wait", "stubborn"):
    original_wait = original.wait
    interrupted = False
    forced_timeout = False
    def wait(self, *args, **kwargs):
        global interrupted, forced_timeout
        if not interrupted and len(children) == 2:
            interrupted = True
            if fault == "wait":
                raise OSError("expected wait failure")
            raise KeyboardInterrupt
        if (fault == "stubborn" and self is children[0] and self.poll() is None
                and not forced_timeout):
            forced_timeout = True
            raise subprocess.TimeoutExpired(self.args, kwargs.get("timeout"))
        return original_wait(self, *args, **kwargs)
    original.wait = wait
code = pair.main(sys.argv[3:])
Path(audit).write_text(json.dumps(dict(code=code, signals=signals,
    children=[dict(pid=c.pid, code=c.returncode, polled=c.poll()) for c in children])))
raise SystemExit(code)
'''
    files = FILES.copy()
    if fault == "nonzero":
        files["conftest.py"] = '''
def pytest_unconfigure(config):
    plugin = next(p for p in config.pluginmanager.get_plugins()
                  if type(p).__name__ == "StackWatchdog")
    plugin.failures.append("expected late diagnostic failure")
'''
    else:
        # Roots stay alive until the supervisor reaps them, without sleep.
        files = {name: "import threading\n"
                 "def test_wait(): assert threading.Event().wait(20)\n"
                 for name in FILES}
    checkout = tmp_path / "checkout"
    env = project(checkout, files)
    audit, root = tmp_path / "audit.json", tmp_path / "reports"
    result = subprocess.run(
        [sys.executable, "-c", bootstrap, fault, str(audit),
         *pair_args(0, root)[2:]],
        cwd=checkout, env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert audit.exists(), result.stdout + result.stderr
    details = json.loads(audit.read_text())
    assert details["code"] == result.returncode
    assert result.returncode == {"interrupt": 2, "stubborn": 2,
                                 "nonzero": 3}.get(fault, 1)
    assert len(details["children"]) == (1 if fault == "spawn" else 2)
    assert all(c["code"] is not None and c["polled"] == c["code"]
               for c in details["children"])
    assert all(signal[2] is None for signal in details["signals"])
    if fault == "stubborn":
        assert any(signal[1] == "kill" for signal in details["signals"])
    if fault == "nonzero":
        assert details["signals"] == []
        docs = [json.loads(p.read_text()) for p in sorted(root.rglob("shard.json"))]
        assert len(docs) == 2
        assert all(doc["pytest_session_exit"] == 0 for doc in docs)


@pytest.mark.parametrize("fault", ["terminate", "wait", "refused", "reaped"])
@pytest.mark.parametrize("newline", ["lf", "crlf"])
def test_pair_cleanup_handles_each_real_root_independently(tmp_path, fault, newline):
    # A failure on the first owned root must not skip the second. Pipe readers
    # acknowledge startup and stay alive until signalled; no timing sleeps.
    bootstrap = r'''
import json, subprocess, sys
from pathlib import Path
import pytest_ci_pair as pair
real_popen = subprocess.Popen
roots, signals, waits = [], [], []
fault, audit, newline = sys.argv[1:4]
ending = "\r\n" if newline == "crlf" else "\n"
interrupted = False
wait_failed = False
def spawn(*args, **kwargs):
    index = len(roots)
    body = ("pass" if fault == "reaped" and index == 0 else
            f"import sys; sys.stdout.reconfigure(newline={ending!r}); "
            "print('ready', flush=True); sys.stdin.buffer.read(1)")
    child = real_popen([sys.executable, "-u", "-c", body],
                      stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    roots.append(child)
    if not (fault == "reaped" and index == 0):
        assert child.stdout.readline() in {b"ready\n", b"ready\r\n"}
    original_wait, original_stop, original_kill = child.wait, child.terminate, child.kill
    killed = False
    def wait(*args, **kwargs):
        global interrupted, wait_failed
        if not interrupted and index == int(fault == "reaped"):
            interrupted = True
            raise KeyboardInterrupt
        waits.append([index, kwargs.get("timeout")])
        if index == 0 and fault != "reaped" and not killed:
            if fault == "wait" and not wait_failed:
                wait_failed = True
                raise OSError("injected wait error")
            raise subprocess.TimeoutExpired(child.args, kwargs.get("timeout"))
        return original_wait(*args, **kwargs)
    def stop():
        signals.append([index, "terminate", child.returncode])
        if index == 0 and fault in ("terminate", "refused"):
            raise PermissionError("injected terminate error")
        if not (index == 0 and fault == "wait"):
            original_stop()
    def kill():
        nonlocal killed
        signals.append([index, "kill", child.returncode])
        if index == 0 and fault == "refused":
            raise OSError("injected kill error")
        original_kill()
        killed = True
    child.wait, child.terminate, child.kill = wait, stop, kill
    return child
try:
    pair.subprocess.Popen = spawn
    code = pair.main(sys.argv[4:])
    observed = [p.poll() for p in roots]
finally:
    # The test owns fallback cleanup only for the deliberately OS-refused root.
    for p in roots:
        if p.poll() is None:
            real_popen.kill(p)
        real_popen.wait(p, timeout=5)
        p.stdin.close()
        p.stdout.close()
Path(audit).write_text(json.dumps(dict(code=code, observed=observed,
    signals=signals, waits=waits, final=[p.returncode for p in roots])))
raise SystemExit(code)
'''
    checkout = tmp_path / "checkout"
    env = project(checkout, FILES)
    audit = tmp_path / "audit.json"
    result = subprocess.run(
        [sys.executable, "-c", bootstrap, fault, str(audit), newline,
         *pair_args(0, tmp_path / "reports")[2:]],
        cwd=checkout, env=env, capture_output=True, text=True, timeout=20,
    )
    assert audit.exists(), result.stdout + result.stderr
    details = json.loads(audit.read_text())
    assert details["code"] == result.returncode == (2 if fault == "reaped" else 1)
    assert len(details["observed"]) == 2
    assert details["observed"][1] is not None, details
    assert any(s[:2] == [1, "terminate"] for s in details["signals"]), details
    assert all(s[2] is None for s in details["signals"]), details
    cleanup_waits = details["waits"][1:] if fault == "reaped" else details["waits"]
    assert cleanup_waits and all(w[1] is not None and 0 < w[1] <= 5
                                 for w in cleanup_waits), details
    assert all(code is not None for code in details["final"])
    if fault == "refused":
        assert details["observed"][0] is None
        assert "cleanup unconfirmed" in result.stderr
        assert "PermissionError" in result.stderr  # Keep the first failure.
    else:
        assert details["observed"][0] is not None, details
    if fault == "reaped":
        assert details["observed"][0] == 0
        assert not any(s[0] == 0 for s in details["signals"]), details
