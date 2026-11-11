"""W3 验收：PRD §8.1.1 隔离门禁（四边界真实探针 + 不满足即关闭能力）。

测试即证据：四个探针全部 spawn **真实子进程**（父进程只观察文件/端口/进程），
不用 mock 冒充。每个探针本身都有超时，坏掉的情况必须落成 `unknown` 而不是
"没发现问题 = 通过"。

本机（macOS arm64）实测预期：文件/网络/凭据三个边界 `not_enforced`（本地后端
就是没有沙箱），进程树终止在 POSIX 上是 `enforced`（进程组杀树可确认）。
没有实机的 Windows / 其它架构不在这里假装：相关断言按平台放宽，矩阵文档里
登记为 non-verified。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import pytest

from tianshu import isolation
from tianshu.exec import (
    BOUNDARY_ENFORCED,
    BOUNDARY_NOT_ENFORCED,
    BOUNDARY_UNKNOWN,
    ISOLATION_BOUNDARIES,
    ISOLATION_BOUNDARY_CREDENTIALS,
    ISOLATION_BOUNDARY_FILES,
    ISOLATION_BOUNDARY_NETWORK,
    ISOLATION_BOUNDARY_PROCESS_TREE,
    LOCAL_EXEC_BACKEND,
    LOCAL_EXEC_CAPABILITIES,
    local_isolation_claims,
    platform_label,
    spawn,
)
from tianshu.execpolicy import ExecBackendCapabilities

POSIX = os.name == "posix"


# --------------------------------------------------------------- 夹具


@pytest.fixture(scope="module")
def local_report(tmp_path_factory) -> isolation.IsolationReport:
    root = tmp_path_factory.mktemp("isolation-local")
    return isolation.probe_backend(
        LOCAL_EXEC_BACKEND,
        workspace=root / "ws",
        data_root=root / "data",
    )


@dataclass(frozen=True)
class LyingBackend:
    """**声称**有 app_container 隔离，实际 spawn 交给没有隔离的本地后端。

    负控：门必须按探针证据拒绝，不能被 `capabilities.isolation` 的自称骗过。
    """

    capabilities: ExecBackendCapabilities = field(
        default_factory=lambda: ExecBackendCapabilities(
            name="lying-app-container",
            isolation="app_container",
            supports_pty=True,
            supports_signals=True,
            supports_process_group=True,
            supports_job_objects=True,
            supports_network_isolation=True,
        )
    )

    def spawn(self, argv, *, cwd, env, policy, blob_dir=None, pty=False):
        return spawn(
            argv,
            cwd=cwd,
            env=env,
            policy=policy,
            capabilities=LOCAL_EXEC_CAPABILITIES,
            blob_dir=blob_dir,
            pty=pty,
        )


@dataclass(frozen=True)
class BrokenBackend:
    """spawn 直接坏掉：所有探针必须落 `unknown`，门必须拒绝。"""

    capabilities: ExecBackendCapabilities = field(
        default_factory=lambda: ExecBackendCapabilities(
            name="broken-backend", isolation="container"
        )
    )

    def spawn(self, argv, *, cwd, env, policy, blob_dir=None, pty=False):
        raise RuntimeError("backend spawn 坏了")


@pytest.fixture(scope="module")
def lying_report(tmp_path_factory) -> isolation.IsolationReport:
    root = tmp_path_factory.mktemp("isolation-lying")
    return isolation.probe_backend(
        LyingBackend(),
        workspace=root / "ws",
        data_root=root / "data",
    )


# --------------------------------------------------------------- 报告结构


def test_local_report_covers_four_boundaries(local_report):
    assert local_report.backend == LOCAL_EXEC_CAPABILITIES.name
    assert local_report.platform == platform_label()
    assert {f.boundary for f in local_report.findings} == set(ISOLATION_BOUNDARIES)
    for finding in local_report.findings:
        assert finding.status in (
            BOUNDARY_ENFORCED,
            BOUNDARY_NOT_ENFORCED,
            BOUNDARY_UNKNOWN,
        )
        assert finding.evidence, "每条结论都要有可复现的 evidence"


def test_local_backend_statuses_match_reality(local_report):
    """本机实测：三个边界不隔离；进程树终止 POSIX 可确认、其它平台不宣称。

    Windows 上网络探针可能因为 runner 的 winsock 服务故障而落成 `unknown`
    （实测 [WinError 10106]），那是**探针跑不成**，不是「网络被挡住」。所以
    只对 POSIX 要求三边界都 `not_enforced`；其它平台允许网络 `unknown`，但
    仍要求文件/凭据实测 `not_enforced`（它们不依赖 winsock）。
    """
    expected = {
        ISOLATION_BOUNDARY_FILES: BOUNDARY_NOT_ENFORCED,
        ISOLATION_BOUNDARY_CREDENTIALS: BOUNDARY_NOT_ENFORCED,
    }
    if POSIX:
        expected[ISOLATION_BOUNDARY_NETWORK] = BOUNDARY_NOT_ENFORCED
    for boundary, status in expected.items():
        assert local_report.finding(boundary).status == status, (
            f"{boundary} 实测与预期不符：{local_report.finding(boundary).evidence}"
        )
    if not POSIX:
        # 探针本身跑不成时结论必须是 unknown（fail-closed），不能是 enforced。
        network = local_report.finding(ISOLATION_BOUNDARY_NETWORK)
        assert network.status in (BOUNDARY_NOT_ENFORCED, BOUNDARY_UNKNOWN), (
            f"network 实测与预期不符：{network.evidence}"
        )
    tree = local_report.finding(ISOLATION_BOUNDARY_PROCESS_TREE)
    if POSIX:
        assert tree.status == BOUNDARY_ENFORCED, tree.evidence
    else:
        # Windows 零依赖没有 Job Object：不把这一次观察升级成能力声明。
        assert tree.status in (BOUNDARY_UNKNOWN, BOUNDARY_NOT_ENFORCED), tree.evidence


def test_local_declaration_does_not_overclaim_measured_evidence(local_report):
    """声明与实测必须一致：unknown 的声明不能被实测悄悄升级成 enforced。"""
    claims = local_isolation_claims()
    assert set(claims) == set(ISOLATION_BOUNDARIES)
    for boundary, claim in claims.items():
        measured = local_report.finding(boundary).status
        if claim == BOUNDARY_ENFORCED:
            assert measured == BOUNDARY_ENFORCED, (
                f"{boundary}：声明 enforced 但实测 {measured}"
            )
        elif claim == BOUNDARY_NOT_ENFORCED:
            # 声明保守、实测读不出（探针跑不成 → unknown）是允许的：unknown
            # 与 not_enforced 一样会阻断依赖能力。但不允许实测被升级成
            # enforced 后声明还停在 not_enforced 却不更新。
            assert measured in (BOUNDARY_NOT_ENFORCED, BOUNDARY_UNKNOWN), (
                f"{boundary}：声明 not_enforced 但实测 {measured}"
            )
        else:
            assert measured != BOUNDARY_ENFORCED, (
                f"{boundary}：声明 unknown 却测出 enforced，声明需要更新"
            )


# --------------------------------------------------------------- 门：本地后端


@pytest.mark.parametrize("capability", sorted(isolation.CAPABILITY_BOUNDARIES))
def test_gate_refuses_every_capability_on_local_backend(local_report, capability):
    with pytest.raises(isolation.IsolationRefused) as refused:
        isolation.require_capability(
            LOCAL_EXEC_BACKEND, capability, report=local_report
        )
    error = refused.value
    assert error.capability == capability
    assert error.status in (BOUNDARY_NOT_ENFORCED, BOUNDARY_UNKNOWN)
    # 门是 PolicyUnsupported 语义：改策略不会通过，只能换后端/换平台。
    assert error.feature.startswith("isolation:")


def test_no_dependent_capability_is_enabled_on_local_backend(local_report):
    assert isolation.enabled_capabilities(LOCAL_EXEC_BACKEND, report=local_report) == []


@pytest.mark.skipif(not POSIX, reason="进程组杀树只有 POSIX 能确认")
def test_gate_allows_boundary_with_real_evidence(local_report):
    """正控：进程树边界有实测证据时，门放行这一条边界。"""
    finding = isolation.require_isolation(
        LOCAL_EXEC_BACKEND, ISOLATION_BOUNDARY_PROCESS_TREE, report=local_report
    )
    assert finding.status == BOUNDARY_ENFORCED


def test_gate_without_report_is_unknown_and_refused():
    """没有实测报告 ≠ 隔离成立：fail-closed 成 unknown 并拒绝。"""
    for boundary in ISOLATION_BOUNDARIES:
        with pytest.raises(isolation.IsolationRefused) as refused:
            isolation.require_isolation(LOCAL_EXEC_BACKEND, boundary)
        assert refused.value.status == BOUNDARY_UNKNOWN
    with pytest.raises(isolation.IsolationRefused):
        isolation.require_capability(
            LOCAL_EXEC_BACKEND, isolation.CAPABILITY_MODEL_GENERATED_CODE
        )


# --------------------------------------------------------------- 负控：靠声明骗不过门


def test_lying_backend_is_refused_despite_claiming_isolation(lying_report):
    backend = LyingBackend()
    # 后端自称强隔离——这正是必须被证据打掉的自称。
    assert backend.capabilities.isolation == "app_container"
    assert backend.capabilities.supports_network_isolation is True
    assert lying_report.finding(ISOLATION_BOUNDARY_FILES).status == (
        BOUNDARY_NOT_ENFORCED
    )
    # Windows runner 的 winsock 故障会让网络探针读成 unknown——同样必须被门
    # 拒绝；这里只要求「不是 enforced」，不把探针失败洗成「已挡住」。
    assert lying_report.finding(ISOLATION_BOUNDARY_NETWORK).status in (
        BOUNDARY_NOT_ENFORCED,
        BOUNDARY_UNKNOWN,
    )
    assert lying_report.finding(ISOLATION_BOUNDARY_CREDENTIALS).status == (
        BOUNDARY_NOT_ENFORCED
    )
    with pytest.raises(isolation.IsolationRefused) as refused:
        isolation.require_capability(
            backend,
            isolation.CAPABILITY_UNTRUSTED_LOCAL_MCP,
            report=lying_report,
        )
    assert "not_enforced" in str(refused.value) or "unknown" in str(refused.value)
    assert isolation.enabled_capabilities(backend, report=lying_report) == []


def test_broken_backend_probe_is_unknown_not_enforced(tmp_path):
    """后端自己坏了 → 全部 unknown；门拒绝（不许把探针失败读成"挡住了"）。"""
    report = isolation.probe_backend(
        BrokenBackend(),
        workspace=tmp_path / "ws",
        data_root=tmp_path / "data",
    )
    assert all(f.status == BOUNDARY_UNKNOWN for f in report.findings)
    for boundary in ISOLATION_BOUNDARIES:
        with pytest.raises(isolation.IsolationRefused) as refused:
            isolation.require_isolation(BrokenBackend(), boundary, report=report)
        assert refused.value.status == BOUNDARY_UNKNOWN
    assert isolation.enabled_capabilities(BrokenBackend(), report=report) == []


# --------------------------------------------------------------- 结构校验 / 参数


def test_finding_and_report_validation():
    with pytest.raises(ValueError):
        isolation.IsolationFinding("not_a_boundary", BOUNDARY_ENFORCED, "x")
    with pytest.raises(ValueError):
        isolation.IsolationFinding(ISOLATION_BOUNDARY_FILES, "maybe", "x")
    with pytest.raises(ValueError):
        isolation.IsolationFinding(ISOLATION_BOUNDARY_FILES, BOUNDARY_ENFORCED, "")
    incomplete = (
        isolation.IsolationFinding(ISOLATION_BOUNDARY_FILES, BOUNDARY_ENFORCED, "x"),
    )
    with pytest.raises(ValueError):
        isolation.IsolationReport("b", "macos-arm64", incomplete)


def test_probe_rejects_invalid_timeout(tmp_path):
    for bad in (0, -1, True, 1.5, "100"):
        with pytest.raises(ValueError):
            isolation.probe_backend(
                LOCAL_EXEC_BACKEND,
                workspace=tmp_path / "ws",
                data_root=tmp_path / "data",
                timeout_ms=bad,
            )
