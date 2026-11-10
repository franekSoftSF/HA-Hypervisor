"""Proxmox VE API access: one node, a cluster without HA, a cluster with HA.

Guests are QEMU VMs and LXC containers. Their id is "<type>-<vmid>", for
example qemu-100, unique in a Proxmox cluster. Every node answers for the
whole cluster, a call about another node is forwarded by Proxmox itself.
"""

from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import quote

import requests
import urllib3

from .config import AgentConfig, HostConfig
from .errors import (
    GuestNotFound,
    PlatformAuthError,
    PlatformError,
    PlatformUnavailable,
)
from .inventory import SOURCE_PROXMOX, View, is_system_vm
from .power import HostPower, tcp_open

_LOGGER = logging.getLogger(__name__)
_MB = 1024 * 1024
_GB = 1024 * 1024 * 1024
_TIMEOUT = 10
_SNAPSHOT_TIMEOUT = 3600
_TASK_POLL = 1.0
_AGENT_TIMEOUT = 4

GUEST_TYPES = ("qemu", "lxc")
HA_STARTED = "started"
HA_STOPPED = "stopped"


def guest_id(kind: str, vmid: int | str) -> str:
    """Return the id of a guest as published to Home Assistant."""
    return f"{kind}-{vmid}"


def split_guest_id(value: str) -> tuple[str, int]:
    """Return (type, vmid) of a guest id."""
    kind, _, vmid = value.partition("-")
    if kind not in GUEST_TYPES or not vmid.isdigit():
        raise GuestNotFound(value)
    return kind, int(vmid)


def ha_sid(kind: str, vmid: int | str) -> str:
    """Return the HA resource id of a guest."""
    return f"{'vm' if kind == 'qemu' else 'ct'}:{vmid}"


def _int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


