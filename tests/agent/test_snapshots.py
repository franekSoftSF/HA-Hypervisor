"""Snapshots on vSphere (fake client) and Proxmox (simulated API): create, revert, delete, scenarios."""

from __future__ import annotations

import pytest

from ha_hypervisor_agent.operations import OperationFailed, validate_snapshot
from ha_hypervisor_agent.protocol import (
    CMD_SCENARIO_START,
    CMD_SNAPSHOT_CREATE,
    CMD_SNAPSHOT_DELETE,
    CMD_SNAPSHOT_REVERT,
    OP_COMPLETED,
    OP_FAILED,
)
from test_operations import scenario
from test_proxmox import single  # noqa: F401  (fixture)
from test_proxmox import _plain_http  # noqa: F401  (autouse fixture)


def body(*vms, name="Demo", **extra):
    return {"vms": list(vms), "name": name, **extra}


@pytest.mark.parametrize(
    "bad",
    [
        {"vms": [], "name": "Demo"},
        {"vms": ["a"], "name": "1st"},
        {"vms": ["a"], "name": "has space"},
        {"vms": ["a"], "name": "x"},
        {"vms": "a", "name": "Demo"},
        {"name": "Demo"},
    ],
)
def test_snapshot_commands_are_validated(bad):
    with pytest.raises(OperationFailed) as err:
        validate_snapshot(bad)
    assert err.value.code == "invalid_request"


# ------------------------------------------------------------------- vSphere


@pytest.fixture
def demo(harness):
    harness.lab.add("vm-1", "h1", name="web")
    harness.lab.add("vm-2", "h2", name="db")
    return harness


def test_vsphere_create_revert_delete(demo):
    operation = demo.run(CMD_SNAPSHOT_CREATE, body("vm-1", "vm-2", memory=True))
    assert operation["status"] == OP_COMPLETED, operation
    assert operation["plan"] == {"snapshot": "Demo", "vms": ["web", "db"]}
    assert demo.lab.vms["vm-1"].snapshots == {"Demo": "on"}

    demo.lab.vms["vm-1"].power = "off"
    operation = demo.run(CMD_SNAPSHOT_REVERT, body("vm-1"))
    assert operation["status"] == OP_COMPLETED, operation
    assert demo.lab.vms["vm-1"].power == "on"  # memory snapshot: running again

    operation = demo.run(CMD_SNAPSHOT_DELETE, body("vm-1", "vm-2"))
    assert operation["status"] == OP_COMPLETED, operation
    assert demo.lab.vms["vm-2"].snapshots == {}


def test_vsphere_disk_snapshot_revert_can_start_the_vm(demo):
    demo.run(CMD_SNAPSHOT_CREATE, body("vm-1"))
    assert demo.lab.vms["vm-1"].snapshots == {"Demo": "off"}
    operation = demo.run(CMD_SNAPSHOT_REVERT, body("vm-1", start=True))
    assert operation["status"] == OP_COMPLETED, operation
    assert demo.lab.order("revert") == ["revert:vm-1:Demo"]
    assert demo.lab.vms["vm-1"].power == "on"


def test_existing_name_is_refused_before_any_snapshot(demo):
    demo.lab.vms["vm-2"].snapshots["Demo"] = "off"
    operation = demo.run(CMD_SNAPSHOT_CREATE, body("vm-1", "vm-2"))
    assert operation["status"] == OP_FAILED
    assert operation["error_code"] == "snapshot_exists"
    assert demo.lab.vms["vm-1"].snapshots == {}


def test_revert_needs_the_snapshot_on_every_vm_first(demo):
    demo.lab.vms["vm-1"].snapshots["Demo"] = "off"
    operation = demo.run(CMD_SNAPSHOT_REVERT, body("vm-1", "vm-2"))
    assert operation["error_code"] == "snapshot_not_found"
    assert "db" in operation["error"]
    assert demo.lab.order("revert") == []


def test_vcenter_vm_is_never_reverted(demo):
    demo.lab.vms["vcsa"].snapshots["Demo"] = "on"
    operation = demo.run(CMD_SNAPSHOT_REVERT, body("vcsa"))
    assert operation["error_code"] == "vm_protected"
    # A snapshot of vCenter is harmless and allowed.
    operation = demo.run(CMD_SNAPSHOT_CREATE, body("vcsa", name="Before"))
    assert operation["status"] == OP_COMPLETED


