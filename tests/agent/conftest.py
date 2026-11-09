"""A simulated lab: hosts, VMs, vCenter and BMCs that react to each other."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from ha_vmware_agent.config import parse
from ha_vmware_agent.operations import Orchestrator
from ha_vmware_agent.power import PowerError
from ha_vmware_agent.store import Store
from ha_vmware_agent.vim import VimNotFound, VimToolsUnavailable, VimUnavailable

KEY = "k" * 40


def make_config(tmp_path, **overrides: Any):
    """Return a two-host configuration with instant timeouts."""
    raw = {
        "agent_id": "lab",
        "shared_key": KEY,
        "mqtt": {"host": "broker"},
        "vcenter": {
            "host": "vc",
            "username": "u",
            "password": "p",
            "vm_name": "vcsa",
        },
        "hosts": [
            {"name": "h1", "address": "h1", "username": "u", "password": "p"},
            {"name": "h2", "address": "h2", "username": "u", "password": "p"},
        ],
        "state_file": str(tmp_path / "state.json"),
        "timeouts": {
            "guest_stop": 0.05,
            "vcenter_stop": 0.05,
            "host_stop": 0.05,
            "host_start": 0.05,
            "vcenter_start": 0.05,
            "host_sync": 0.05,
            "vcls_retreat": 0.02,
            "poll_step": 0.001,
        },
        **overrides,
    }
    return parse(raw)


@dataclass
class Vm:
    id: str
    name: str
    host: str
    power: str = "off"
    tools: bool = True
    ignores_shutdown: bool = False


@dataclass
class Lab:
    vms: dict[str, Vm] = field(default_factory=dict)
    hosts_up: set[str] = field(default_factory=lambda: {"h1", "h2"})
    events: list[str] = field(default_factory=list)
    can_wake: dict[str, bool] = field(default_factory=dict)
    esxi_login_fails: set[str] = field(default_factory=set)
    wake_fails: set[str] = field(default_factory=set)
    vms_boot_tools: bool = True

    def add(self, vm_id: str, host: str, power: str = "on", **kwargs: Any) -> Vm:
        name = kwargs.pop("name", vm_id)
        self.vms[vm_id] = Vm(vm_id, name, host, power, **kwargs)
        return self.vms[vm_id]

    @property
    def vcenter_up(self) -> bool:
        vm = self.vms.get("vcsa")
        return vm is not None and vm.power == "on" and vm.host in self.hosts_up

    def order(self, prefix: str = "") -> list[str]:
        return [event for event in self.events if event.startswith(prefix)]


class FakeClient:
    def __init__(self, lab: Lab, host: str | None) -> None:
        self.lab = lab
        self.host = host
        self.via = host or "vcenter"
        self.version = "fake"

    def _vm(self, vm_id: str) -> Vm:
        vm = self.lab.vms.get(vm_id)
        if vm is None or (self.host and vm.host != self.host):
            raise VimNotFound(vm_id)
        return vm

    def collect(self, host_name: str | None = None) -> dict[str, list[dict[str, Any]]]:
        if self.host is None and not self.lab.vcenter_up:
            raise VimUnavailable("vcenter down")
        if self.host is not None and self.host not in self.lab.hosts_up:
            raise VimUnavailable("host down")
        names = [self.host] if self.host else ["h1", "h2"]
        hosts = [
            {
                "id": name,
                "name": name,
                "connected": name in self.lab.hosts_up,
                "power": "on" if name in self.lab.hosts_up else "off",
                "maintenance": False,
                "cpu_used_mhz": 100,
                "cpu_total_mhz": 1000,
                "mem_used_mb": 100,
                "mem_total_mb": 1000,
                "uptime_s": 1,
                "vendor": None,
                "model": None,
                "version": None,
            }
            for name in names
        ]
        vms = [
            {
                "id": vm.id,
                "name": vm.name,
                "power": vm.power if vm.host in self.lab.hosts_up else "off",
                "host": vm.host,
                "cpu_mhz": 0,
                "mem_mb": 0,
                "mem_total_mb": 0,
                "cpus": 1,
                "storage_gb": 0,
                "tools": vm.tools and vm.power == "on",
                "ip": None,
                "guest_os": None,
                "uptime_s": 0,
            }
            for vm in self.lab.vms.values()
            if self.host is None or vm.host == self.host
        ]
        clusters = [{"id": "domain-c1", "name": "c1"}] if self.host is None else []
        return {"hosts": hosts, "vms": vms, "datastores": [], "clusters": clusters}

    def vm_status(self, vm_id: str) -> dict[str, Any]:
        vm = self._vm(vm_id)
        return {"power": vm.power, "tools": vm.tools and vm.power == "on"}

    def power_on(self, vm_id: str) -> None:
        vm = self._vm(vm_id)
        vm.power = "on"
        vm.tools = self.lab.vms_boot_tools
        self.lab.events.append(f"on:{vm_id}:{self.via}")

    def power_off(self, vm_id: str) -> None:
        self._vm(vm_id).power = "off"
        self.lab.events.append(f"force:{vm_id}:{self.via}")

    def shutdown_guest(self, vm_id: str) -> None:
        vm = self._vm(vm_id)
        if not vm.tools:
            raise VimToolsUnavailable(vm_id)
        self.lab.events.append(f"guest:{vm_id}:{self.via}")
        if not vm.ignores_shutdown:
            vm.power = "off"

    def shutdown_host(self) -> None:
        assert self.host is not None
        self.lab.events.append(f"host:{self.host}")
        self.lab.hosts_up.discard(self.host)

    def set_vcls_enabled(self, cluster_id: str, enabled: bool) -> None:
        self.lab.events.append(f"vcls:{cluster_id}:{enabled}")
        if not enabled:
            for vm in self.lab.vms.values():
                if vm.name.startswith("vCLS"):
                    vm.power = "off"


class FakePower:
    def __init__(self, lab: Lab, host: str) -> None:
        self.lab = lab
        self.host = host
        self.methods: list[Any] = []

    def state(self) -> str:
        return "on" if self.host in self.lab.hosts_up else "off"

    def can_wake(self) -> bool:
        return self.lab.can_wake.get(self.host, True)

    def verified(self) -> bool:
        return self.can_wake()

    def power_on(self) -> str:
        if self.host in self.lab.wake_fails:
            raise PowerError("bmc refused")
        self.lab.events.append(f"wake:{self.host}")
        self.lab.hosts_up.add(self.host)
        return "redfish"


class FakeInfra:
    def __init__(self, lab: Lab) -> None:
        self.lab = lab

    def vcenter(self) -> FakeClient:
        if not self.lab.vcenter_up:
            raise VimUnavailable("vcenter down")
        return FakeClient(self.lab, None)

    def esxi(self, host: Any) -> FakeClient:
        if host.name not in self.lab.hosts_up or host.name in self.lab.esxi_login_fails:
            raise VimUnavailable("esxi down")
        return FakeClient(self.lab, host.name)

    def power(self, host: Any) -> FakePower:
        return FakePower(self.lab, host.name)

    def reachable(self, host: Any) -> bool:
        return host.name in self.lab.hosts_up


@dataclass
class Harness:
    lab: Lab
    config: Any
    store: Store
    orchestrator: Orchestrator
    updates: list[dict[str, Any]]

    def run(self, kind: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        self.orchestrator.start(kind, body or {})
        self.orchestrator.wait(10)
        operation = self.orchestrator.operation
        assert operation is not None
        return operation


@pytest.fixture
def lab() -> Lab:
    result = Lab()
    result.add("vcsa", "h1")
    return result


@pytest.fixture
def harness(lab: Lab, tmp_path) -> Harness:
    config = make_config(tmp_path)
    store = Store(config.state_file)
    updates: list[dict[str, Any]] = []
    orchestrator = Orchestrator(config, FakeInfra(lab), store, updates.append)
    return Harness(lab, config, store, orchestrator, updates)
