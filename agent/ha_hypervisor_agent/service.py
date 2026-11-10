"""MQTT side of the agent: state publishing and command handling."""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import paho.mqtt.client as mqtt

from . import __version__
from .config import AgentConfig, MqttConfig
from .inventory import build_state, build_view, remember
from .operations import Busy, OperationFailed, Orchestrator
from .protocol import (
    CMD_CANCEL,
    CMD_CLUSTER_SHUTDOWN,
    CMD_CLUSTER_START,
    CMD_REFRESH,
    CMD_SCENARIO_START,
    CMD_SCENARIO_STOP,
    CMD_VM_POWER,
    KIND_INFO,
    KIND_OPERATION,
    KIND_RESULT,
    KIND_STATE,
    LEAF_COMMAND,
    LEAF_INFO,
    LEAF_OPERATION,
    LEAF_RESULT,
    LEAF_STATE,
    LEAF_STATUS,
    PLATFORM_PROXMOX,
    PROTOCOL_VERSION,
    SNAPSHOT_COMMANDS,
    STATUS_OFFLINE,
    STATUS_ONLINE,
    Envelope,
    ProtocolError,
    seal,
    topic,
    unseal,
)
from .errors import GuestNotFound
from .pve import PveInfra, build_pve_view
from .pve_operations import ProxmoxOrchestrator
from .store import Store
from .vim import Infra, VimError, VimUnavailable

_LOGGER = logging.getLogger(__name__)
_QOS = 1
_OPERATIONS = (
    CMD_CLUSTER_SHUTDOWN,
    CMD_CLUSTER_START,
    CMD_SCENARIO_START,
    CMD_SCENARIO_STOP,
    *SNAPSHOT_COMMANDS,
)


def create_mqtt_client(settings: MqttConfig, client_id: str) -> mqtt.Client:
    """Return a client with the transport, TLS and login of the settings."""
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=client_id,
        transport=settings.transport,
        # Never let the broker queue commands for an offline agent.
        clean_session=True,
    )
    if settings.transport == "websockets":
        client.ws_set_options(path=settings.path)
    if settings.tls:
        client.tls_set(ca_certs=settings.ca_file)
        client.tls_insecure_set(not settings.verify_tls)
    if settings.username:
        client.username_pw_set(settings.username, settings.password)
    return client


def test_mqtt(settings: MqttConfig, agent_id: str, timeout: float = 10) -> str | None:
    """Try to log in to the broker, return the problem or None.

    The client id differs from the agent's, otherwise the broker would
    disconnect an agent that is already running.
    """
    client = create_mqtt_client(settings, f"ha_hypervisor_agent_{agent_id}_test")
    done = threading.Event()
    problem: list[str] = []

    def connected(_client, _userdata, _flags, reason_code, _properties) -> None:  # noqa: ANN001
        if reason_code.is_failure:
            problem.append(str(reason_code))
        done.set()

    client.on_connect = connected
    try:
        client.connect(settings.host, settings.port, keepalive=15)
    except (OSError, ValueError) as err:
        return str(err) or type(err).__name__
    client.loop_start()
    try:
        if not done.wait(timeout):
            return "no answer from the broker"
    finally:
        client.disconnect()
        client.loop_stop()
    return problem[0] if problem else None


class ReplayGuard:
    """Remembers command ids until they expire."""

    def __init__(self) -> None:
        """Start empty."""
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def accept(self, message_id: str, expires: float) -> bool:
        """Return True the first time an id is seen."""
        now = time.time()
        with self._lock:
            self._seen = {k: v for k, v in self._seen.items() if v >= now}
            if message_id in self._seen:
                return False
            self._seen[message_id] = expires
        return True


