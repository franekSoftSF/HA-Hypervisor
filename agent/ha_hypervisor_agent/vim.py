"""vSphere API access, the same code for vCenter and for a single ESXi host."""

from __future__ import annotations

import http.client
import logging
import threading
import time
from typing import Any

from pyVim.connect import Disconnect, SmartConnect
from pyVmomi import vim, vmodl

from .config import AgentConfig, HostConfig
from .power import HostPower, tcp_open

_LOGGER = logging.getLogger(__name__)
_PC = vmodl.query.PropertyCollector
_MB = 1024 * 1024
_GB = 1024 * 1024 * 1024
_AUTH_BACKOFF = 120
_TASK_TIMEOUT = 120
_NETWORK_ERRORS = (OSError, http.client.HTTPException)

_VM_PATHS = [
    "name",
    "summary.runtime.powerState",
    "summary.runtime.host",
    "summary.quickStats.overallCpuUsage",
    "summary.quickStats.guestMemoryUsage",
    "summary.quickStats.uptimeSeconds",
    "summary.config.memorySizeMB",
    "summary.config.numCpu",
    "summary.config.instanceUuid",
    "summary.config.template",
    "summary.config.guestFullName",
    "summary.guest.toolsRunningStatus",
    "summary.guest.ipAddress",
    "summary.storage.committed",
]
_HOST_PATHS = [
    "name",
    "runtime.connectionState",
    "runtime.powerState",
    "runtime.inMaintenanceMode",
    "summary.quickStats.overallCpuUsage",
    "summary.quickStats.overallMemoryUsage",
    "summary.quickStats.uptime",
    "summary.hardware.cpuMhz",
    "summary.hardware.numCpuCores",
    "summary.hardware.memorySize",
    "summary.hardware.vendor",
    "summary.hardware.model",
    "summary.config.product.version",
]
_DATASTORE_PATHS = [
    "summary.name",
    "summary.capacity",
    "summary.freeSpace",
    "summary.type",
    "summary.url",
]


class VimUnavailable(Exception):
    """The endpoint cannot be reached or refused the login."""


class VimAuthError(VimUnavailable):
    """The endpoint refused the credentials."""


class VimNotFound(Exception):
    """The object does not exist on this endpoint."""


class VimToolsUnavailable(Exception):
    """A guest operation needs VMware Tools, which are not running."""


class VimError(Exception):
    """An API call failed."""


def _power(state: Any) -> str:
    return {"poweredOn": "on", "poweredOff": "off", "suspended": "suspended"}.get(
        str(state), "unknown"
    )


def _int(value: Any) -> int:
    return int(value) if value is not None else 0