class ProxmoxClient:
    """API of one node, authenticated with an API token."""

    SCHEME = "https"  # tests use plain HTTP

    def __init__(
        self,
        address: str,
        port: int,
        token_id: str,
        token_secret: str,
        verify_ssl: bool = False,
    ) -> None:
        """Prepare the session, nothing is sent yet."""
        self.address = address
        self._base = f"{self.SCHEME}://{address}:{port}/api2/json"
        self._session = requests.Session()
        self._session.verify = verify_ssl
        self._session.headers["Authorization"] = f"PVEAPIToken={token_id}={token_secret}"
        if not verify_ssl:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        # Guests are located once per poll, actions use this cache.
        self._nodes: dict[str, str] = {}

    def _request(
        self, method: str, path: str, wait: float = _TIMEOUT, **params: Any
    ) -> Any:
        url = self._base + path
        try:
            if method == "GET":
                response = self._session.get(url, params=params or None, timeout=wait)
            else:
                response = self._session.request(method, url, data=params, timeout=wait)
        except requests.RequestException as err:
            raise PlatformUnavailable(f"{self.address}: {err}") from err
        if response.status_code == 401:
            raise PlatformAuthError(f"{self.address}: the API token was refused")
        if response.status_code >= 400:
            message = response.reason or str(response.status_code)
            try:
                body = response.json()
            except ValueError:
                body = None
            if isinstance(body, dict) and body.get("errors"):
                message = f"{message}: {body['errors']}"
            if "does not exist" in message:
                raise GuestNotFound(message)
            raise PlatformError(f"{self.address} {method} {path}: {message}")
        try:
            return response.json().get("data")
        except ValueError as err:
            raise PlatformError(f"{self.address}: not a Proxmox API answer") from err

    # --------------------------------------------------------------- reading

    def version(self) -> str:
        """Return the Proxmox VE version, also a cheap check of the token."""
        data = self._request("GET", "/version") or {}
        return str(data.get("version") or data.get("release") or "")

    def cluster_status(self) -> list[dict[str, Any]]:
        """Return the cluster entry (only in a cluster) and one entry per node."""
        return self._request("GET", "/cluster/status") or []

    def resources(self, kind: str) -> list[dict[str, Any]]:
        """Return node, vm or storage resources of the whole cluster."""
        return self._request("GET", "/cluster/resources", type=kind) or []

    def node_status(self, node: str) -> dict[str, Any]:
        """Return CPU model and speed, memory and version of a node."""
        return self._request("GET", f"/nodes/{quote(node)}/status") or {}

    def ha_resources(self) -> list[dict[str, Any]]:
        """Return the HA resources, empty without HA."""
        try:
            return self._request("GET", "/cluster/ha/resources") or []
        except GuestNotFound:
            return []

    def set_ha_state(self, sid: str, state: str) -> None:
        """Ask the HA manager to start or stop a resource."""
        self._request("PUT", f"/cluster/ha/resources/{quote(sid)}", state=state)

    def guest_ips(self, node: str, kind: str, vmid: int) -> tuple[bool, str | None]:
        """Return (guest tools answer, first IPv4 address) of a running guest."""
        try:
            if kind == "qemu":
                data = self._request(
                    "GET",
                    f"/nodes/{quote(node)}/qemu/{vmid}/agent/network-get-interfaces",
                    wait=_AGENT_TIMEOUT,
                )
                interfaces = (data or {}).get("result") or []
                addresses = [
                    address.get("ip-address")
                    for interface in interfaces
                    if interface.get("name") != "lo"
                    for address in interface.get("ip-addresses") or []
                    if address.get("ip-address-type") == "ipv4"
                ]
            else:
                interfaces = self._request(
                    "GET", f"/nodes/{quote(node)}/lxc/{vmid}/interfaces", wait=_AGENT_TIMEOUT
                ) or []
                addresses = [
                    str(interface.get("inet", "")).split("/")[0]
                    for interface in interfaces
                    if interface.get("name") != "lo" and interface.get("inet")
                ]
        except (PlatformError, PlatformUnavailable, GuestNotFound):
            # No guest agent in the VM, or it is not running yet.
            return kind == "lxc", None
        return True, next((address for address in addresses if address), None)

    # ------------------------------------------------------------ the guests

    def _node_of(self, kind: str, vmid: int) -> str:
        key = guest_id(kind, vmid)
        if key not in self._nodes:
            for item in self.resources("vm"):
                if item.get("type") in GUEST_TYPES:
                    self._nodes[guest_id(item["type"], item["vmid"])] = item["node"]
        if key not in self._nodes:
            raise GuestNotFound(key)
        return self._nodes[key]

    def remember_nodes(self, placement: dict[str, str]) -> None:
        """Keep where each guest runs, found by the last poll."""
        self._nodes.update(placement)

    def _guest_path(self, vm_id: str) -> tuple[str, str, int]:
        kind, vmid = split_guest_id(vm_id)
        node = self._node_of(kind, vmid)
        return f"/nodes/{quote(node)}/{kind}/{vmid}", kind, vmid

    def _guest_request(self, vm_id: str, method: str, suffix: str, **params: Any) -> Any:
        path, _, _ = self._guest_path(vm_id)
        try:
            return self._request(method, f"{path}/{suffix}", **params)
        except GuestNotFound:
            # Migrated by HA or by hand since the last poll: locate it again.
            self._nodes.pop(vm_id, None)
            path, _, _ = self._guest_path(vm_id)
            return self._request(method, f"{path}/{suffix}", **params)

    def vm_status(self, vm_id: str) -> dict[str, Any]:
        """Return the live power state and whether the guest tools answer."""
        data = self._guest_request(vm_id, "GET", "status/current") or {}
        path, kind, _ = self._guest_path(vm_id)
        power = "on" if data.get("status") == "running" else "off"
        tools = False
        if power == "on":
            if kind == "lxc":
                tools = True
            else:
                try:
                    self._request("POST", f"{path}/agent/ping", wait=_AGENT_TIMEOUT)
                    tools = True
                except (PlatformError, PlatformUnavailable, GuestNotFound):
                    tools = False
        return {"power": power, "tools": tools}

    def power_on(self, vm_id: str) -> None:
        """Start a guest. With HA this asks the HA manager to start it."""
        self._guest_request(vm_id, "POST", "status/start")

    def power_off(self, vm_id: str) -> None:
        """Stop a guest at once, like pulling the plug."""
        self._guest_request(vm_id, "POST", "status/stop")

    def shutdown_guest(self, vm_id: str, timeout: float = 300) -> None:
        """Ask the guest to shut down, through the guest agent or ACPI.

        Returns before the guest is off. forceStop stays off: the agent
        decides itself whether to cut the power after its own timeout.
        """
        self._guest_request(
            vm_id, "POST", "status/shutdown", forceStop=0, timeout=int(timeout)
        )

    def shutdown_node(self, node: str) -> None:
        """Shut a node down. Call it on the node itself."""
        self._request("POST", f"/nodes/{quote(node)}/status", command="shutdown")

    # -------------------------------------------------------------- snapshots

    def list_snapshots(self, node: str, kind: str, vmid: int) -> list[str]:
        """Return the snapshot names of a guest, oldest first."""
        try:
            items = self._request("GET", f"/nodes/{quote(node)}/{kind}/{vmid}/snapshot") or []
        except (PlatformError, PlatformUnavailable, GuestNotFound) as err:
            _LOGGER.debug("Snapshots of %s-%s: %s", kind, vmid, err)
            return []
        # "current" is the running state, not a snapshot.
        found = [item for item in items if item.get("name") != "current"]
        found.sort(key=lambda item: _int(item.get("snaptime")))
        return [str(item["name"]) for item in found]

    def wait_task(self, upid: Any, timeout: float = _SNAPSHOT_TIMEOUT) -> None:
        """Wait for a Proxmox task, raising PlatformError when it fails."""
        if not isinstance(upid, str) or not upid.startswith("UPID:"):
            return  # nothing to wait for
        node = upid.split(":")[1]
        deadline = time.monotonic() + timeout
        while True:
            status = self._request("GET", f"/nodes/{quote(node)}/tasks/{quote(upid)}/status") or {}
            if status.get("status") == "stopped":
                if status.get("exitstatus") != "OK":
                    raise PlatformError(f"task failed: {status.get('exitstatus')}")
                return
            if time.monotonic() >= deadline:
                raise PlatformError("task timed out")
            time.sleep(_TASK_POLL)

    def snapshots(self, vm_id: str) -> list[str]:
        """Return the snapshot names of a guest, read now."""
        path, kind, vmid = self._guest_path(vm_id)
        node = path.split("/")[2]
        return self.list_snapshots(node, kind, vmid)

    def create_snapshot(
        self, vm_id: str, name: str, description: str = "", memory: bool = False
    ) -> None:
        """Take a snapshot and wait for it. Memory (vmstate) exists for QEMU only."""
        kind, _ = split_guest_id(vm_id)
        params: dict[str, Any] = {"snapname": name, "description": description}
        if memory and kind == "qemu":
            params["vmstate"] = 1
        self.wait_task(self._guest_request(vm_id, "POST", "snapshot", **params))

    def revert_snapshot(self, vm_id: str, name: str) -> None:
        """Go back to a snapshot. A running guest is stopped first, its state is lost anyway."""
        if self.vm_status(vm_id)["power"] == "on":
            self.wait_task(self._guest_request(vm_id, "POST", "status/stop"), _TIMEOUT * 12)
        self.wait_task(self._guest_request(vm_id, "POST", f"snapshot/{quote(name)}/rollback"))

    def delete_snapshot(self, vm_id: str, name: str) -> None:
        """Remove a snapshot."""
        self.wait_task(self._guest_request(vm_id, "DELETE", f"snapshot/{quote(name)}"))