class AgentService:
    """Connects the agent to the broker and runs until stopped."""

    def __init__(self, config: AgentConfig) -> None:
        """Prepare the MQTT client, nothing connects yet."""
        self._config = config
        self._store = Store(config.state_file)
        if config.platform == PLATFORM_PROXMOX:
            self._infra: Any = PveInfra(config)
            self._build_view: Any = build_pve_view
            orchestrator: type[Orchestrator] = ProxmoxOrchestrator
        else:
            self._infra = Infra(config)
            self._build_view = build_view
            orchestrator = Orchestrator
        self._orchestrator = orchestrator(
            config, self._infra, self._store, self._publish_operation
        )
        self._guard = ReplayGuard()
        self._stop = threading.Event()
        self._poll_now = threading.Event()
        self._workers = ThreadPoolExecutor(max_workers=4, thread_name_prefix="command")
        self._client = self._build_client()

    def _topic(self, leaf: str) -> str:
        return topic(self._config.agent_id, leaf)

    def _build_client(self) -> mqtt.Client:
        settings = self._config.mqtt
        client = create_mqtt_client(
            settings, f"ha_hypervisor_agent_{self._config.agent_id}"
        )
        client.will_set(
            self._topic(LEAF_STATUS), STATUS_OFFLINE, qos=_QOS, retain=True
        )
        client.reconnect_delay_set(min_delay=1, max_delay=60)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        return client

    def _publish(self, leaf: str, kind: str, body: dict, *, retain: bool) -> None:
        target = self._topic(leaf)
        payload = seal(self._config.shared_key, target, kind, body)
        self._client.publish(target, payload, qos=_QOS, retain=retain)

    def _on_connect(self, client, _userdata, _flags, reason_code, _properties) -> None:  # noqa: ANN001
        if reason_code.is_failure:
            _LOGGER.error("Broker refused the connection: %s", reason_code)
            return
        _LOGGER.info("Connected to the broker")
        client.subscribe(self._topic(LEAF_COMMAND), qos=_QOS)
        self._announce()
        self._poll_now.set()

    def _announce(self) -> None:
        """Publish everything Home Assistant needs to know about the agent."""
        self._client.publish(
            self._topic(LEAF_STATUS), STATUS_ONLINE, qos=_QOS, retain=True
        )
        self._publish(
            LEAF_INFO,
            KIND_INFO,
            {
                "agent_version": __version__,
                "protocol": PROTOCOL_VERSION,
                "agent_id": self._config.agent_id,
                "platform": self._config.platform,
            },
            retain=True,
        )
        operation = self._orchestrator.operation
        if operation is not None:
            self._publish_operation(operation)

    def _on_disconnect(self, _client, _userdata, _flags, reason_code, _props) -> None:  # noqa: ANN001
        _LOGGER.warning("Disconnected from the broker: %s", reason_code)

    def _on_message(self, _client, _userdata, message) -> None:  # noqa: ANN001
        try:
            envelope = unseal(
                self._config.shared_key,
                message.topic,
                message.payload,
                require_fresh=True,
            )
        except ProtocolError as err:
            # Not answered: without a valid signature the sender is unknown.
            hint = " (check the clocks)" if err.code in ("expired", "clock_skew") else ""
            _LOGGER.warning("Rejected command: %s%s", err, hint)
            return
        if message.retain:
            _LOGGER.warning("Rejected retained command %s", envelope.id)
            return
        if not self._guard.accept(envelope.id, envelope.exp or 0):
            _LOGGER.warning("Rejected replayed command %s", envelope.id)
            return
        # Commands talk to vSphere, the MQTT thread must keep running.
        self._workers.submit(self._handle, envelope)

    def _handle(self, envelope: Envelope) -> None:
        result: dict[str, Any] = {"command_id": envelope.id, "ok": True, "code": None}
        try:
            result.update(self._execute(envelope.kind, envelope.body))
        except Busy:
            result.update(ok=False, code="busy")
        except OperationFailed as err:
            result.update(ok=False, code=err.code, detail=err.detail)
        except Exception:
            _LOGGER.exception("Command %s crashed", envelope.kind)
            result.update(ok=False, code="internal_error")
        self._publish(LEAF_RESULT, KIND_RESULT, result, retain=False)
        self._poll_now.set()

    def _execute(self, kind: str, body: dict[str, Any]) -> dict[str, Any]:
        if kind in _OPERATIONS:
            return {"operation_id": self._orchestrator.start(kind, body)}
        if kind == CMD_REFRESH:
            self._announce()
            return {}
        if kind == CMD_CANCEL:
            if not self._orchestrator.cancel():
                raise OperationFailed("no_operation")
            return {}
        if kind == CMD_VM_POWER:
            vm_id, action = body.get("vm_id"), body.get("action")
            if not isinstance(vm_id, str) or not isinstance(action, str):
                raise OperationFailed("invalid_request", "vm_id and action required")
            self._orchestrator.vm_power(vm_id, action)
            return {}
        raise OperationFailed("unknown_command", kind)

    def _publish_operation(self, operation: dict[str, Any]) -> None:
        self._publish(LEAF_OPERATION, KIND_OPERATION, operation, retain=True)
        self._poll_now.set()

    def _poll(self) -> None:
        try:
            view = self._build_view(self._config, self._infra)
            remember(view, self._config, self._store)
            state = build_state(self._config, self._infra, self._store, view)
        except (VimError, VimUnavailable, GuestNotFound) as err:
            _LOGGER.warning("Poll failed: %s", err)
            return
        except Exception:
            _LOGGER.exception("Poll crashed")
            return
        self._publish(LEAF_STATE, KIND_STATE, state, retain=True)

    def run(self) -> None:
        """Connect and poll until stop() is called."""
        settings = self._config.mqtt
        if not self._config.configured:
            _LOGGER.warning("The hypervisor API or the hosts are not configured yet, "
                            "only Home Assistant is connected")
        _LOGGER.info("Connecting to %s:%s", settings.host, settings.port)
        self._client.connect_async(settings.host, settings.port, keepalive=30)
        self._client.loop_start()
        try:
            while not self._stop.is_set():
                self._poll_now.wait(self._config.poll_interval)
                if self._stop.is_set():
                    break
                self._poll_now.clear()
                if self._client.is_connected():
                    self._poll()
        finally:
            self._shutdown()

    def stop(self) -> None:
        """Ask run() to finish."""
        self._stop.set()
        self._poll_now.set()

    def _shutdown(self) -> None:
        info = self._client.publish(
            self._topic(LEAF_STATUS), STATUS_OFFLINE, qos=_QOS, retain=True
        )
        if self._client.is_connected():
            info.wait_for_publish(timeout=3)
        self._client.disconnect()
        self._client.loop_stop()
        self._workers.shutdown(wait=False, cancel_futures=True)
        self._infra.close()


