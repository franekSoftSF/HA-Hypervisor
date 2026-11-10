"""Proxmox VE against the simulated API: one node, a cluster, a cluster with HA."""

from __future__ import annotations

import time
from typing import Any

import pytest

from fake_pve import SECRET, TOKEN, Cluster
from ha_hypervisor_agent.config import parse
from ha_hypervisor_agent.inventory import build_state
from ha_hypervisor_agent.operations import OperationFailed
from ha_hypervisor_agent.power import POWER_OFF, POWER_ON
from ha_hypervisor_agent.protocol import (
    CMD_CLUSTER_SHUTDOWN,
    CMD_CLUSTER_START,
    CMD_SCENARIO_START,
    CMD_SCENARIO_STOP,
    OP_COMPLETED,
    OP_FAILED,
)
from ha_hypervisor_agent.pve import ProxmoxClient, PveInfra, build_pve_view
from ha_hypervisor_agent.pve_operations import ProxmoxOrchestrator
from ha_hypervisor_agent.store import Store

KEY = "k" * 40


@pytest.fixture(autouse=True)
def _plain_http(monkeypatch):
    monkeypatch.setattr(ProxmoxClient, "SCHEME", "http")


class FakePower:
    """BMC of a simulated node."""

    def __init__(self, cluster: Cluster, name: str, wake: bool = True) -> None:
        self.cluster = cluster
        self.name = name
        self.wake = wake
        self.methods = [type("M", (), {"name": "redfish"})()] if wake else []

    def state(self) -> str:
        return POWER_ON if self.cluster.nodes[self.name].online else POWER_OFF

    def can_wake(self) -> bool:
        return self.wake

    def verified(self) -> bool:
        return self.wake

    def power_on(self) -> str:
        self.cluster.power_on(self.name)
        return "redfish"