class VimClient:
    """One authenticated session."""

    def __init__(self, service_instance: Any) -> None:
        """Wrap a connected service instance."""
        self._si = service_instance
        self._content = service_instance.RetrieveContent()

    @classmethod
    def connect(
        cls, host: str, port: int, user: str, password: str, verify_ssl: bool
    ) -> VimClient:
        """Log in, raising VimUnavailable when that is not possible."""
        try:
            return cls(
                SmartConnect(
                    host=host,
                    port=port,
                    user=user,
                    pwd=password,
                    disableSslCertValidation=not verify_ssl,
                    httpConnectionTimeout=20,
                )
            )
        except vim.fault.InvalidLogin as err:
            raise VimAuthError(f"{host}: invalid credentials") from err
        except (vmodl.MethodFault, OSError, Exception) as err:  # noqa: BLE001
            # While vCenter boots, its proxy answers with arbitrary HTTP
            # errors and pyVmomi lets them through as many exception types.
            raise VimUnavailable(f"{host}: {err}") from err

    @property
    def version(self) -> str:
        """Return the product version."""
        return str(self._content.about.fullName)

    def alive(self) -> bool:
        """Return True when the session is still valid."""
        try:
            return self._content.sessionManager.currentSession is not None
        except Exception:  # noqa: BLE001
            return False

    def disconnect(self) -> None:
        """Log out, ignoring errors on an already dead session."""
        try:
            Disconnect(self._si)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Logout failed", exc_info=True)

    def _fetch(self, vimtype: Any, paths: list[str]) -> list[tuple[Any, dict[str, Any]]]:
        manager = self._content.viewManager
        view = manager.CreateContainerView(self._content.rootFolder, [vimtype], True)
        try:
            traversal = _PC.TraversalSpec(
                name="view", path="view", skip=False, type=vim.view.ContainerView
            )
            spec = _PC.FilterSpec(
                objectSet=[_PC.ObjectSpec(obj=view, skip=True, selectSet=[traversal])],
                propSet=[_PC.PropertySpec(type=vimtype, pathSet=paths, all=False)],
            )
            collector = self._content.propertyCollector
            result = collector.RetrievePropertiesEx([spec], _PC.RetrieveOptions())
            rows: list[tuple[Any, dict[str, Any]]] = []
            while result is not None:
                rows.extend(
                    (item.obj, {prop.name: prop.val for prop in item.propSet})
                    for item in result.objects
                )
                if not result.token:
                    break
                result = collector.ContinueRetrievePropertiesEx(result.token)
            return rows
        finally:
            view.Destroy()

    def collect(self, host_name: str | None = None) -> dict[str, list[dict[str, Any]]]:
        """Return hosts, VMs, datastores and clusters.

        A standalone ESXi often calls itself localhost, so a direct session
        passes the name the host has in vCenter.
        """
        try:
            return self._collect(host_name)
        except vmodl.MethodFault as err:
            raise VimError(str(err.msg)) from err
        except _NETWORK_ERRORS as err:
            raise VimUnavailable(str(err)) from err

    def _collect(self, host_name: str | None) -> dict[str, list[dict[str, Any]]]:
        hosts = []
        host_names: dict[str, str] = {}
        for obj, data in self._fetch(vim.HostSystem, _HOST_PATHS):
            name = host_name or data["name"]
            host_names[obj._moId] = name  # noqa: SLF001
            cores = _int(data.get("summary.hardware.numCpuCores"))
            memory = _int(data.get("summary.hardware.memorySize"))
            hosts.append(
                {
                    "id": name,
                    "name": name,
                    "connected": str(data.get("runtime.connectionState"))
                    == "connected",
                    "power": _power(data.get("runtime.powerState")),
                    "maintenance": bool(data.get("runtime.inMaintenanceMode")),
                    "cpu_used_mhz": _int(data.get("summary.quickStats.overallCpuUsage")),
                    "cpu_total_mhz": _int(data.get("summary.hardware.cpuMhz")) * cores,
                    "mem_used_mb": _int(
                        data.get("summary.quickStats.overallMemoryUsage")
                    ),
                    "mem_total_mb": memory // _MB,
                    "uptime_s": _int(data.get("summary.quickStats.uptime")),
                    "vendor": data.get("summary.hardware.vendor"),
                    "model": data.get("summary.hardware.model"),
                    "version": data.get("summary.config.product.version"),
                }
            )

        vms = []
        for _, data in self._fetch(vim.VirtualMachine, _VM_PATHS):
            if data.get("summary.config.template"):
                continue
            host = data.get("summary.runtime.host")
            vms.append(
                {
                    "id": data["summary.config.instanceUuid"],
                    "name": data["name"],
                    "power": _power(data.get("summary.runtime.powerState")),
                    "host": host_names.get(host._moId) if host else None,  # noqa: SLF001
                    "cpu_mhz": _int(data.get("summary.quickStats.overallCpuUsage")),
                    "mem_mb": _int(data.get("summary.quickStats.guestMemoryUsage")),
                    "mem_total_mb": _int(data.get("summary.config.memorySizeMB")),
                    "cpus": _int(data.get("summary.config.numCpu")),
                    "storage_gb": round(
                        _int(data.get("summary.storage.committed")) / _GB, 2
                    ),
                    "tools": str(data.get("summary.guest.toolsRunningStatus"))
                    == "guestToolsRunning",
                    "ip": data.get("summary.guest.ipAddress"),
                    "guest_os": data.get("summary.config.guestFullName"),
                    "uptime_s": _int(data.get("summary.quickStats.uptimeSeconds")),
                }
            )

        datastores = []
        for _, data in self._fetch(vim.Datastore, _DATASTORE_PATHS):
            datastores.append(
                {
                    "id": data.get("summary.url") or data["summary.name"],
                    "name": data["summary.name"],
                    "type": data.get("summary.type"),
                    "capacity_gb": round(_int(data.get("summary.capacity")) / _GB, 2),
                    "free_gb": round(_int(data.get("summary.freeSpace")) / _GB, 2),
                }
            )

        clusters = [
            {"id": obj._moId, "name": data["name"]}  # noqa: SLF001
            for obj, data in self._fetch(vim.ClusterComputeResource, ["name"])
        ]
        return {
            "hosts": hosts,
            "vms": vms,
            "datastores": datastores,
            "clusters": clusters,
        }

    def _vm(self, vm_id: str) -> Any:
        vm = self._content.searchIndex.FindByUuid(None, vm_id, True, True)
        if vm is None:
            raise VimNotFound(vm_id)
        return vm

    def _wait_task(self, task: Any) -> None:
        deadline = time.monotonic() + _TASK_TIMEOUT
        while time.monotonic() < deadline:
            state = str(task.info.state)
            if state == "success":
                return
            if state == "error":
                error = task.info.error
                raise VimError(str(getattr(error, "msg", error)))
            time.sleep(0.5)
        raise VimError("task timed out")

    def vm_status(self, vm_id: str) -> dict[str, Any]:
        """Return the live power state and tools status of a VM."""
        # Only the two leaf properties: reading whole vm.runtime / vm.guest
        # objects is slower and depends on deserializing all of their fields.
        paths = ["summary.runtime.powerState", "summary.guest.toolsRunningStatus"]
        try:
            spec = _PC.FilterSpec(
                objectSet=[_PC.ObjectSpec(obj=self._vm(vm_id), skip=False)],
                propSet=[
                    _PC.PropertySpec(type=vim.VirtualMachine, pathSet=paths, all=False)
                ],
            )
            result = self._content.propertyCollector.RetrievePropertiesEx(
                [spec], _PC.RetrieveOptions()
            )
        except vmodl.MethodFault as err:
            raise VimError(str(err.msg)) from err
        except _NETWORK_ERRORS as err:
            raise VimUnavailable(str(err)) from err
        if result is None or not result.objects:
            raise VimNotFound(vm_id)
        values = {prop.name: prop.val for prop in result.objects[0].propSet}
        return {
            "power": _power(values.get(paths[0])),
            "tools": str(values.get(paths[1])) == "guestToolsRunning",
        }

    def _call(self, vm_id: str, action: str) -> None:
        vm = self._vm(vm_id)
        try:
            if action == "on":
                self._wait_task(vm.PowerOnVM_Task())
            elif action == "off":
                self._wait_task(vm.PowerOffVM_Task())
            else:
                vm.ShutdownGuest()
        except vim.fault.ToolsUnavailable as err:
            raise VimToolsUnavailable(vm_id) from err
        except vmodl.MethodFault as err:
            raise VimError(str(err.msg)) from err
        except _NETWORK_ERRORS as err:
            raise VimUnavailable(str(err)) from err

    def power_on(self, vm_id: str) -> None:
        """Power a VM on and wait for the task."""
        self._call(vm_id, "on")

    def power_off(self, vm_id: str) -> None:
        """Cut the power of a VM and wait for the task."""
        self._call(vm_id, "off")

    def shutdown_guest(self, vm_id: str) -> None:
        """Ask the guest to shut down. This returns before the guest is off."""
        self._call(vm_id, "shutdown")

    def shutdown_host(self) -> None:
        """Shut down the host of a direct ESXi session."""
        rows = self._fetch(vim.HostSystem, ["name"])
        if len(rows) != 1:
            raise VimError("shutdown_host needs a direct ESXi session")
        try:
            # force: the host does not have to be in maintenance mode.
            # The task is not awaited, its API goes away with the host.
            rows[0][0].ShutdownHost_Task(force=True)
        except vmodl.MethodFault as err:
            raise VimError(str(err.msg)) from err
        except _NETWORK_ERRORS as err:
            raise VimUnavailable(str(err)) from err

    def set_vcls_enabled(self, cluster_id: str, enabled: bool) -> None:
        """Leave or enter vCLS Retreat Mode through the vCenter advanced setting."""
        option = vim.option.OptionValue(
            key=f"config.vcls.clusters.{cluster_id}.enabled",
            value="True" if enabled else "False",
        )
        try:
            self._content.setting.UpdateOptions(changedValue=[option])
        except vmodl.MethodFault as err:
            raise VimError(str(err.msg)) from err


