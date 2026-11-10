"""Long-running sequences: cluster shutdown and start, scenarios."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .config import AgentConfig, HostConfig
from .inventory import View, build_view, is_system_vm, remember
from .power import POWER_OFF, PowerError
from .protocol import (
    CMD_CLUSTER_SHUTDOWN,
    CMD_CLUSTER_START,
    CMD_SCENARIO_START,
    CMD_SCENARIO_STOP,
    OP_CANCELLED,
    OP_COMPLETED,
    OP_FAILED,
    OP_RUNNING,
    VM_ACTION_OFF,
    VM_ACTION_ON,
    VM_ACTION_SHUTDOWN,
    WAIT_MODES,
    WAIT_NONE,
    WAIT_POWER,
)
from .store import Store
from .vim import (
    Infra,
    VimClient,
    VimError,
    VimNotFound,
    VimToolsUnavailable,
    VimUnavailable,
)

_LOGGER = logging.getLogger(__name__)
_PARALLEL = 8
_MAX_STEP_TIMEOUT = 7200
_MAX_STEP_DELAY = 3600


class OperationFailed(Exception):
    """A step failed, with a language-neutral code."""

    def __init__(self, code: str, detail: str = "") -> None:
        """Keep the code for Home Assistant."""
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


class OperationCancelled(Exception):
    """The user cancelled the operation."""


class Busy(Exception):
    """Another operation is running."""


def validate_scenario(scenario: Any) -> dict[str, Any]:
    """Check a scenario received from Home Assistant."""
    try:
        name = scenario["name"]
        scenario_id = scenario["id"]
        steps = scenario["steps"]
        if not (isinstance(name, str) and isinstance(scenario_id, str) and name):
            raise TypeError("id and name must be text")  # noqa: TRY301
        if not (isinstance(steps, list) and steps):
            raise TypeError("steps must be a non-empty list")  # noqa: TRY301
        clean = []
        for step in steps:
            vms = step["vms"]
            wait = step.get("wait", WAIT_POWER)
            delay = float(step.get("delay", 0))
            timeout = float(step.get("timeout", 600))
            if not (
                isinstance(vms, list)
                and vms
                and all(isinstance(vm, str) for vm in vms)
                and wait in WAIT_MODES
                and 0 <= delay <= _MAX_STEP_DELAY
                and 0 < timeout <= _MAX_STEP_TIMEOUT
            ):
                raise TypeError("invalid step")  # noqa: TRY301
            clean.append({"vms": vms, "wait": wait, "delay": delay, "timeout": timeout})
    except (KeyError, TypeError, ValueError) as err:
        raise OperationFailed("invalid_request", str(err)) from err
    return {
        "id": scenario_id,
        "name": name,
        "steps": clean,
        "power_off_empty_hosts": bool(scenario.get("power_off_empty_hosts", False)),
    }


class Orchestrator:
    """Runs one operation at a time."""

    def __init__(
        self,
        config: AgentConfig,
        infra: Infra,
        store: Store,
        on_update: Callable[[dict[str, Any]], None],
    ) -> None:
        """Start idle and close what a previous process left running."""
        self._config = config
        self._infra = infra
        self._store = store
        self._on_update = on_update
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._op: dict[str, Any] | None = store.get("operation")
        if self._op is not None and self._op["status"] == OP_RUNNING:
            self._op.update(
                status=OP_FAILED, error_code="interrupted", finished=int(time.time())
            )
            store.set("operation", self._op)

    @property
    def operation(self) -> dict[str, Any] | None:
        """Return the current or last operation."""
        return self._store.get("operation")

    @property
    def busy(self) -> bool:
        """Return True while an operation runs."""
        return self._thread is not None and self._thread.is_alive()

    def start(self, kind: str, body: dict[str, Any]) -> str:
        """Start an operation in the background and return its id."""
        if not self._config.configured:
            raise OperationFailed("not_configured")
        handlers: dict[str, Callable[[dict[str, Any]], None]] = {
            CMD_CLUSTER_SHUTDOWN: self._cluster_shutdown,
            CMD_CLUSTER_START: self._cluster_start,
            CMD_SCENARIO_START: self._scenario_start,
            CMD_SCENARIO_STOP: self._scenario_stop,
        }
        if kind in (CMD_SCENARIO_START, CMD_SCENARIO_STOP):
            body = {**body, "scenario": validate_scenario(body.get("scenario"))}

        with self._lock:
            if self.busy:
                raise Busy
            self._cancel.clear()
            operation_id = uuid.uuid4().hex
            scenario = body.get("scenario")
            self._op = {
                "id": operation_id,
                "kind": kind,
                "status": OP_RUNNING,
                "step": "preflight",
                "detail": None,
                "error_code": None,
                "error": None,
                "scenario": scenario["id"] if scenario else None,
                "dry_run": bool(body.get("dry_run")),
                "plan": None,
                "started": int(time.time()),
                "finished": None,
            }
            self._publish()
            self._thread = threading.Thread(
                target=self._run,
                args=(handlers[kind], body),
                name=f"operation-{kind}",
                daemon=True,
            )
            self._thread.start()
        return operation_id

    def cancel(self) -> bool:
        """Ask the running operation to stop at the next checkpoint."""
        if not self.busy:
            return False
        self._cancel.set()
        return True

    def wait(self, timeout: float | None = None) -> None:
        """Wait for the running operation, used by tests and at exit."""
        if self._thread is not None:
            self._thread.join(timeout)

    def _publish(self) -> None:
        assert self._op is not None  # noqa: S101
        self._store.set("operation", self._op)
        self._on_update(dict(self._op))

    def _step(self, step: str, detail: str | None = None) -> None:
        self._checkpoint()
        assert self._op is not None  # noqa: S101
        _LOGGER.info("Step %s %s", step, detail or "")
        self._op.update(step=step, detail=detail)
        self._publish()

    def _checkpoint(self) -> None:
        if self._cancel.is_set():
            raise OperationCancelled

    def _run(self, handler: Callable[[dict[str, Any]], None], body: dict) -> None:
        assert self._op is not None  # noqa: S101
        status = OP_COMPLETED
        try:
            handler(body)
        except OperationCancelled:
            status = OP_CANCELLED
        except OperationFailed as err:
            _LOGGER.error("Operation failed: %s", err)  # noqa: TRY400
            status = OP_FAILED
            self._op.update(error_code=err.code, error=err.detail or None)
        except (VimError, VimUnavailable, VimNotFound) as err:
            _LOGGER.error("Operation failed: %s", err)  # noqa: TRY400
            status = OP_FAILED
            self._op.update(error_code="vsphere_error", error=str(err))
        except Exception as err:
            _LOGGER.exception("Operation crashed")
            status = OP_FAILED
            self._op.update(error_code="internal_error", error=str(err))
        self._op.update(status=status, finished=int(time.time()))
        self._publish()

    def _wait(self, check: Callable[[], bool], timeout: float, code: str, detail: str):
        deadline = time.monotonic() + timeout
        while True:
            self._checkpoint()
            if check():
                return
            if time.monotonic() >= deadline:
                raise OperationFailed(code, detail)
            if self._cancel.wait(self._config.timeouts.poll_step):
                raise OperationCancelled

    def _sleep(self, seconds: float) -> None:
        if seconds > 0 and self._cancel.wait(seconds):
            raise OperationCancelled

    def _host(self, name: str | None) -> HostConfig:
        host = self._config.host(name) if name else None
        if host is None:
            raise OperationFailed("host_not_configured", str(name))
        return host

    def _esxi(self, host: HostConfig) -> VimClient:
        try:
            return self._infra.esxi(host)
        except VimUnavailable as err:
            raise OperationFailed("esxi_unavailable", str(err)) from err

    def _view(self) -> View:
        view = build_view(self._config, self._infra)
        remember(view, self._config, self._store)
        return view

    def _vcenter_vm(self, view: View) -> dict[str, Any] | None:
        name = self._config.vcenter_vm_name
        return next((vm for vm in view.vms.values() if vm["name"] == name), None)

    # ---------------------------------------------------------------- shutdown

    def _shutdown_plan(self, view: View) -> dict[str, Any]:
        """Check everything that can fail before anything is stopped."""
        warnings: list[str] = []
        vcenter_vm = self._vcenter_vm(view)
        if view.source == "none":
            # Nothing answers. Either all is off already or the agent is cut off.
            raise OperationFailed("esxi_unavailable", "no host answers")
        if self._config.vcenter_vm_name and vcenter_vm is None:
            raise OperationFailed(
                "vcenter_vm_not_found", self._config.vcenter_vm_name
            )

        active_hosts = [h for h in view.hosts.values() if h["connected"]]
        if view.source == "vcenter":
            # A host vCenter lost contact with may still run workloads.
            active_hosts = list(view.hosts.values())

        hosts: list[str] = []
        for item in active_hosts:
            host = self._host(item["id"])
            self._esxi(host)
            power = self._infra.power(host)
            if not power.can_wake():
                raise OperationFailed("no_wake_method", host.name)
            if not power.verified():
                warnings.append(f"wake_unverified:{host.name}")
            hosts.append(host.name)

        running = [
            vm
            for vm in view.vms.values()
            if vm["power"] == "on" and vm is not vcenter_vm and not is_system_vm(vm)
        ]
        blocked = [vm["name"] for vm in running if self._is_protected(vm)]
        if blocked:
            raise OperationFailed("protected_vm_running", ", ".join(sorted(blocked)))

        vcenter_host = None
        if vcenter_vm is not None and vcenter_vm["power"] == "on":
            vcenter_host = self._host(vcenter_vm["host"]).name

        # The host running vCenter goes last, it is the one to bring back first.
        hosts.sort(key=lambda name: name == vcenter_host)
        retreat = []
        if any(is_system_vm(vm) for vm in view.vms.values()) and view.vcenter:
            retreat = [cluster["id"] for cluster in view.clusters]
        return {
            "vms": sorted(vm["name"] for vm in running),
            "retreat_clusters": retreat,
            "vcenter": {"vm": vcenter_vm["name"], "host": vcenter_host}
            if vcenter_host
            else None,
            "hosts": hosts,
            "warnings": warnings,
        }

    def _is_protected(self, vm: dict[str, Any]) -> bool:
        return vm["name"] in self._config.protected_vms

    def _cluster_shutdown(self, body: dict[str, Any]) -> None:
        assert self._op is not None  # noqa: S101
        self._step("preflight")
        view = self._view()
        plan = self._shutdown_plan(view)
        self._op["plan"] = plan
        self._publish()
        if body.get("dry_run"):
            return

        self._step("stop_vms", f"{len(plan['vms'])} VMs")
        running = [
            vm
            for vm in view.vms.values()
            if vm["name"] in plan["vms"] and vm["power"] == "on"
        ]
        self._stop_vms(view, running)

        if plan["retreat_clusters"]:
            self._step("retreat_on")
            self._enter_retreat(plan["retreat_clusters"])

        self._store.set("active_scenarios", {})

        if plan["vcenter"]:
            self._step("stop_vcenter", plan["vcenter"]["vm"])
            self._stop_vcenter(view, self._host(plan["vcenter"]["host"]))

        self._step("stop_hosts", ", ".join(plan["hosts"]))
        for name in plan["hosts"]:
            self._stop_host(self._host(name))

    def _stop_vms(self, view: View, vms: list[dict[str, Any]]) -> None:
        if not vms:
            return
        with ThreadPoolExecutor(max_workers=_PARALLEL) as pool:
            futures = [pool.submit(self._stop_vm, view, vm) for vm in vms]
            for future in futures:
                future.result()

    def _stop_vm(self, view: View, vm: dict[str, Any]) -> None:
        client = view.clients[vm["id"]]
        timeout = self._config.timeouts.guest_stop
        force = self._config.force_after_timeout
        try:
            client.shutdown_guest(vm["id"])
        except (VimToolsUnavailable, VimError) as err:
            if not force:
                raise OperationFailed("tools_unavailable", vm["name"]) from err
            _LOGGER.warning("%s has no guest shutdown, powering off", vm["name"])
            client.power_off(vm["id"])
            return
        try:
            self._wait(
                lambda: client.vm_status(vm["id"])["power"] == "off",
                timeout,
                "vm_stop_timeout",
                vm["name"],
            )
        except OperationFailed:
            if not force:
                raise
            _LOGGER.warning("%s ignored the shutdown, powering off", vm["name"])
            client.power_off(vm["id"])

    def _enter_retreat(self, clusters: list[str]) -> None:
        vcenter = self._infra.vcenter()
        # Saved first: a crash must not leave Retreat Mode forgotten.
        self._store.set("retreat_clusters", clusters)
        for cluster in clusters:
            vcenter.set_vcls_enabled(cluster, False)

        def gone() -> bool:
            data = vcenter.collect()
            return not any(
                vm["power"] == "on" and is_system_vm(vm) for vm in data["vms"]
            )

        try:
            self._wait(gone, self._config.timeouts.vcls_retreat, "timeout", "vCLS")
        except OperationFailed:
            # They stop with their host anyway.
            _LOGGER.warning("vCLS VMs are still running, continuing")

    def _stop_vcenter(self, view: View, host: HostConfig) -> None:
        vcenter_vm = self._vcenter_vm(view)
        assert vcenter_vm is not None  # noqa: S101
        vm_id = vcenter_vm["id"]
        client = self._esxi(host)
        self._store.set("vcenter_host", host.name)
        try:
            client.shutdown_guest(vm_id)
        except VimToolsUnavailable as err:
            raise OperationFailed("tools_unavailable", vcenter_vm["name"]) from err
        # Never forced: cutting the power of the vCenter database is how a lab
        # stops being able to start at all.
        self._wait(
            lambda: client.vm_status(vm_id)["power"] == "off",
            self._config.timeouts.vcenter_stop,
            "vcenter_stop_timeout",
            vcenter_vm["name"],
        )

    def _stop_host(self, host: HostConfig) -> None:
        self._esxi(host).shutdown_host()
        power = self._infra.power(host)
        self._wait(
            lambda: power.state() == POWER_OFF or not self._infra.reachable(host),
            self._config.timeouts.host_stop,
            "host_stop_timeout",
            host.name,
        )

    # ------------------------------------------------------------------- start

    def _cluster_start(self, _body: dict[str, Any]) -> None:
        self._ensure_infra([host.name for host in self._config.hosts])

    def _ensure_infra(self, wanted: list[str]) -> None:
        """Bring up the given hosts, vCenter and its cluster services."""
        timeouts = self._config.timeouts
        vcenter_host = self._store.get("vcenter_host")
        names = list(wanted)
        if self._config.vcenter_vm_name and vcenter_host and vcenter_host not in names:
            names.append(vcenter_host)
        if self._config.vcenter_vm_name and not vcenter_host:
            # Never seen where vCenter runs, it can be on any of them.
            names = [host.name for host in self._config.hosts]
        hosts = [self._host(name) for name in names]

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
                lambda host=host: self._esxi_ready(host),
                timeouts.host_start,
                "host_start_timeout",
                host.name,
            )

        if self._config.vcenter_vm_name:
            self._start_vcenter(hosts)

        self._step("wait_vcenter", self._config.vcenter.host)
        self._wait(
            self._vcenter_ready,
            timeouts.vcenter_start,
            "vcenter_start_timeout",
            self._config.vcenter.host,
        )
        self._wait(
            lambda: self._hosts_connected(names),
            timeouts.host_sync,
            "host_start_timeout",
            "hosts did not reconnect to vCenter",
        )

        clusters = self._store.get("retreat_clusters")
        if clusters:
            self._step("retreat_off")
            vcenter = self._infra.vcenter()
            for cluster in clusters:
                vcenter.set_vcls_enabled(cluster, True)
            self._store.set("retreat_clusters", [])

    def _esxi_ready(self, host: HostConfig) -> bool:
        try:
            self._infra.esxi(host).collect(host.name)
        except (VimUnavailable, VimError):
            return False
        return True

    def _vcenter_ready(self) -> bool:
        try:
            self._infra.vcenter().collect()
        except (VimUnavailable, VimError):
            return False
        return True

    def _hosts_connected(self, names: list[str]) -> bool:
        hosts = {h["id"]: h for h in self._infra.vcenter().collect()["hosts"]}
        return all(hosts.get(name, {}).get("connected") for name in names)

    def _start_vcenter(self, hosts: list[HostConfig]) -> None:
        name = self._config.vcenter_vm_name
        self._step("start_vcenter", name)
        if self._vcenter_ready():
            return
        for host in hosts:
            client = self._esxi(host)
            vm = next(
                (v for v in client.collect(host.name)["vms"] if v["name"] == name),
                None,
            )
            if vm is None:
                continue
            self._store.set("vcenter_host", host.name)
            if vm["power"] != "on":
                client.power_on(vm["id"])
            return
        raise OperationFailed("vcenter_vm_not_found", str(name))

    # --------------------------------------------------------------- scenarios

    def _scenario_start(self, body: dict[str, Any]) -> None:
        scenario = body["scenario"]
        wanted = [vm for step in scenario["steps"] for vm in step["vms"]]

        self._step("preflight", scenario["name"])
        known = self._store.get("vms")
        view = self._view()
        if view.source != "vcenter":
            placement = [known.get(vm, {}).get("host") for vm in wanted]
            if all(placement):
                hosts = sorted(set(placement))
            else:
                hosts = [host.name for host in self._config.hosts]
            self._ensure_infra(hosts)
            view = self._view()
        else:
            needed = {view.vms[vm]["host"] for vm in wanted if vm in view.vms}
            down = [h for h in needed if not view.hosts.get(h, {}).get("connected")]
            if down:
                self._ensure_infra(sorted(needed))
                view = self._view()

        missing = [vm for vm in wanted if vm not in view.vms]
        if missing:
            names = [known.get(vm, {}).get("name", vm) for vm in missing]
            raise OperationFailed("vm_not_found", ", ".join(names))

        active = self._store.get("active_scenarios")
        active[scenario["id"]] = {"name": scenario["name"], "vms": wanted}
        self._store.set("active_scenarios", active)

        total = len(scenario["steps"])
        for index, step in enumerate(scenario["steps"], start=1):
            names = [view.vms[vm]["name"] for vm in step["vms"]]
            self._step("start_vms", f"{index}/{total}: {', '.join(names)}")
            self._start_group(view, step)
            self._sleep(step["delay"])

    def _start_group(self, view: View, step: dict[str, Any]) -> None:
        for vm_id in step["vms"]:
            client = view.clients[vm_id]
            if client.vm_status(vm_id)["power"] != "on":
                client.power_on(vm_id)
        if step["wait"] == WAIT_NONE:
            return

        def ready(vm_id: str) -> bool:
            status = view.clients[vm_id].vm_status(vm_id)
            return status["power"] == "on" and (
                step["wait"] == WAIT_POWER or status["tools"]
            )

        deadline = time.monotonic() + step["timeout"]
        for vm_id in step["vms"]:
            self._wait(
                lambda vm_id=vm_id: ready(vm_id),
                max(0.0, deadline - time.monotonic()),
                "vm_start_timeout",
                view.vms[vm_id]["name"],
            )

    def _scenario_stop(self, body: dict[str, Any]) -> None:
        scenario = body["scenario"]
        self._step("preflight", scenario["name"])
        view = self._view()

        active = self._store.get("active_scenarios")
        active.pop(scenario["id"], None)
        shared = {vm for item in active.values() for vm in item["vms"]}

        hosts: set[str] = set()
        total = len(scenario["steps"])
        for index, step in enumerate(reversed(scenario["steps"]), start=1):
            group = [
                view.vms[vm]
                for vm in step["vms"]
                if vm in view.vms and vm not in shared and view.vms[vm]["power"] == "on"
            ]
            hosts.update(vm["host"] for vm in group)
            self._step("stop_vms", f"{index}/{total}: {len(group)} VMs")
            self._stop_vms(view, group)
        self._store.set("active_scenarios", active)

        if scenario["power_off_empty_hosts"]:
            self._stop_empty_hosts(hosts)

    def _stop_empty_hosts(self, hosts: set[str]) -> None:
        view = self._view()
        vcenter_vm = self._vcenter_vm(view)
        keep = {vcenter_vm["host"]} if vcenter_vm else set()
        keep.add(self._store.get("vcenter_host"))
        busy = {
            vm["host"]
            for vm in view.vms.values()
            if vm["power"] == "on" and not is_system_vm(vm)
        }
        empty = sorted(hosts - busy - keep)
        if not empty:
            return
        self._step("stop_empty_hosts", ", ".join(empty))
        for name in empty:
            self._stop_host(self._host(name))

    # ---------------------------------------------------------------- single VM

    def vm_power(self, vm_id: str, action: str) -> None:
        """Power one VM, raising OperationFailed with a code on refusal."""
        if action not in (VM_ACTION_ON, VM_ACTION_SHUTDOWN, VM_ACTION_OFF):
            raise OperationFailed("invalid_request", action)
        if self.busy:
            raise Busy
        view = build_view(self._config, self._infra)
        vm = view.vms.get(vm_id)
        if vm is None:
            raise OperationFailed("vm_unreachable", vm_id)
        protected = set(self._config.protected_vms) | {self._config.vcenter_vm_name}
        if action != VM_ACTION_ON and (vm["name"] in protected or is_system_vm(vm)):
            raise OperationFailed("vm_protected", vm["name"])

        client = view.clients[vm_id]
        try:
            if action == VM_ACTION_ON:
                if vm["power"] != "on":
                    client.power_on(vm_id)
            elif vm["power"] == "on":
                if action == VM_ACTION_SHUTDOWN:
                    client.shutdown_guest(vm_id)
                else:
                    client.power_off(vm_id)
        except VimToolsUnavailable as err:
            raise OperationFailed("tools_unavailable", vm["name"]) from err
        except VimNotFound as err:
            raise OperationFailed("vm_not_found", vm["name"]) from err
        except (VimError, VimUnavailable) as err:
            raise OperationFailed("vsphere_error", str(err)) from err