def test_vm_on_a_stopped_host_is_unreachable(demo):
    demo.run(CMD_SNAPSHOT_CREATE, body("vm-2"))  # remembered with its host
    demo.lab.hosts_up.discard("h2")
    operation = demo.run(CMD_SNAPSHOT_REVERT, body("vm-2"))
    assert operation["error_code"] == "vm_unreachable"


def test_scenario_reverts_before_starting(demo):
    demo.lab.vms["vm-1"].power = "off"
    demo.lab.vms["vm-2"].power = "off"
    demo.run(CMD_SNAPSHOT_CREATE, body("vm-1", "vm-2", name="Clean"))
    operation = demo.run(CMD_SCENARIO_START, scenario(["vm-2"], ["vm-1"], snapshot="Clean"))
    assert operation["status"] == OP_COMPLETED, operation
    events = demo.lab.order("revert") + demo.lab.order("on:")
    assert events == ["revert:vm-2:Clean", "revert:vm-1:Clean", "on:vm-2:vcenter", "on:vm-1:vcenter"]


def test_scenario_with_a_missing_snapshot_touches_nothing(demo):
    demo.lab.vms["vm-1"].power = "off"
    demo.lab.vms["vm-1"].snapshots["Clean"] = "off"
    operation = demo.run(CMD_SCENARIO_START, scenario(["vm-1", "vm-2"], snapshot="Clean"))
    assert operation["error_code"] == "snapshot_not_found"
    assert demo.lab.order("revert") == [] and demo.lab.order("on:") == []


def test_scenario_snapshot_name_is_validated(demo):
    with pytest.raises(OperationFailed):
        demo.orchestrator.start(CMD_SCENARIO_START, scenario(["vm-1"], snapshot="bad name"))


# ------------------------------------------------------------------- Proxmox


def test_proxmox_snapshots_are_in_the_state(single):
    single.guest("dc01").snapshots["Base"] = {"vmstate": False, "running": False}
    view = single.view()
    assert view.vms["qemu-100"]["snapshots"] == ["Base"]
    assert view.vms["lxc-200"]["snapshots"] == []


def test_proxmox_create_revert_delete(single):
    operation = single.run(CMD_SNAPSHOT_CREATE, body("qemu-100", "lxc-200", memory=True))
    assert operation["status"] == OP_COMPLETED, operation
    # Memory exists for QEMU only, a container gets a disk snapshot.
    assert single.guest("dc01").snapshots["Demo"]["vmstate"] is True
    assert single.guest("dns").snapshots["Demo"]["vmstate"] is False

    operation = single.run(CMD_SNAPSHOT_REVERT, body("qemu-100", "lxc-200", start=True))
    assert operation["status"] == OP_COMPLETED, operation
    assert "rollback:dc01:Demo" in single.cluster.events
    assert single.guest("dc01").running  # memory snapshot
    assert single.guest("dns").running  # started after the rollback

    operation = single.run(CMD_SNAPSHOT_DELETE, body("qemu-100", "lxc-200"))
    assert operation["status"] == OP_COMPLETED, operation
    assert single.guest("dc01").snapshots == {}


def test_proxmox_running_container_is_stopped_before_rollback(single):
    single.run(CMD_SNAPSHOT_CREATE, body("lxc-200"))
    assert single.guest("dns").running
    operation = single.run(CMD_SNAPSHOT_REVERT, body("lxc-200"))
    assert operation["status"] == OP_COMPLETED, operation
    assert not single.guest("dns").running


def test_proxmox_scenario_reverts_lab_after_a_demo(single):
    for guest in single.cluster.guests.values():
        guest.running = False
    single.run(CMD_SNAPSHOT_CREATE, body("qemu-100", "lxc-200", name="Clean"))
    single.guest("dc01").running = True  # the demo changed things
    operation = single.run(
        CMD_SCENARIO_START,
        {"scenario": {**scenario(["lxc-200"], ["qemu-100"])["scenario"], "snapshot": "Clean"}},
    )
    assert operation["status"] == OP_COMPLETED, operation
    assert single.cluster.events.index("rollback:dc01:Clean") < single.cluster.events.index("start:dns")
    assert single.guest("dc01").running and single.guest("dns").running