class Infra:
    """Hands out cached sessions and the host power chains."""

    def __init__(self, config: AgentConfig) -> None:
        """Start without any session."""
        self._config = config
        self._lock = threading.Lock()
        self._clients: dict[str, VimClient] = {}
        self._auth_blocked: dict[str, float] = {}
        self._power: dict[str, HostPower] = {}

    def _session(
        self, key: str, address: str, port: int, user: str, pwd: str, verify: bool
    ) -> VimClient:
        with self._lock:
            client = self._clients.get(key)
            if client is not None:
                if client.alive():
                    return client
                self._clients.pop(key)

            # Repeated wrong passwords would lock the account.
            if time.monotonic() < self._auth_blocked.get(key, 0):
                raise VimAuthError(f"{address}: login paused after a refused login")
            if not tcp_open(address, port):
                raise VimUnavailable(f"{address}:{port} is not reachable")
            try:
                client = VimClient.connect(address, port, user, pwd, verify)
            except VimAuthError:
                self._auth_blocked[key] = time.monotonic() + _AUTH_BACKOFF
                raise
            self._clients[key] = client
            return client

    def vcenter(self) -> VimClient:
        """Return a vCenter session or raise VimUnavailable."""
        config = self._config.vcenter
        return self._session(
            "vcenter",
            config.host,
            config.port,
            config.username,
            config.password,
            config.verify_ssl,
        )

    def esxi(self, host: HostConfig) -> VimClient:
        """Return a direct ESXi session or raise VimUnavailable."""
        return self._session(
            f"esxi:{host.name}",
            host.address,
            host.port,
            host.username,
            host.password,
            host.verify_ssl,
        )

    def power(self, host: HostConfig) -> HostPower:
        """Return the power chain of a host."""
        if host.name not in self._power:
            self._power[host.name] = HostPower.for_host(host)
        return self._power[host.name]

    def reachable(self, host: HostConfig) -> bool:
        """Return True when the ESXi API port answers."""
        return tcp_open(host.address, host.port)

    def close(self) -> None:
        """Log out of all sessions."""
        with self._lock:
            for client in self._clients.values():
                client.disconnect()
            self._clients.clear()