class PveInfra:
    """Hands out node clients and the host power chains."""

    def __init__(self, config: AgentConfig) -> None:
        """Start without any connection."""
        self._config = config
        self._clients: dict[str, ProxmoxClient] = {}
        self._power: dict[str, HostPower] = {}

    def node(self, host: HostConfig) -> ProxmoxClient:
        """Return the client of one node, raising PlatformUnavailable."""
        settings = self._config.proxmox
        if settings is None:
            raise PlatformUnavailable("Proxmox is not configured")
        if not tcp_open(host.address, host.port):
            raise PlatformUnavailable(f"{host.address}:{host.port} is not reachable")
        if host.name not in self._clients:
            self._clients[host.name] = ProxmoxClient(
                host.address,
                host.port,
                settings.token_id,
                settings.token_secret,
                settings.verify_ssl,
            )
        return self._clients[host.name]

    def any_node(self) -> ProxmoxClient:
        """Return the first node that answers, it speaks for the cluster."""
        problems = []
        for host in self._config.hosts:
            try:
                client = self.node(host)
                client.version()
            except PlatformAuthError:
                raise
            except (PlatformUnavailable, PlatformError) as err:
                problems.append(str(err))
                continue
            return client
        raise PlatformUnavailable("; ".join(problems) or "no Proxmox node configured")

    def power(self, host: HostConfig) -> HostPower:
        """Return the power chain of a host."""
        if host.name not in self._power:
            self._power[host.name] = HostPower.for_host(host)
        return self._power[host.name]

    def reachable(self, host: HostConfig) -> bool:
        """Return True when the Proxmox API port answers."""
        return tcp_open(host.address, host.port)

    def close(self) -> None:
        """Nothing to log out of: tokens have no session."""
        self._clients.clear()


def cluster_quorate(status: list[dict[str, Any]]) -> bool:
    """Return True when a cluster has quorum. A single node always has."""
    cluster = next((item for item in status if item.get("type") == "cluster"), None)
    return cluster is None or bool(cluster.get("quorate"))


