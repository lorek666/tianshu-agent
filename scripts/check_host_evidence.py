"""Assert diagnostic observations; a successful collector is not acceptance."""

import argparse
import errno
import json
from pathlib import Path


def check_interleavings(data):
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
    hard_exit = cases["metadata_guard_hard_exit"]
    assert hard_exit["holder_returncode"] == 23, (
        "holder worker did not acknowledge CRASH and exit with 23", hard_exit
    )
    for name in ("metadata_guard_normal_release", "metadata_guard_hard_exit"):
        case = cases[name]
        assert case["first_reader_event"]["phase"] == "LOCK_BUSY", case
        assert case["reader_result"]["error"] is None, case
        assert case["reader_result"]["value"]["pid"] == 424242, case
        assert case["holder_event"] == {
            "phase": "GUARD_HELD", "pid": case["holder_worker_pid"], "scope": "hold_meta"
        }, case
        assert case["holder_worker_pid"] > 0 and case["holder_launcher_pid"] > 0, case
        assert case["first_reader_event"]["pid"] != case["holder_worker_pid"], case
        assert case["holder_output"] == {"stdout": "", "stderr": ""}, case
    assert cases["metadata_guard_normal_release"]["holder_returncode"] == 0
    assert cases["metadata_guard_normal_release"]["crash_event"] is None
    assert hard_exit["crash_event"] == {
        "phase": "CRASHING", "pid": hard_exit["holder_worker_pid"], "scope": "hold_meta"
    }, hard_exit
    case = cases["business_guard_is_separate"]
    assert case["first_reader_event"]["phase"] == "RESULT", case
    assert case["reader_result"]["error"] is None, case
    assert case["reader_result"]["value"]["pid"] == 424242, case
    assert case["holder_event"]["scope"] == "hold_business", case
    assert case["holder_returncode"] == 0 and case["crash_event"] is None, case
    assert case["holder_output"] == {"stdout": "", "stderr": ""}, case
    case = cases["metadata_guard_timeout"]
    assert case["result"]["error"] == "HostError", case
    assert "5000ms" in case["result"]["message"], case
    assert case["result"]["elapsed_s"] >= 4.5, case
    assert case["metadata_exists"] and case["metadata_guard_exists"], case


def check_exclusive_read(exclusive):
    assert exclusive["original_bytes_preserved"], exclusive
    operations = {item["operation"]: item["observed"] for item in exclusive["cases"]}
    assert set(operations) == {"read", "clear", "stop"}
    assert len(exclusive["cases"]) == 3
    assert all(item["native_share_mode"] == 0 for item in exclusive["cases"]), exclusive
    for operation in ("read", "clear"):
        case = operations[operation]
        assert case["error"] == "HostError", case
        assert case["cause_type"] == "PermissionError" and case["cause_errno"] == errno.EACCES, case
        assert case["path_exists"], case
    case = operations["stop"]
    assert case["error"] is None and case["returned"] == 1 and case["path_exists"], case
    assert exclusive["read_after_release_matches_original"], exclusive
    assert exclusive["read_after_release"] == {"pid": 424242, "started_at": 1}, exclusive


def check_ready_transition(ready):
    assert [event["phase"] for event in ready["events"]] == [
        "SERVE_HELD", "LOCK_BUSY", "SERVER_STARTED", "EXIT"
    ], ready
    assert "workers" in ready, "worker handshakes are required; launcher PID is not worker identity"
    workers = ready["workers"]
    assert set(workers) == {"first", "second"}, ready
    for role, worker in workers.items():
        assert worker["worker_pid"] > 0 and worker["launcher_pid"] > 0, worker
        assert worker["handshake"] == {
            "phase": "WORKER_READY", "role": role, "pid": worker["worker_pid"]
        }, worker
    winner, loser = workers["first"]["worker_pid"], workers["second"]["worker_pid"]
    assert winner != loser, ready
    assert workers["first"]["launcher_pid"] != workers["second"]["launcher_pid"], ready
    exits = {item["role"]: item for item in ready["process_exits"]}
    assert set(exits) == {"first", "second"} and len(ready["process_exits"]) == 2, ready
    for role, expected_code in (("first", 0), ("second", 1)):
        item = exits[role]
        assert item["worker_pid"] == workers[role]["worker_pid"], item
        assert item["launcher_pid"] == workers[role]["launcher_pid"], item
        assert item["exit"] == expected_code, item
        event = item["event"]
        assert event["phase"] == "EXIT" and event["role"] == role, item
        assert event["pid"] == item["worker_pid"] and event["returned"] == expected_code, item
    held, busy, started, rejected = ready["events"]
    assert held["role"] == started["role"] == "first", ready
    assert held["pid"] == started["pid"] == winner, ready
    assert busy["role"] == rejected["role"] == "second", ready
    assert busy["pid"] == rejected["pid"] == loser, ready
    assert rejected["returned"] == 1, ready
    assert ready["current_owner"] == {"pid": winner, "port": held["port"]}, ready
    assert ready["snapshot_health"] == [
        {"pid": winner, "port": held["port"], "alive": True}
    ], ready
    assert not ready["two_distinct_hosts_alive"], ready
    assert ready["daemon_alive_before_cleanup"] and not ready["daemon_alive_after_cleanup"], ready
    assert exits["first"]["stderr"] == "", ready
    assert exits["second"]["stderr"] == f"Host 已在运行：http://127.0.0.1:{held['port']}/\n", ready
    assert not ready.get("forced_cleanup") and not ready.get("cleanup_http_errors"), ready


def check_monitor_start(monitor):
    assert [event["phase"] for event in monitor["events"]] == [
        "MONITOR_START_HELD", "SERVER_READY", "EXIT"
    ], monitor
    held, ready, outcome = monitor["events"]
    assert held["thread_published"] and held["port"] == ready["port"], monitor
    assert not monitor["http_alive_at_thread_start_boundary"], monitor
    assert monitor["stopper"]["stderr"] == monitor["stderr"] == "", monitor
    client = monitor["boundary_client"]
    assert client["timed_out_before_release"] and client["received_before_release"] == 0, client
    assert client["kept_open_at_release"] and client["full_response_drained"], client
    assert client["health_pid"] == held["pid"] and client["response_bytes"] > 0, client
    assert monitor["stopper"]["exit"] == monitor["process_exit"] == 0, monitor
    assert monitor["remaining_stdout"] == "", monitor
    assert not monitor["daemon_alive_after_stop"], monitor
    assert not monitor["daemon_alive_after_exit"], monitor
    assert monitor["metadata_absent_after_exit"], monitor
    assert outcome["returned"] == 0, monitor
    assert not outcome["log_open"] and not outcome["monitor_alive"], monitor
    assert outcome["bridge_closed"], monitor


CHECKS = {
    "interleavings": check_interleavings,
    "exclusive-read": check_exclusive_read,
    "ready-transition": check_ready_transition,
    "monitor-start": check_monitor_start,
}

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--sections", nargs="+", choices=CHECKS, default=list(CHECKS))
    args = parser.parse_args()
    for section in args.sections:
        data = json.loads((args.root / f"{section}.json").read_text(encoding="utf-8"))
        CHECKS[section](data)
    print("Verified observed sections: " + ", ".join(args.sections))
