"""Proxmox VE sequences. Scenarios and single guests reuse the vSphere code.

Shutdown: preflight, stop the guests (an HA guest is stopped through the HA
manager, so it is neither moved nor fenced), shut the nodes down.
Start: wake the nodes, wait for their API, wait for quorum, start again the
HA resources that ran before the shutdown.

The same code covers one node, a cluster without HA and a cluster with HA:
without a cluster there is no quorum to wait for, without HA there are no
HA resources.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from .config import HostConfig
from .errors import GuestNotFound, PlatformError, PlatformUnavailable
from .inventory import View, is_system_vm
from .operations import OperationFailed, Orchestrator
from .protocol import OP_COMPLETED
from .power import POWER_OFF, PowerError
from .pve import (
    HA_STARTED,
    ProxmoxClient,
    PveInfra,
    build_pve_view,
    cluster_quorate,
    local_node_name,
    started_ha_guests,
)

_LOGGER = logging.getLogger(__name__)


class ProxmoxOrchestrator(Orchestrator):
    """Runs one operation at a time on Proxmox VE."""

    ERROR_CODE = "proxmox_error"
    SELF_GRACE = 2.0
    _infra: PveInfra

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Use the Proxmox view of the environment."""
        super().__init__(*args, **kwargs)
        self._build_view = build_pve_view
        # Set when the agent runs on one of the nodes it drives.
        self._self_node = local_node_name()

    def _is_self(self, name: str) -> bool:
        return name == self._self_node

    def _node(self, host: HostConfig) -> ProxmoxClient:
        try:
            client = self._infra.node(host)
            client.version()
        except (PlatformUnavailable, PlatformError) as err:
            raise OperationFailed("proxmox_unavailable", f"{host.name}: {err}") from err
        return client

    # The vSphere code calls _esxi for "the API of this host".
    _esxi = _node

    # ---------------------------------------------------------------- shutdown

    def _shutdown_plan(self, view: View) -> dict[str, Any]:
        """Check everything that can fail before anything is stopped."""
        if view.source == "none":
            raise OperationFailed("proxmox_unavailable", "no node answers")
        if view.clusters and not view.clusters[0].get("quorate"):
            # Without quorum Proxmox refuses to stop guests: fail now, not halfway.
            raise OperationFailed("quorum_timeout", "the cluster has no quorum")

        warnings: list[str] = []
        hosts: list[str] = []
        for item in view.hosts.values():
            if not item["connected"]:
                continue
            host = self._host(item["id"])
            self._node(host)
            if self._is_self(host.name):
                # The agent stops with this node: it cannot power it on again.
                warnings.append(f"self_hosted:{host.name}")
                continue
            power = self._infra.power(host)
            if not power.can_wake():
                raise OperationFailed("no_wake_method", host.name)
            if not power.verified():
                warnings.append(f"wake_unverified:{host.name}")
            hosts.append(host.name)

        own = [name for name in view.hosts if self._is_self(name) and view.hosts[name]["connected"]]
        running = [
            vm for vm in view.vms.values() if vm["power"] == "on" and not is_system_vm(vm)
        ]
        blocked = [vm["name"] for vm in running if self._is_protected(vm)]
        if blocked:
            raise OperationFailed("protected_vm_running", ", ".join(sorted(blocked)))
        return {
            "vms": sorted(vm["name"] for vm in running),
            "ha_resources": started_ha_guests(view),
            "retreat_clusters": [],
            "vcenter": None,
            # The node running the agent goes last.
            "hosts": hosts + own,
            "warnings": warnings,
        }

    def _cluster_shutdown(self, body: dict[str, Any]) -> None:
        assert self._op is not None  # noqa: S101
        self._step("preflight")
        view = self._view()
        plan = self._shutdown_plan(view)
        self._op["plan"] = plan
        self._publish()
        if body.get("dry_run"):
            return

        if plan["ha_resources"]:
            # Saved first: after a crash they must still be started again.
            known = set(self._store.get("ha_resources")) | set(plan["ha_resources"])
            self._store.set("ha_resources", sorted(known))

        self._step("stop_vms", f"{len(plan['vms'])} VMs")
        running = [
            vm for vm in view.vms.values() if vm["name"] in plan["vms"] and vm["power"] == "on"
        ]
        self._stop_vms(view, running)
        self._store.set("active_scenarios", {})

        self._step("stop_hosts", ", ".join(plan["hosts"]))
        for name in plan["hosts"]:
            if self._is_self(name):
                self._stop_own_node(self._host(name))
            else:
                self._stop_host(self._host(name))

    def _stop_own_node(self, host: HostConfig) -> None:
        """Report the operation as done, then shut down the node this agent runs on."""
        assert self._op is not None  # noqa: S101
        self._op.update(
            status=OP_COMPLETED,
            finished=int(time.time()),
            detail=f"{host.name} runs this agent and is shutting down",
        )
        self._publish()
        # A moment for the retained message to reach the broker.
        time.sleep(self.SELF_GRACE)
        self._node(host).shutdown_node(host.name)

    def _stop_host(self, host: HostConfig) -> None:
        self._node(host).shutdown_node(host.name)
        power = self._infra.power(host)
        self._wait(
            lambda: power.state() == POWER_OFF or not self._infra.reachable(host),
            self._config.timeouts.host_stop,
            "host_stop_timeout",
            host.name,
        )

    def _stop_empty_hosts(self, hosts: set[str]) -> None:
        view = self._view()
        busy = {
            vm["host"] for vm in view.vms.values() if vm["power"] == "on" and not is_system_vm(vm)
        }
        # Never the node running the agent: the scenario would end with the agent gone.
        empty = sorted(
            name
            for name in hosts - busy
            if view.hosts.get(name, {}).get("connected") and not self._is_self(name)
        )
        if not empty:
            return
        if view.clusters:
            # Losing quorum makes the remaining nodes read-only, and with HA
            # their watchdog would reset them. Stop only what keeps a majority.
            total = len(view.hosts)
            online = sum(1 for host in view.hosts.values() if host["connected"])
            allowed = max(0, online - (total // 2 + 1))
            if allowed < len(empty):
                _LOGGER.warning(
                    "Keeping %s on to keep quorum", ", ".join(empty[allowed:])
                )
                empty = empty[:allowed]
            if not empty:
                return
        self._step("stop_empty_hosts", ", ".join(empty))
        for name in empty:
            self._stop_host(self._host(name))

    # ------------------------------------------------------------------- start

    def _ensure_infra(self, wanted: list[str]) -> None:
        """Bring up the given nodes, wait for quorum, restart HA resources."""
        timeouts = self._config.timeouts
        hosts = [self._host(name) for name in wanted]
        names = [host.name for host in hosts]

        self._step("wake_hosts", ", ".join(names))
        for host in hosts:
            if self._infra.reachable(host):
                continue
            try:
                method = self._infra.power(host).power_on()
            except PowerError as err:
                raise OperationFailed("wake_failed", f"{host.name}: {err}") from err
            _LOGGER.info("Woke %s with %s", host.name, method)

        self._step("wait_hosts", ", ".join(names))
        for host in hosts:
            self._wait(
                lambda host=host: self._node_ready(host),
                timeouts.host_start,
                "host_start_timeout",
                host.name,
            )

        self._step("wait_quorum")
        self._wait(
            lambda: self._quorate(names),
            timeouts.host_sync,
            "quorum_timeout",
            ", ".join(names),
        )

        resources = self._store.get("ha_resources")
        if resources:
            self._step("restore_ha", ", ".join(resources))
            client = self._infra.any_node()
            present = {item.get("sid"): item for item in client.ha_resources()}
            for sid in resources:
                if sid not in present:
                    continue  # removed from HA meanwhile
                if present[sid].get("state") != HA_STARTED:
                    client.set_ha_state(sid, HA_STARTED)
            self._store.set("ha_resources", [])

    def _node_ready(self, host: HostConfig) -> bool:
        try:
            self._infra.node(host).version()
        except (PlatformUnavailable, PlatformError):
            return False
        return True

    def _quorate(self, names: list[str]) -> bool:
        try:
            client = self._infra.any_node()
            status = client.cluster_status()
            nodes = {item.get("node"): item for item in client.resources("node")}
        except (PlatformUnavailable, PlatformError, GuestNotFound):
            return False
        online = all(nodes.get(name, {}).get("status") == "online" for name in names)
        return online and cluster_quorate(status)
