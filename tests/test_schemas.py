"""schema 与 dataclass 的一致性验收。

没有 JSON Schema 校验库（本项目零运行时依赖），所以这里做的是**结构性对照**：
schema 里的 `required` 必须与 dataclass 的必填字段一一对应，
枚举值必须覆盖代码里的常量。它的价值不在于"验证了数据"，而在于
**改了 dataclass 却忘了改 schema 时会红**——契约文件烂掉的方式通常就是这种。

真正的字段级校验要等引入校验库（或 Host 侧 Rust）时补，不能把这里当成
"契约已冻结"的证据。
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from tianshu.jobs import JOB_OUTCOMES, JobClaim
from tianshu.permissions import (
    ACTION_CREATE,
    ACTION_DELETE,
    ACTION_EXEC,
    ACTION_OVERWRITE,
    ACTION_READ,
    APPROVAL_STATES,
    ActionRequest,
    Approval,
    Grant,
)
from tianshu.tasks import EFFECT_STATES, RUN_STATES, Run, Task

SCHEMAS = Path(__file__).resolve().parents[1] / "schemas" / "m2"


def _schema(name: str) -> dict:
    return json.loads((SCHEMAS / name).read_text(encoding="utf-8"))


def _required_fields(dataclass_type) -> set[str]:
    """没有默认值的字段 = 必填。"""
    out = set()
    for f in dataclasses.fields(dataclass_type):
        if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING:
            out.add(f.name)
    return out


@pytest.mark.parametrize(
    "schema_file,cls",
    [
        ("task.json", Task),
        ("run.json", Run),
        ("job.json", JobClaim),
        ("approval.json", Approval),
        ("grant.json", Grant),
    ],
)
def test_schema_required_matches_dataclass(schema_file, cls):
    schema = _schema(schema_file)
    assert set(schema["required"]) == _required_fields(cls), (
        f"{schema_file} 的 required 与 {cls.__name__} 的必填字段不一致"
    )


@pytest.mark.parametrize(
    "schema_file,cls",
    [
        ("task.json", Task),
        ("run.json", Run),
        ("job.json", JobClaim),
        ("approval.json", Approval),
        ("grant.json", Grant),
    ],
)
def test_schema_declares_every_field(schema_file, cls):
    schema = _schema(schema_file)
    declared = set(schema["properties"])
    actual = {f.name for f in dataclasses.fields(cls)}
    assert declared == actual, (
        f"{schema_file} 与 {cls.__name__} 的字段集合不一致："
        f"仅 schema={declared - actual} 仅代码={actual - declared}"
    )


def test_approval_state_enum_is_complete():
    schema = _schema("approval.json")
    assert set(schema["properties"]["state"]["enum"]) == APPROVAL_STATES


def test_action_enum_is_complete():
    expected = {ACTION_READ, ACTION_CREATE, ACTION_OVERWRITE, ACTION_DELETE, ACTION_EXEC}
    assert set(_schema("approval.json")["properties"]["action"]["enum"]) == expected
    assert set(_schema("grant.json")["properties"]["action"]["enum"]) == expected


def test_run_effect_state_enum_is_complete():
    schema = _schema("run.json")
    enum = set(schema["properties"]["effect_state"]["enum"]) - {None}
    assert enum == EFFECT_STATES


def test_run_outcome_enum_matches_job_outcomes():
    schema = _schema("run.json")
    enum = set(schema["properties"]["outcome"]["enum"]) - {None}
    assert enum == JOB_OUTCOMES


def test_run_state_enum_matches_prd_state_machine():
    """PRD §4.3 的状态机有 11 个状态，schema 少一个就等于"这份契约写不出来"。"""
    schema = _schema("run.json")
    assert set(schema["properties"]["state"]["enum"]) == RUN_STATES


def test_run_control_request_enum_is_closed():
    """控制请求只有暂停和取消两种；别留一个 schema 认、代码不认的值。"""
    from tianshu.tasks import CONTROL_REQUESTS

    schema = _schema("run.json")
    enum = set(schema["properties"]["control_requested"]["enum"]) - {None}
    assert enum == CONTROL_REQUESTS


def test_task_workspace_mode_enum_matches_binding():
    from tianshu.workspace import WorkspaceBinding

    schema = _schema("task.json")
    item = schema["properties"]["workspaces"]["items"]
    assert set(item["properties"]["mode"]["enum"]) == set(WorkspaceBinding.MODES)
    assert set(item["properties"]["snapshot_policy"]["enum"]) == set(
        WorkspaceBinding.SNAPSHOT_POLICIES
    )


def test_action_request_digest_shape_is_documented():
    """ActionRequest 没有单独 schema：它是**输入**，摘要就是它的规范化形式。

    这里钉住两件事：

    1. 请求的字段集合必须与摘要里绑住的字段一致——少绑一个字段等于给旧审批
       留一条复用通道。
    2. `description_hash`（PRD §5.2.3「任务绑定固定…描述摘要」）是**条件字段**：
       只有非空时才进规范字典。空的（历史请求）逐字节不写，否则历史库里的审批、
       grant 与派发记录的摘要会集体变化——那不是"更安全"，是把既有授权链作废。

    第 2 条是本轮唯一改动的既有判据：旧口径是"字典键集合 ≡ 字段集合"，
    新口径是"字典键集合 = 字段集合 − 空的可选绑定字段"。理由是语义真的变了，
    不是为了让测试变绿。
    """
    request = ActionRequest(
        request_id="r",
        task_id="t",
        run_id="run",
        job_id="job",
        claim_epoch=1,
        owner_instance_id="i",
        goal_revision=1,
        lease_epoch=1,
        call_id="c",
        attempt_id=1,
        tool_id="tool",
        tool_version="1",
        schema_hash="s",
        action="read",
        resources=("/ws/a",),
        args_hash="a",
    )
    fields = {f.name for f in dataclasses.fields(ActionRequest)}
    assert set(request.to_canonical_dict()) == fields - {"description_hash"}

    bound = dataclasses.replace(request, description_hash="d" * 64)
    assert set(bound.to_canonical_dict()) == fields