"""Builds one picture of the environment from whatever is running."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .config import AgentConfig
from .protocol import CLUSTER_DEGRADED, CLUSTER_NOT_CONFIGURED, CLUSTER_OFF, CLUSTER_ON
from .store import Store
from .vim import Infra, VimClient, VimError, VimUnavailable

_LOGGER = logging.getLogger(__name__)

SOURCE_VCENTER = "vcenter"
SOURCE_ESXI = "esxi"
SOURCE_NONE = "none"
SOURCE_PROXMOX = "proxmox"
# Sources that see every host, also the ones that are down.
FULL_SOURCES = (SOURCE_VCENTER, SOURCE_PROXMOX)


@dataclass
class View:
    """The environment as seen right now."""

    source: str = SOURCE_NONE
    vcenter: VimClient | None = None
    vcenter_version: str | None = None
    hosts: dict[str, dict[str, Any]] = field(default_factory=dict)
    vms: dict[str, dict[str, Any]] = field(default_factory=dict)
    datastores: list[dict[str, Any]] = field(default_factory=list)
    clusters: list[dict[str, Any]] = field(default_factory=list)
    # Proxmox: HA resource id -> requested state.
    ha: dict[str, Any] = field(default_factory=dict)
    # The session able to control each VM.
    clients: dict[str, VimClient] = field(default_factory=dict)


def is_system_vm(vm: dict[str, Any]) -> bool:
    """Return True for vSphere Cluster Services agent VMs."""
    return str(vm["name"]).startswith("vCLS")


def build_view(config: AgentConfig, infra: Infra) -> View:
    """Ask vCenter, or every reachable ESXi host when vCenter is down."""
    view = View()
    try:
        client = infra.vcenter()
        data = client.collect()
    except (VimUnavailable, VimError) as err:
        _LOGGER.debug("vCenter is not available: %s", err)
    else:
        view.source = SOURCE_VCENTER
        view.vcenter = client
        view.vcenter_version = client.version
        view.clusters = data["clusters"]
        _merge(view, data, client)
        return view

    for host in config.hosts:
        if not infra.reachable(host):
            continue
        try:
            client = infra.esxi(host)
            _merge(view, client.collect(host.name), client)
        except (VimUnavailable, VimError) as err:
            _LOGGER.debug("ESXi %s is not available: %s", host.name, err)
            continue
        view.source = SOURCE_ESXI
    return view


def _merge(view: View, data: dict[str, Any], client: VimClient) -> None:
    view.hosts.update({host["id"]: host for host in data["hosts"]})
    view.datastores.extend(data["datastores"])
    for vm in data["vms"]:
        view.vms[vm["id"]] = vm
        view.clients[vm["id"]] = client


def remember(view: View, config: AgentConfig, store: Store) -> None:
    """Save the VM placement while it can still be read."""
    if view.source == SOURCE_NONE:
        return
    known = store.get("vms")
    if view.source in FULL_SOURCES:
        # vCenter and Proxmox see everything, anything they do not list is gone.
        known = {}
    known.update(view.vms)
    store.set("vms", known)

    name = config.vcenter_vm_name
    vcenter_vm = next((vm for vm in view.vms.values() if vm["name"] == name), None)
    if vcenter_vm is not None and vcenter_vm.get("host"):
        store.set("vcenter_host", vcenter_vm["host"])


def _down_host(name: str, power: str) -> dict[str, Any]:
    return {
        "id": name,
        "name": name,
        "connected": False,
        "power": power,
        "maintenance": False,
        "cpu_used_mhz": 0,
        "cpu_total_mhz": 0,
        "mem_used_mb": 0,
        "mem_total_mb": 0,
        "uptime_s": 0,
        "vendor": None,
        "model": None,
        "version": None,
    }


def cluster_state(config: AgentConfig, view: View) -> str:
    """Return on, degraded, off or not_configured."""
    if not config.configured:
        return CLUSTER_NOT_CONFIGURED
    connected = {name for name, host in view.hosts.items() if host["connected"]}
    if view.source in FULL_SOURCES and all(h.name in connected for h in config.hosts):
        return CLUSTER_ON
    return CLUSTER_DEGRADED if connected else CLUSTER_OFF


def build_state(
    config: AgentConfig, infra: Infra, store: Store, view: View
) -> dict[str, Any]:
    """Return the state document published to Home Assistant."""
    hosts = dict(view.hosts)
    for host in config.hosts:
        current = hosts.get(host.name)
        if current is None or not current["connected"]:
            # Only a BMC knows the truth about a host that does not answer.
            hosts[host.name] = {
                **(current or _down_host(host.name, "unknown")),
                "power": infra.power(host).state(),
            }

    vms = dict(view.vms)
    # A VM on a stopped host is off. Keep it so its entity and the scenarios
    # that use it stay valid while the cluster sleeps.
    for vm_id, vm in store.get("vms").items():
        if vm_id not in vms:
            vms[vm_id] = {
                **vm,
                "power": "off",
                "cpu_mhz": 0,
                "mem_mb": 0,
                "tools": False,
                "ip": None,
                "uptime_s": 0,
            }

    protected = set(config.protected_vms) | {config.vcenter_vm_name}
    for vm in vms.values():
        vm["system"] = is_system_vm(vm)
        vm["protected"] = vm["name"] in protected or str(vm.get("vmid")) in protected

    scenarios = {}
    for scenario_id, scenario in store.get("active_scenarios").items():
        scenarios[scenario_id] = {
            "name": scenario["name"],
            "vms_total": len(scenario["vms"]),
            "vms_on": sum(
                1 for vm_id in scenario["vms"] if vms.get(vm_id, {}).get("power") == "on"
            ),
        }

    return {
        "platform": config.platform,
        "source": view.source,
        "cluster": cluster_state(config, view),
        "vcenter": {
            "reachable": view.source in FULL_SOURCES,
            "version": view.vcenter_version,
        },
        "hosts": sorted(hosts.values(), key=lambda host: host["name"]),
        "vms": sorted(vms.values(), key=lambda vm: vm["name"]),
        "datastores": sorted(view.datastores, key=lambda ds: ds["name"]),
        "scenarios": scenarios,
    }
