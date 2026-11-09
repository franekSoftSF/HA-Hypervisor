"""Wire protocol between the Home Assistant integration and the agent.

This file exists twice, in custom_components/ha_hypervisor and in agent/ha_hypervisor_agent,
because the Marketplace installs only the integration directory. Both copies must stay byte-identical
(tests/test_protocol.py checks it). It must not import anything outside the standard
library.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Any

PROTOCOL_VERSION = 1
TOPIC_ROOT = "ha_hypervisor"
MIN_KEY_LENGTH = 32

# A command is valid for this long. It covers MQTT delays only, never a queue:
# an old "shut the cluster down" must not fire after a reconnect.
COMMAND_TTL = 120
MAX_FUTURE_SKEW = 120

STATUS_ONLINE = "online"
STATUS_OFFLINE = "offline"

LEAF_STATUS = "status"  # retained, plain text, also the MQTT Last Will
LEAF_INFO = "info"  # retained, signed
LEAF_STATE = "state"  # retained, signed
LEAF_OPERATION = "operation"  # retained, signed
LEAF_COMMAND = "cmd"  # never retained, signed
LEAF_RESULT = "result"  # never retained, signed

KIND_INFO = "info"
KIND_STATE = "state"
KIND_OPERATION = "operation"
KIND_RESULT = "result"

CMD_REFRESH = "refresh"
CMD_CANCEL = "cancel"
CMD_VM_POWER = "vm_power"
CMD_CLUSTER_SHUTDOWN = "cluster_shutdown"
CMD_CLUSTER_START = "cluster_start"
CMD_SCENARIO_START = "scenario_start"
CMD_SCENARIO_STOP = "scenario_stop"

VM_ACTION_ON = "on"
VM_ACTION_SHUTDOWN = "shutdown"
VM_ACTION_OFF = "off"

WAIT_NONE = "none"
WAIT_POWER = "power"
WAIT_TOOLS = "tools"
WAIT_MODES = (WAIT_NONE, WAIT_POWER, WAIT_TOOLS)

OP_RUNNING = "running"
OP_COMPLETED = "completed"
OP_FAILED = "failed"
OP_CANCELLED = "cancelled"

CLUSTER_ON = "on"
CLUSTER_DEGRADED = "degraded"
CLUSTER_OFF = "off"

# Language-neutral step keys, translated in Home Assistant.
STEPS = (
    "preflight",
    "stop_vms",
    "retreat_on",
    "stop_vcenter",
    "stop_hosts",
    "wake_hosts",
    "wait_hosts",
    "start_vcenter",
    "wait_vcenter",
    "retreat_off",
    "start_vms",
    "stop_empty_hosts",
)

# Language-neutral error codes, translated in Home Assistant.
ERROR_CODES = (
    "busy",
    "invalid_request",
    "unknown_command",
    "no_operation",
    "interrupted",
    "internal_error",
    "vcenter_unavailable",
    "vcenter_vm_not_found",
    "vcenter_stop_timeout",
    "vcenter_start_timeout",
    "esxi_unavailable",
    "host_not_configured",
    "host_stop_timeout",
    "host_start_timeout",
    "no_wake_method",
    "wake_failed",
    "protected_vm_running",
    "vm_protected",
    "vm_not_found",
    "vm_unreachable",
    "vm_stop_timeout",
    "vm_start_timeout",
    "tools_unavailable",
    "vsphere_error",
)

_AGENT_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class ProtocolError(ValueError):
    """A message failed validation."""

    def __init__(self, code: str, message: str) -> None:
        """Keep a stable code next to the description."""
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class Envelope:
    """A verified message."""

    id: str
    ts: int
    exp: int | None
    kind: str
    body: dict[str, Any]


def is_valid_agent_id(agent_id: str) -> bool:
    """Return True when the id is safe to use as a topic level."""
    return bool(_AGENT_ID.match(agent_id))


def is_valid_key(key: str) -> bool:
    """Return True when the shared key is long enough."""
    return len(key) >= MIN_KEY_LENGTH


def generate_key() -> str:
    """Return a new random shared key."""
    return secrets.token_urlsafe(32)


def topic(agent_id: str, leaf: str) -> str:
    """Return the topic of one message type of an agent."""
    return f"{TOPIC_ROOT}/{agent_id}/{leaf}"


def _canonical(data: dict[str, Any]) -> bytes:
    return json.dumps(
        data, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _signature(key: str, target: str, envelope: dict[str, Any]) -> str:
    # The topic is signed too, so a message cannot be replayed to another
    # agent or passed off as a different message type.
    signed = target.encode("utf-8") + b"\n" + _canonical(envelope)
    return hmac.new(key.encode("utf-8"), signed, hashlib.sha256).hexdigest()


def seal(
    key: str,
    target: str,
    kind: str,
    body: dict[str, Any],
    *,
    ttl: int | None = None,
    now: float | None = None,
) -> str:
    """Return the signed JSON payload for a topic."""
    timestamp = int(time.time() if now is None else now)
    envelope: dict[str, Any] = {
        "v": PROTOCOL_VERSION,
        "id": uuid.uuid4().hex,
        "ts": timestamp,
        "exp": None if ttl is None else timestamp + ttl,
        "kind": kind,
        "body": body,
    }
    envelope["sig"] = _signature(key, target, envelope)
    return json.dumps(envelope, separators=(",", ":"), ensure_ascii=False)


def unseal(
    key: str,
    target: str,
    payload: str | bytes,
    *,
    require_fresh: bool = False,
    now: float | None = None,
) -> Envelope:
    """Verify a payload and return it, or raise ProtocolError."""
    try:
        data = json.loads(payload)
    except (TypeError, ValueError) as err:
        raise ProtocolError("malformed", "payload is not JSON") from err
    if not isinstance(data, dict):
        raise ProtocolError("malformed", "payload is not an object")
    signature = data.pop("sig", None)
    if not isinstance(signature, str) or not hmac.compare_digest(
        signature, _signature(key, target, data)
    ):
        raise ProtocolError("bad_signature", "signature does not match")
    if data.get("v") != PROTOCOL_VERSION:
        raise ProtocolError("version", f"unsupported protocol {data.get('v')!r}")

    try:
        envelope = Envelope(
            id=data["id"],
            ts=data["ts"],
            exp=data["exp"],
            kind=data["kind"],
            body=data["body"],
        )
    except KeyError as err:
        raise ProtocolError("malformed", f"missing field {err}") from err
    if not isinstance(envelope.body, dict):
        raise ProtocolError("malformed", "body is not an object")

    if require_fresh:
        current = time.time() if now is None else now
        if envelope.exp is None or current > envelope.exp:
            raise ProtocolError("expired", "message expired")
        if envelope.ts > current + MAX_FUTURE_SKEW:
            raise ProtocolError("clock_skew", "message comes from the future")
    return envelope