def build_pve_view(config: AgentConfig, infra: PveInfra) -> View:
    """Read the whole cluster through the first node that answers."""
    view = View()
    try:
        client = infra.any_node()
        version = client.version()
        status = client.cluster_status()
        nodes = client.resources("node")
        guests = client.resources("vm")
        storages = client.resources("storage")
        ha = {item.get("sid"): item.get("state") for item in client.ha_resources()}
    except (PlatformUnavailable, PlatformError, GuestNotFound) as err:
        _LOGGER.debug("Proxmox is not available: %s", err)
        return view

    view.source = SOURCE_PROXMOX
    view.vcenter = None
    view.vcenter_version = f"Proxmox VE {version}" if version else "Proxmox VE"
    cluster = next((item for item in status if item.get("type") == "cluster"), None)
    view.clusters = (
        [{"id": cluster.get("name"), "name": cluster.get("name"), "quorate": cluster_quorate(status)}]
        if cluster
        else []
    )
    view.ha = ha

    speed: dict[str, float] = {}
    for item in nodes:
        name = item.get("node")
        online = item.get("status") == "online"
        details: dict[str, Any] = {}
        if online:
            try:
                details = client.node_status(name)
            except (PlatformUnavailable, PlatformError, GuestNotFound) as err:
                _LOGGER.debug("Node %s status: %s", name, err)
        cpuinfo = details.get("cpuinfo") or {}
        cpus = _int(cpuinfo.get("cpus")) or _int(item.get("maxcpu"))
        speed[name] = _float(cpuinfo.get("mhz"))
        total_mhz = int(speed[name] * cpus)
        pve_version = str(details.get("pveversion") or "")
        view.hosts[name] = {
            "id": name,
            "name": name,
            "connected": online,
            "power": "on" if online else "unknown",
            "maintenance": False,
            "cpu_used_mhz": int(_float(item.get("cpu")) * total_mhz),
            "cpu_total_mhz": total_mhz,
            "mem_used_mb": _int(item.get("mem")) // _MB,
            "mem_total_mb": _int(item.get("maxmem")) // _MB,
            "uptime_s": _int(item.get("uptime")),
            "vendor": "Proxmox",
            "model": cpuinfo.get("model") or "Proxmox VE",
            # pve-manager/9.0.3/... -> 9.0.3
            "version": pve_version.split("/")[1] if pve_version.count("/") >= 1 else version,
        }

    placement: dict[str, str] = {}
    for item in guests:
        kind = item.get("type")
        if kind not in GUEST_TYPES or item.get("template"):
            continue
        vmid = item.get("vmid")
        node = item.get("node")
        vm_id = guest_id(kind, vmid)
        placement[vm_id] = node
        running = item.get("status") == "running"
        tools, address = (False, None)
        if running:
            tools, address = client.guest_ips(node, kind, vmid)
        snapshots: list[str] = []
        if view.hosts.get(node, {}).get("connected"):
            snapshots = client.list_snapshots(node, kind, vmid)
        cpus = _int(item.get("maxcpu"))
        view.vms[vm_id] = {
            "id": vm_id,
            "name": item.get("name") or vm_id,
            "vmid": vmid,
            "type": kind,
            "power": "on" if running else "off",
            "host": node,
            "cpu_mhz": int(_float(item.get("cpu")) * cpus * speed.get(node, 0)),
            "mem_mb": _int(item.get("mem")) // _MB,
            "mem_total_mb": _int(item.get("maxmem")) // _MB,
            "cpus": cpus,
            "storage_gb": round(_int(item.get("maxdisk")) / _GB, 2),
            "tools": tools,
            "ip": address,
            "guest_os": "LXC container" if kind == "lxc" else "QEMU virtual machine",
            "uptime_s": _int(item.get("uptime")),
            "ha": ha.get(ha_sid(kind, vmid)),
            "snapshots": snapshots,
        }
        view.clients[vm_id] = client  # type: ignore[assignment]
    client.remember_nodes(placement)

    seen_shared: set[str] = set()
    for item in storages:
        if item.get("status") not in (None, "available"):
            continue
        name = item.get("storage")
        if item.get("shared"):
            if name in seen_shared:
                continue
            seen_shared.add(name)
            store_id, label = name, name
        else:
            store_id = f"{item.get('node')}/{name}"
            label = f"{name} ({item.get('node')})" if len(nodes) > 1 else name
        capacity = _int(item.get("maxdisk"))
        used = _int(item.get("disk"))
        view.datastores.append(
            {
                "id": store_id,
                "name": label,
                "type": item.get("plugintype"),
                "capacity_gb": round(capacity / _GB, 2),
                "free_gb": round((capacity - used) / _GB, 2),
            }
        )
    return view


def started_ha_guests(view: View) -> list[str]:
    """Return the HA resources of running guests, to start again later."""
    return sorted(
        ha_sid(vm["type"], vm["vmid"])
        for vm in view.vms.values()
        if vm.get("ha") == HA_STARTED and vm["power"] == "on" and not is_system_vm(vm)
    )
