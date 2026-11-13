"""Run pytest with a Python-thread timed stack dump; retain native fatal handling.

CPython 3.12's native timed traceback traversal can race with frame destruction
(python/cpython#140815). This runner avoids that timed path. A Python watchdog
cannot run while an extension indefinitely holds the GIL; the external CI job
limit still applies but does not provide the missing stack in that situation.
"""

from __future__ import annotations

import math
import os
import sys
import threading
from collections.abc import Mapping
from types import FrameType

import pytest

THREAD_NAME = "pytest-python-stack-watchdog"
MAX_STACK_BYTES = 262144


def render_stacks(frames: Mapping[int, FrameType]) -> bytes:
    """Bound diagnostic output without copying source lines, locals, or node IDs."""
    truncated = b"\n[stack output truncated]\n"
    buffer = bytearray(b"\nPYTHON STACK WATCHDOG\n")

    def append(line: str) -> bool:
        data = line.encode("utf-8", errors="backslashreplace")
        if len(buffer) + len(data) > MAX_STACK_BYTES - len(truncated):
            buffer.extend(truncated)
            return False
        buffer.extend(data)
        return True

    for index, (ident, frame) in enumerate(frames.items()):
        if index >= 100:
            append("[thread limit reached]\n")
            break
        if not append(f"Thread {ident}:\n"):
            break
        depth = 0
        while frame is not None and depth < 100:
            code = frame.f_code
            filename, function = code.co_filename[:1024], code.co_name[:128]
            if not append(f'  File "{filename}", line {frame.f_lineno}, in {function}\n'):
                return bytes(buffer)
            frame = frame.f_back
            depth += 1
        if frame is not None and not append("[frame limit reached]\n"):
            break
    return bytes(buffer)


class StackWatchdog:
    def __init__(self, fd: int) -> None:
        self.fd = fd
        self.active: dict[threading.Thread, threading.Event] = {}
        self.failures: list[str] = []

    def pytest_addoption(self, parser) -> None:
        parser.addoption(
            "--stack-timeout", type=float, default=60.0,
            help="Dump Python stacks after this many seconds per test (0 disables).",
        )

    def pytest_configure(self, config) -> None:
        timeout = config.getoption("--stack-timeout")
        if not math.isfinite(timeout) or not 0 <= timeout <= threading.TIMEOUT_MAX:
            raise pytest.UsageError(
                "--stack-timeout must be finite, nonnegative, and <= threading.TIMEOUT_MAX"
            )
        if not config.pluginmanager.hasplugin("faulthandler"):
            raise pytest.UsageError("The native fatal faulthandler plugin must remain enabled")
        if float(config.getini("faulthandler_timeout") or 0):
            raise pytest.UsageError("Use --stack-timeout, not native faulthandler_timeout")

    def write(self, data: bytes) -> None:
        offset = 0
        while offset < len(data):
            written = os.write(self.fd, data[offset:])
            if written == 0:
                raise OSError("stack output made no progress")
            offset += written

    def snapshot(self) -> None:
        frames = sys._current_frames()
        try:
            data = render_stacks(frames)
        finally:
            del frames
        self.write(data)

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_protocol(self, item):
        timeout = item.config.getoption("--stack-timeout")
        if timeout == 0 or self.failures:
            return (yield)
        stopped = threading.Event()

        def watch() -> None:
            try:
                if not stopped.wait(timeout):
                    self.snapshot()
            except BaseException as exc:
                # Do not leak exception values, which can contain private data.
                self.failures.append(f"stack collection failed ({type(exc).__name__[:128]})")

        thread = threading.Thread(target=watch, name=THREAD_NAME, daemon=True)
        self.active[thread] = stopped
        try:
            thread.start()
        except BaseException:
            self.active.pop(thread)
            raise
        try:
            return (yield)
        finally:
            stopped.set()
            thread.join(timeout=2)
            if thread.is_alive():
                self.failures.append("stack watchdog did not stop within 2 seconds")
            else:
                self.active.pop(thread)

    def pytest_sessionfinish(self, session, exitstatus) -> None:
        if self.failures and exitstatus == pytest.ExitCode.OK:
            session.exitstatus = pytest.ExitCode.INTERNAL_ERROR

    def pytest_terminal_summary(self, terminalreporter) -> None:
        if self.failures:
            terminalreporter.write_sep("=", "Python stack watchdog failed")
            for failure in self.failures:
                terminalreporter.write_line(failure)

    def close(self) -> None:
        for thread, stopped in list(self.active.items()):
            stopped.set()
            # The per-test cleanup already waited; never wait again on a blocked fd.
            thread.join(timeout=0)
            if thread.is_alive():
                self.failures.append("stack watchdog remained active at session close")
            else:
                self.active.pop(thread)


def main(argv: list[str]) -> int:
    fd = os.dup(sys.stderr.fileno())  # Keep diagnostics outside pytest's capture fd.
    plugin = StackWatchdog(fd)
    try:
        result = int(pytest.main(argv, plugins=[plugin]))
    finally:
        try:
            plugin.close()
        finally:
            # A blocked writer still owns this fd. Do not risk another wait while
            # closing it; the failed CLI process leaves it for OS exit cleanup.
            if not plugin.active:
                os.close(fd)
    if result == pytest.ExitCode.OK and plugin.failures:
        return int(pytest.ExitCode.INTERNAL_ERROR)
    return result


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
