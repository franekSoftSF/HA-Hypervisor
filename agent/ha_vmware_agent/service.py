"""MQTT side of the agent: state publishing and command handling."""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import paho.mqtt.client as mqtt

from . import __version__
from .config import AgentConfig
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
    PROTOCOL_VERSION,
    STATUS_OFFLINE,
    STATUS_ONLINE,
    Envelope,
    ProtocolError,
    seal,
    topic,
    unseal,
)
from .store import Store
from .vim import Infra, VimError, VimUnavailable

_LOGGER = logging.getLogger(__name__)
_QOS = 1
_OPERATIONS = (
    CMD_CLUSTER_SHUTDOWN,
    CMD_CLUSTER_START,
    CMD_SCENARIO_START,
    CMD_SCENARIO_STOP,
)


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
        self._infra = Infra(config)
        self._store = Store(config.state_file)
        self._orchestrator = Orchestrator(
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
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"ha_vmware_agent_{self._config.agent_id}",
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
            view = build_view(self._config, self._infra)
            remember(view, self._config, self._store)
            state = build_state(self._config, self._infra, self._store, view)
        except (VimError, VimUnavailable) as err:
            _LOGGER.warning("Poll failed: %s", err)
            return
        except Exception:
            _LOGGER.exception("Poll crashed")
            return
        self._publish(LEAF_STATE, KIND_STATE, state, retain=True)

    def run(self) -> None:
        """Connect and poll until stop() is called."""
        settings = self._config.mqtt
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


def check(config: AgentConfig) -> int:
    """Read-only test of every connection, printing one line for each."""
    infra = Infra(config)
    failures = 0
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
        power = infra.power(host)
        state = power.state()
        methods = ", ".join(method.name for method in power.methods) or "none"
        if power.can_wake():
            print(f"OK    Power {host.name}: {state} (methods: {methods})")  # noqa: T201
        else:
            failures += 1
            print(f"FAIL  Power {host.name}: no working method ({methods})")  # noqa: T201
    infra.close()
    return 1 if failures else 0