def _check_power(infra: Any, host: Any, *, proxmox: bool = False) -> int:
    return check_power(infra.power(host), host, proxmox=proxmox)


def check_power(power: Any, host: Any, *, proxmox: bool = False) -> int:
    """Print the power line of a host, return 1 when it cannot be started again."""
    from .pve import local_node_name  # noqa: PLC0415

    state = power.state()
    methods = ", ".join(method.name for method in power.methods) or "none"
    if proxmox and host.name == local_node_name():
        print(f"OK    Power {host.name}: this node runs the agent, power it on by hand")  # noqa: T201
        return 0
    if power.verified():
        print(f"OK    Power {host.name}: {state} (methods: {methods})")  # noqa: T201
        return 0
    if power.can_wake():
        print(f"WARN  Power {host.name}: only Wake on LAN is left, it cannot be checked")  # noqa: T201
        return 0
    if host.manual_power_on:
        print(f"OK    Power {host.name}: powered on by hand (manual power on)")  # noqa: T201
        return 0
    print(f"FAIL  Power {host.name}: no working method ({methods})")  # noqa: T201
    return 1


def _check_proxmox(config: AgentConfig) -> int:
    from .pve import PveInfra, cluster_quorate  # noqa: PLC0415

    infra = PveInfra(config)
    failures = 0
    if config.proxmox is None:
        failures += 1
        print("MISSING Proxmox: run proxmox set")  # noqa: T201
    for host in config.hosts:
        try:
            client = infra.node(host)
            version = client.version()
            status = client.cluster_status()
            ha = client.ha_resources()
        except (VimUnavailable, VimError) as err:
            failures += 1
            print(f"FAIL  Proxmox {host.name} ({host.address}): {err}")  # noqa: T201
        else:
            cluster = next((i for i in status if i.get("type") == "cluster"), None)
            kind = f"cluster {cluster.get('name')}" if cluster else "single node"
            quorum = "" if cluster is None else (
                ", quorate" if cluster_quorate(status) else ", NO QUORUM"
            )
            print(  # noqa: T201
                f"OK    Proxmox {host.name}: {version}, {kind}{quorum}, "
                f"{len(ha)} HA resources"
            )
        failures += _check_power(infra, host, proxmox=True)
    if not config.hosts:
        failures += 1
        print("MISSING Proxmox nodes: run node add")  # noqa: T201
    return failures


def check(config: AgentConfig) -> int:
    """Read-only test of every connection, printing one line for each."""
    failures = 0
    problem = test_mqtt(config.mqtt, config.agent_id)
    if problem:
        failures += 1
        print(f"FAIL  MQTT {config.mqtt.host}: {problem}")  # noqa: T201
    else:
        print(f"OK    MQTT {config.mqtt.host}")  # noqa: T201

    if config.platform == PLATFORM_PROXMOX:
        failures += _check_proxmox(config)
        return 1 if failures else 0

    infra = Infra(config)
    if config.vcenter is None:
        failures += 1
        print("MISSING vCenter: run vcenter set")  # noqa: T201
    else:
        try:
            client = infra.vcenter()
            print(f"OK    vCenter {config.vcenter.host}: {client.version}")  # noqa: T201
        except VimUnavailable as err:
            failures += 1
            print(f"FAIL  vCenter {config.vcenter.host}: {err}")  # noqa: T201

    for host in config.hosts:
        try:
            infra.esxi(host)
            print(f"OK    ESXi {host.name}")  # noqa: T201
        except VimUnavailable as err:
            failures += 1
            print(f"FAIL  ESXi {host.name}: {err}")  # noqa: T201
        failures += _check_power(infra, host)
    if not config.hosts:
        failures += 1
        print("MISSING ESXi hosts: run esxi add")  # noqa: T201
    infra.close()
    return 1 if failures else 0