class Lab:
    def __init__(self, tmp_path, cluster: Cluster, monkeypatch, **overrides: Any) -> None:
        self.cluster = cluster
        self.no_wake: set[str] = set()
        self.config = parse(
            {
                "agent_id": "pve",
                "shared_key": KEY,
                "platform": "proxmox",
                "mqtt": {"host": "broker"},
                "proxmox": {"token_id": TOKEN, "token_secret": SECRET},
                "hosts": [
                    {"name": node.name, "address": "127.0.0.1", "port": node.port}
                    for node in cluster.nodes.values()
                ],
                "state_file": str(tmp_path / "state.json"),
                "timeouts": {
                    "guest_stop": 0.3,
                    "host_stop": 3,
                    "host_start": 3,
                    "host_sync": 3,
                    "poll_step": 0.02,
                },
                **overrides,
            }
        )
        self.infra = PveInfra(self.config)
        powers: dict[str, FakePower] = {}

        def power(host):
            if host.name not in powers:
                powers[host.name] = FakePower(cluster, host.name, host.name not in self.no_wake)
            return powers[host.name]

        monkeypatch.setattr(self.infra, "power", power)
        self.store = Store(self.config.state_file)
        self.updates: list[dict[str, Any]] = []
        self.orchestrator = ProxmoxOrchestrator(
            self.config, self.infra, self.store, self.updates.append
        )

    def run(self, kind: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        self.orchestrator.start(kind, body or {})
        self.orchestrator.wait(30)
        return self.orchestrator.operation

    def view(self):
        return build_pve_view(self.config, self.infra)

    def guest(self, name: str):
        return next(g for g in self.cluster.guests.values() if g.name == name)


@pytest.fixture
def single(tmp_path, monkeypatch):
    cluster = Cluster()
    cluster.add_node("pve")
    cluster.add_guest(100, "dc01", "pve", running=True)
    cluster.add_guest(101, "web01", "pve", running=True, agent=False)
    cluster.add_guest(200, "dns", "pve", kind="lxc", running=True)
    cluster.add_guest(300, "test", "pve", running=False)
    cluster.add_guest(9000, "template", "pve", template=True)
    yield Lab(tmp_path, cluster, monkeypatch)
    cluster.stop()


def _cluster(tmp_path, monkeypatch, ha: bool) -> Lab:
    cluster = Cluster(name="homelab")
    for name in ("pve1", "pve2", "pve3"):
        cluster.add_node(name)
    state = "started" if ha else None
    cluster.add_guest(100, "dc01", "pve1", running=True, ha=state)
    cluster.add_guest(101, "sql01", "pve2", running=True, ha=state)
    cluster.add_guest(102, "web01", "pve3", running=True)
    cluster.add_guest(200, "dns", "pve3", kind="lxc", running=True, ha=state)
    cluster.add_guest(103, "app01", "pve2", running=False)
    return Lab(tmp_path, cluster, monkeypatch)


@pytest.fixture
def cluster(tmp_path, monkeypatch):
    lab = _cluster(tmp_path, monkeypatch, ha=False)
    yield lab
    lab.cluster.stop()


@pytest.fixture
def ha(tmp_path, monkeypatch):
    lab = _cluster(tmp_path, monkeypatch, ha=True)
    yield lab
    lab.cluster.stop()


# ------------------------------------------------------------------- reading


def test_single_node_state(single):
    view = single.view()
    state = build_state(single.config, single.infra, single.store, view)
    assert state["platform"] == "proxmox"
    assert state["cluster"] == "on"
    assert state["vcenter"] == {"reachable": True, "version": "Proxmox VE 9.0.10"}
    (host,) = state["hosts"]
    assert host["name"] == "pve"
    assert host["cpu_total_mhz"] == 8 * 2200
    assert host["cpu_used_mhz"] == 4400
    assert host["mem_total_mb"] == 32 * 1024
    assert host["version"] == "9.0.10"
    vms = {vm["name"]: vm for vm in state["vms"]}
    assert set(vms) == {"dc01", "web01", "dns", "test"}  # no template
    assert vms["dc01"]["id"] == "qemu-100"
    assert vms["dc01"]["tools"] is True
    assert vms["dc01"]["ip"] == "10.0.1.100"
    assert vms["web01"]["tools"] is False  # no guest agent
    assert vms["dns"]["id"] == "lxc-200"
    assert vms["dns"]["ip"] == "10.0.0.200"
    assert vms["test"]["power"] == "off"
    (store,) = state["datastores"]
    assert store["name"] == "local-lvm"
    assert store["free_gb"] == 300


def test_cluster_lists_shared_storage_once(cluster):
    names = sorted(store["name"] for store in cluster.view().datastores)
    assert names == ["local-lvm (pve1)", "local-lvm (pve2)", "local-lvm (pve3)", "nfs"]


def test_node_down_makes_cluster_degraded_and_keeps_its_guests(cluster):
    cluster.cluster.power_off("pve3", "test")
    view = cluster.view()
    state = build_state(cluster.config, cluster.infra, cluster.store, view)
    assert state["cluster"] == "degraded"
    hosts = {host["name"]: host for host in state["hosts"]}
    assert hosts["pve3"]["connected"] is False
    assert hosts["pve3"]["power"] == POWER_OFF
    vms = {vm["name"]: vm for vm in state["vms"]}
    assert vms["web01"]["power"] == "off"
    assert vms["web01"]["host"] == "pve3"


def test_wrong_token_is_reported(single, monkeypatch):
    from ha_hypervisor_agent.errors import PlatformAuthError

    client = ProxmoxClient("127.0.0.1", single.cluster.nodes["pve"].port, TOKEN, "wrong")
    with pytest.raises(PlatformAuthError):
        client.version()


# ------------------------------------------------------------------ shutdown


def test_single_node_shutdown(single):
    operation = single.run(CMD_CLUSTER_SHUTDOWN)
    assert operation["status"] == OP_COMPLETED, operation
    assert operation["plan"]["hosts"] == ["pve"]
    assert operation["plan"]["ha_resources"] == []
    assert not single.cluster.nodes["pve"].online
    assert single.cluster.violations == []
    # Guests stopped one by one, the node last.
    assert single.cluster.events[-1] == "node_off:pve"
    assert "shutdown:dc01" in single.cluster.events


def test_guest_ignoring_shutdown_is_stopped(single):
    single.guest("dc01").ignores_shutdown = True
    operation = single.run(CMD_CLUSTER_SHUTDOWN)
    assert operation["status"] == OP_COMPLETED, operation
    assert "stop:dc01" in single.cluster.events
    assert single.cluster.violations == []


def test_dry_run_changes_nothing(cluster):
    operation = cluster.run(CMD_CLUSTER_SHUTDOWN, {"dry_run": True})
    assert operation["status"] == OP_COMPLETED
    assert operation["plan"]["vms"] == ["dc01", "dns", "sql01", "web01"]
    assert operation["plan"]["hosts"] == ["pve1", "pve2", "pve3"]
    assert all(node.online for node in cluster.cluster.nodes.values())
    assert not any(event.startswith(("shutdown", "stop")) for event in cluster.cluster.events)


def test_preflight_refuses_a_node_that_cannot_be_woken(cluster):
    cluster.no_wake.add("pve2")
    operation = cluster.run(CMD_CLUSTER_SHUTDOWN)
    assert operation["status"] == OP_FAILED
    assert operation["error_code"] == "no_wake_method"
    assert cluster.guest("dc01").running


def test_preflight_refuses_a_protected_guest(tmp_path, monkeypatch, cluster):
    lab = Lab(tmp_path, cluster.cluster, monkeypatch, protected_vms=["101"])
    operation = lab.run(CMD_CLUSTER_SHUTDOWN)
    assert operation["error_code"] == "protected_vm_running"
    assert cluster.guest("sql01").running


def test_preflight_refuses_without_quorum(cluster):
    cluster.cluster.power_off("pve2", "test")
    cluster.cluster.power_off("pve3", "test")
    operation = cluster.run(CMD_CLUSTER_SHUTDOWN)
    assert operation["status"] == OP_FAILED
    assert operation["error_code"] == "quorum_timeout"
    assert cluster.cluster.nodes["pve1"].online


def test_cluster_shutdown_and_start_without_ha(cluster):
    operation = cluster.run(CMD_CLUSTER_SHUTDOWN)
    assert operation["status"] == OP_COMPLETED, operation
    assert not any(node.online for node in cluster.cluster.nodes.values())
    assert cluster.cluster.violations == []

    operation = cluster.run(CMD_CLUSTER_START)
    assert operation["status"] == OP_COMPLETED, operation
    assert all(node.online for node in cluster.cluster.nodes.values())
    steps = [update["step"] for update in cluster.updates]
    assert "wait_quorum" in steps
    assert "restore_ha" not in steps
    # Like vSphere: guests stay off until a scenario starts them.
    assert not any(guest.running for guest in cluster.cluster.guests.values())


def test_cluster_with_ha_is_shut_down_without_fencing_and_restored(ha):
    operation = ha.run(CMD_CLUSTER_SHUTDOWN)
    assert operation["status"] == OP_COMPLETED, operation
    assert operation["plan"]["ha_resources"] == ["ct:200", "vm:100", "vm:101"]
    # HA guests went through the HA manager, none was left to be fenced.
    assert "ha_request:dc01=stopped" in ha.cluster.events
    assert ha.cluster.violations == []
    assert ha.store.get("ha_resources") == ["ct:200", "vm:100", "vm:101"]

    operation = ha.run(CMD_CLUSTER_START)
    assert operation["status"] == OP_COMPLETED, operation
    assert "restore_ha" in [update["step"] for update in ha.updates]
    assert ha.guest("dc01").ha == "started"
    assert ha.guest("dc01").running and ha.guest("sql01").running and ha.guest("dns").running
    assert not ha.guest("web01").running  # not HA, waits for a scenario
    assert ha.store.get("ha_resources") == []


def test_ha_resource_removed_meanwhile_is_skipped(ha):
    ha.run(CMD_CLUSTER_SHUTDOWN)
    ha.guest("sql01").ha = None
    operation = ha.run(CMD_CLUSTER_START)
    assert operation["status"] == OP_COMPLETED, operation
    assert not ha.guest("sql01").running


# ----------------------------------------------------------------- scenarios


def _scenario(*groups: list[str], wait: str = "tools", empty: bool = False) -> dict[str, Any]:
    return {
        "scenario": {
            "id": "s1",
            "name": "Lab",
            "steps": [{"vms": list(group), "wait": wait, "delay": 0, "timeout": 3} for group in groups],
            "power_off_empty_hosts": empty,
        }
    }


def test_scenario_wakes_a_single_node_and_starts_guests(single):
    single.run(CMD_CLUSTER_SHUTDOWN)
    operation = single.run(CMD_SCENARIO_START, _scenario(["qemu-100"], ["lxc-200"]))
    assert operation["status"] == OP_COMPLETED, operation
    assert single.cluster.nodes["pve"].online
    assert single.guest("dc01").running and single.guest("dns").running
    assert not single.guest("web01").running


def test_scenario_waits_for_the_guest_agent(single):
    single.guest("web01").running = False
    operation = single.run(CMD_SCENARIO_START, _scenario(["qemu-101"]))
    # web01 has no guest agent: waiting for tools times out.
    assert operation["error_code"] == "vm_start_timeout"
    operation = single.run(CMD_SCENARIO_START, _scenario(["qemu-101"], wait="power"))
    assert operation["status"] == OP_COMPLETED


def test_scenario_starts_the_node_it_needs(cluster):
    cluster.cluster.power_off("pve3", "test")
    cluster.cluster.violations.clear()
    operation = cluster.run(CMD_SCENARIO_START, _scenario(["qemu-102"]))
    assert operation["status"] == OP_COMPLETED, operation
    assert cluster.cluster.nodes["pve3"].online
    assert cluster.guest("web01").running


def test_scenario_stop_keeps_quorum(cluster):
    for guest in cluster.cluster.guests.values():
        guest.running = False
    cluster.run(CMD_SCENARIO_START, _scenario(["qemu-100", "qemu-101", "qemu-102"], wait="power"))
    operation = cluster.run(
        CMD_SCENARIO_STOP, _scenario(["qemu-100", "qemu-101", "qemu-102"], empty=True)
    )
    assert operation["status"] == OP_COMPLETED, operation
    online = [node.name for node in cluster.cluster.nodes.values() if node.online]
    # Three nodes need two for quorum: only one empty node may go.
    assert len(online) == 2
    assert cluster.cluster.violations == []


def test_single_node_scenario_stop_can_power_off_the_node(single):
    for guest in single.cluster.guests.values():
        guest.running = False
    single.run(CMD_SCENARIO_START, _scenario(["qemu-100"], wait="power"))
    operation = single.run(CMD_SCENARIO_STOP, _scenario(["qemu-100"], empty=True))
    assert operation["status"] == OP_COMPLETED, operation
    assert not single.cluster.nodes["pve"].online


def test_ha_guest_scenario_goes_through_ha(ha):
    ha.run(CMD_SCENARIO_STOP, _scenario(["qemu-100"]))
    assert ha.guest("dc01").ha == "stopped"
    operation = ha.run(CMD_SCENARIO_START, _scenario(["qemu-100"]))
    assert operation["status"] == OP_COMPLETED, operation
    assert ha.guest("dc01").ha == "started"
    assert ha.guest("dc01").running


# ------------------------------------------------------------------ one guest


def test_vm_power(single):
    single.orchestrator.vm_power("qemu-300", "on")
    assert single.guest("test").running
    single.orchestrator.vm_power("qemu-300", "shutdown")
    assert not single.guest("test").running
    single.orchestrator.vm_power("lxc-200", "off")
    assert not single.guest("dns").running


def test_vm_power_respects_protection(tmp_path, monkeypatch, single):
    lab = Lab(tmp_path, single.cluster, monkeypatch, protected_vms=["dc01"])
    with pytest.raises(OperationFailed) as err:
        lab.orchestrator.vm_power("qemu-100", "off")
    assert err.value.code == "vm_protected"


def test_vm_power_on_unknown_guest(single):
    with pytest.raises(OperationFailed) as err:
        single.orchestrator.vm_power("qemu-999", "on")
    assert err.value.code == "vm_unreachable"


def test_guest_migrated_by_ha_is_found_again(ha):
    client = ha.infra.any_node()
    ha.view()  # caches the placement
    ha.guest("app01").node = "pve3"
    client.power_on("qemu-103")
    assert ha.guest("app01").running


# ------------------------------------------------------ agent on the node


def wait_off(cluster: Cluster, name: str) -> None:
    """The node goes down a moment after the agent asked, like a real one."""
    deadline = time.monotonic() + 3
    while cluster.nodes[name].online and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not cluster.nodes[name].online


@pytest.fixture
def on_node(tmp_path, monkeypatch, single):
    """The agent runs on the node "pve" itself."""
    monkeypatch.setattr("ha_hypervisor_agent.pve_operations.local_node_name", lambda: "pve")
    monkeypatch.setattr(ProxmoxOrchestrator, "SELF_GRACE", 0)
    lab = Lab(tmp_path / "self", single.cluster, monkeypatch)
    lab.no_wake.add("pve")  # nothing can wake it, the agent is on it
    return lab


def test_agent_on_its_node_shuts_it_down_last_and_reports_first(on_node):
    operation = on_node.run(CMD_CLUSTER_SHUTDOWN, {"dry_run": True})
    assert operation["status"] == OP_COMPLETED, operation
    assert "self_hosted:pve" in operation["plan"]["warnings"]
    assert operation["plan"]["hosts"] == ["pve"]

    operation = on_node.run(CMD_CLUSTER_SHUTDOWN)
    assert operation["status"] == OP_COMPLETED, operation
    assert "runs this agent" in operation["detail"]
    wait_off(on_node.cluster, "pve")
    assert on_node.cluster.violations == []
    # Completed was published before the node went down.
    completed = [u for u in on_node.updates if u["status"] == OP_COMPLETED and not u["dry_run"]]
    assert completed and completed[0]["step"] == "stop_hosts"


def test_agent_on_one_node_of_a_cluster_goes_last(tmp_path, monkeypatch, cluster):
    monkeypatch.setattr("ha_hypervisor_agent.pve_operations.local_node_name", lambda: "pve1")
    monkeypatch.setattr(ProxmoxOrchestrator, "SELF_GRACE", 0)
    lab = Lab(tmp_path / "self", cluster.cluster, monkeypatch)
    operation = lab.run(CMD_CLUSTER_SHUTDOWN)
    assert operation["status"] == OP_COMPLETED, operation
    assert operation["plan"]["hosts"] == ["pve2", "pve3", "pve1"]
    wait_off(lab.cluster, "pve1")
    offs = [e for e in lab.cluster.events if e.startswith("node_off")]
    assert offs == ["node_off:pve2", "node_off:pve3", "node_off:pve1"]


def test_agent_never_stops_its_own_node_as_empty(on_node):
    for guest in on_node.cluster.guests.values():
        guest.running = False
    on_node.run(CMD_SCENARIO_START, _scenario(["qemu-100"], wait="power"))
    operation = on_node.run(CMD_SCENARIO_STOP, _scenario(["qemu-100"], empty=True))
    assert operation["status"] == OP_COMPLETED, operation
    assert on_node.cluster.nodes["pve"].online
