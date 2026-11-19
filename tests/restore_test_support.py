"""Fixtures for managed restore tests that use the real local registry."""

from __future__ import annotations

import os
import uuid

import pytest

from tianshu.maintenance import acquire_restore_gate
from tianshu.restore_contract import ReservedV1, RestoreEventV1, RestorePlanV1
from tianshu.restore_registry import RestoreRegistry

requires_managed_restore_writes = pytest.mark.skipif(
    os.name == "nt", reason="Managed restore writes require POSIX directory fsync (#200)"
)


def record_reserved_batch(registry: RestoreRegistry, plan: RestorePlanV1) -> str:
    """Seed the existing reserved state using native identities from a real plan."""
    current = registry.initialize()
    plan_inputs = plan.inputs
    batch_id = str(uuid.uuid4())
    payload = ReservedV1(
        plan=plan_inputs,
        plan_sha256=plan.plan_sha256,
        copy_inventory=plan_inputs.copy_inventory,
        inventory_sha256=plan_inputs.inventory_sha256,
        placement=plan_inputs.placement,
        original_target=plan_inputs.original_target,
        stage_name=f".{plan_inputs.placement.final_name}.restore-{batch_id}",
    )
    event = RestoreEventV1(
        version=1,
        registry_id=current.meta.registry_id,
        sequence=current.next_sequence,
        batch_id=batch_id,
        previous_sha256=current.head_sha256,
        kind="reserved",
        payload=payload,
    )
    gate = acquire_restore_gate(exclusive=True)
    try:
        registry.append_event(event, expected=current, operation_access=gate)
    finally:
        gate.close()
    return batch_id
