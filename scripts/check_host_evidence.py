"""Assert diagnostic observations; a successful collector is not acceptance."""

import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
data = json.loads((root / "interleavings.json").read_text(encoding="utf-8"))
cases = {item["name"]: item for item in data["cases"]}
assert len(cases) == len(data["cases"]) == 7
for name, owner in (
    ("clear_then_writer", 424243),
    ("clear_then_clear", None),
    ("partial_writer_then_reader", 424243),
):
    case = cases[name]
    assert case["second_event"]["phase"] == "LOCK_BUSY", case
    assert case["first_result"]["error"] is None, case
    assert case["second_result"]["error"] is None, case
    assert case["current_owner"] == owner, case
assert cases["partial_writer_then_reader"]["second_result"]["value"]["pid"] == 424243
for name in ("metadata_guard_normal_release", "metadata_guard_hard_exit"):
    case = cases[name]
    assert case["first_reader_event"]["phase"] == "LOCK_BUSY", case
    assert case["reader_result"]["error"] is None, case
    assert case["reader_result"]["value"]["pid"] == 424242, case
assert cases["metadata_guard_normal_release"]["holder_returncode"] == 0
assert cases["metadata_guard_hard_exit"]["holder_returncode"] != 0
case = cases["business_guard_is_separate"]
assert case["first_reader_event"]["phase"] == "RESULT", case
assert case["reader_result"]["error"] is None, case
assert case["reader_result"]["value"]["pid"] == 424242, case
case = cases["metadata_guard_timeout"]
assert case["result"]["error"] == "HostError", case
assert "5000ms" in case["result"]["message"], case
assert case["result"]["elapsed_s"] >= 4.5, case
assert case["metadata_exists"] and case["metadata_guard_exists"], case

exclusive = json.loads((root / "exclusive-read.json").read_text(encoding="utf-8"))
assert exclusive["original_bytes_preserved"]
operations = {item["operation"]: item["observed"] for item in exclusive["cases"]}
assert set(operations) == {"read", "clear", "stop"}
assert len(exclusive["cases"]) == 3
for operation in ("read", "clear"):
    case = operations[operation]
    assert case["error"] == "HostError" and case["cause_winerror"] == 32, case
    assert case["path_exists"], case
case = operations["stop"]
assert case["error"] is None and case["returned"] == 1 and case["path_exists"], case

ready = json.loads((root / "ready-transition.json").read_text(encoding="utf-8"))
assert [event["phase"] for event in ready["events"]] == [
    "SERVE_HELD", "LOCK_BUSY", "SERVER_STARTED", "EXIT"
], ready
winner = ready["events"][0]["pid"]
exits = {item["pid"]: item["exit"] for item in ready["process_exits"]}
assert len(exits) == len(ready["process_exits"]) == 2, ready
assert winner in exits, ready
others = set(exits) - {winner}
assert len(others) == 1, ready
loser = others.pop()
assert winner != loser
assert ready["current_owner"]["pid"] == winner, ready
assert len(ready["snapshot_health"]) == 1, ready
assert ready["snapshot_health"][0]["pid"] == winner, ready
assert ready["snapshot_health"][0]["alive"], ready
assert not ready["two_distinct_hosts_alive"], ready
assert ready["daemon_alive_before_cleanup"] and not ready["daemon_alive_after_cleanup"], ready
assert exits == {winner: 0, loser: 1}, ready
assert ready["events"][-1]["returned"] == 1, ready
assert not ready.get("forced_cleanup") and not ready.get("cleanup_http_errors"), ready
monitor = json.loads((root / "monitor-start.json").read_text(encoding="utf-8"))
monitor_events = {event["phase"]: event for event in monitor["events"]}
assert len(monitor_events) == len(monitor["events"]) == 3, monitor
assert set(monitor_events) == {"MONITOR_START_HELD", "SERVER_READY", "EXIT"}, monitor
assert monitor["stopper"]["exit"] == monitor["process_exit"] == 0, monitor
assert monitor["stopper"]["stderr"] == monitor["stderr"] == "", monitor
assert monitor["remaining_stdout"] == "", monitor
assert not monitor["daemon_alive_after_stop"] and not monitor["daemon_alive_after_exit"], monitor
assert monitor["metadata_absent_after_exit"], monitor
monitor_exit = monitor_events["EXIT"]
assert monitor_exit["returned"] == 0, monitor
assert not monitor_exit["log_open"] and not monitor_exit["monitor_alive"], monitor
assert monitor_exit["bridge_closed"], monitor
print("Process interleavings, read denial, Host publication and monitor cleanup verified.")
