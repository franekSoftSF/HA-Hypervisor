"""Connection to one agent over the Home Assistant MQTT integration."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import logging
from typing import Any

from homeassistant.components import mqtt
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr, issue_registry as ir
from homeassistant.helpers.event import async_call_later

from .const import AGENT_CHECK_SECONDS, ARM_SECONDS, COMMAND_TIMEOUT, DOMAIN, RELEASES_URL
from .protocol import (
    CMD_CANCEL,
    CMD_CLUSTER_SHUTDOWN,
    CMD_CLUSTER_START,
    CMD_REFRESH,
    CMD_SCENARIO_START,
    CMD_SCENARIO_STOP,
    CMD_VM_POWER,
    COMMAND_TTL,
    ERROR_CODES,
    LEAF_COMMAND,
    LEAF_INFO,
    LEAF_OPERATION,
    LEAF_RESULT,
    LEAF_STATE,
    LEAF_STATUS,
    OP_RUNNING,
    PLATFORM_PROXMOX,
    STATUS_ONLINE,
    Envelope,
    ProtocolError,
    seal,
    topic,
    unseal,
)

_LOGGER = logging.getLogger(__name__)
_QOS = 1


def parse_version(text: Any) -> tuple[int, ...] | None:
    """Return a version such as 0.3.2 as numbers, None when it is not one."""
    try:
        return tuple(int(part) for part in str(text).split("."))
    except ValueError:
        return None


def agent_error(code: str) -> HomeAssistantError:
    """Return a translated error for a code sent by the agent."""
    key = code if code in ERROR_CODES else "internal_error"
    return HomeAssistantError(translation_domain=DOMAIN, translation_key=f"agent_{key}")


class AgentLink:
    """Keeps the verified agent data and sends signed commands."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        agent_id: str,
        shared_key: str,
        integration_version: str | None = None,
    ) -> None:
        """Start without any data."""
        self.hass = hass
        self.integration_version = integration_version
        self._check: CALLBACK_TYPE | None = None
        self._entry_id = entry_id
        self.agent_id = agent_id
        self._key = shared_key
        self.online = False
        self.info: dict[str, Any] | None = None
        self.state: dict[str, Any] | None = None
        self.operation: dict[str, Any] | None = None
        self.armed = False
        self._timestamps: dict[str, int] = {}
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._listeners: list[CALLBACK_TYPE] = []
        self._unsubscribe: list[CALLBACK_TYPE] = []
        self._disarm: CALLBACK_TYPE | None = None
        self._rejected = False

    async def async_start(self) -> None:
        """Subscribe to the agent topics."""
        handlers: dict[str, Callable[[mqtt.ReceiveMessage], None]] = {
            LEAF_STATUS: self._on_status,
            LEAF_INFO: self._on_info,
            LEAF_STATE: self._on_state,
            LEAF_OPERATION: self._on_operation,
            LEAF_RESULT: self._on_result,
        }
        for leaf, handler in handlers.items():
            self._unsubscribe.append(
                await mqtt.async_subscribe(
                    self.hass, topic(self.agent_id, leaf), handler, qos=_QOS
                )
            )
        await self._async_request_announce()
        # After an update or restart of Home Assistant: does the agent answer?
        self._check = async_call_later(
            self.hass, AGENT_CHECK_SECONDS, self._async_check_reachable
        )

    @property
    def _issue_unreachable(self) -> str:
        return f"agent_unreachable_{self.agent_id}"

    @property
    def _issue_outdated(self) -> str:
        return f"agent_outdated_{self.agent_id}"

    @callback
    def _async_check_reachable(self, _now: Any = None) -> None:
        self._check = None
        if self.online:
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self._issue_unreachable,
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key="agent_unreachable",
            translation_placeholders={"agent_id": self.agent_id},
        )

    @callback
    def _async_check_version(self) -> None:
        """Raise a repair when the agent is older than the integration."""
        agent_version = (self.info or {}).get("agent_version")
        agent = parse_version(agent_version)
        ours = parse_version(self.integration_version)
        if agent is None or ours is None or agent >= ours:
            ir.async_delete_issue(self.hass, DOMAIN, self._issue_outdated)
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self._issue_outdated,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            learn_more_url=RELEASES_URL,
            translation_key="agent_outdated",
            translation_placeholders={
                "agent_id": self.agent_id,
                "agent_version": str(agent_version),
                "version": str(self.integration_version),
            },
        )

    @callback
    def async_remove_issues(self) -> None:
        """Forget the repairs of an agent that is removed."""
        ir.async_delete_issue(self.hass, DOMAIN, self._issue_unreachable)
        ir.async_delete_issue(self.hass, DOMAIN, self._issue_outdated)

    @callback
    def _async_seen_online(self) -> None:
        ir.async_delete_issue(self.hass, DOMAIN, self._issue_unreachable)

    async def _async_request_announce(self) -> None:
        """Ask the agent to repeat its status.

        After a quick reload the broker does not resend retained messages,
        because the MQTT integration keeps the old subscription for a moment.
        """
        target = topic(self.agent_id, LEAF_COMMAND)
        payload = seal(self._key, target, CMD_REFRESH, {}, ttl=COMMAND_TTL)
        try:
            await mqtt.async_publish(self.hass, target, payload, qos=_QOS, retain=False)
        except HomeAssistantError as err:
            _LOGGER.debug("Could not ask agent %s for status: %s", self.agent_id, err)

    @callback
    def async_update_agent_device(self) -> None:
        """Show the agent version on its device."""
        if self.info is None:
            return
        registry = dr.async_get(self.hass)
        for device in dr.async_entries_for_config_entry(registry, self._entry_id):
            if (DOMAIN, self.agent_id) in device.identifiers:
                registry.async_update_device(
                    device.id, sw_version=self.info.get("agent_version")
                )

    @callback
    def async_stop(self) -> None:
        """Unsubscribe and fail the commands still waiting."""
        for unsubscribe in self._unsubscribe:
            unsubscribe()
        self._unsubscribe.clear()
        if self._check is not None:
            self._check()
            self._check = None
        self.disarm()
        for future in self._pending.values():
            future.cancel()
        self._pending.clear()

    @callback
    def add_listener(self, listener: CALLBACK_TYPE) -> CALLBACK_TYPE:
        """Register a function called after every data change."""
        self._listeners.append(listener)

        @callback
        def remove() -> None:
            self._listeners.remove(listener)

        return remove

    @callback
    def _notify(self) -> None:
        # A listener can add entities, which register new listeners.
        for listener in list(self._listeners):
            try:
                listener()
            except Exception:
                # One broken entity must not freeze all the others.
                _LOGGER.exception("Updating from agent %s failed", self.agent_id)

    @property
    def is_proxmox(self) -> bool:
        """Return True when the agent drives Proxmox VE."""
        return (self.state or {}).get("platform") == PLATFORM_PROXMOX

    @property
    def vendor(self) -> str:
        """Return the name shown as manufacturer of VMs and datastores."""
        return "Proxmox" if self.is_proxmox else "VMware"

    def items(self, section: str) -> list[dict[str, Any]]:
        """Return the hosts, vms or datastores list."""
        return (self.state or {}).get(section, [])

    def item(self, section: str, item_id: str) -> dict[str, Any] | None:
        """Return one record of a list."""
        return next((i for i in self.items(section) if i["id"] == item_id), None)

    @callback
    def _open(self, message: mqtt.ReceiveMessage, leaf: str) -> Envelope | None:
        try:
            envelope = unseal(self._key, message.topic, message.payload)
        except ProtocolError as err:
            # Once per link: a wrong key would otherwise flood the log.
            if not self._rejected:
                self._rejected = True
                _LOGGER.warning(
                    "Ignoring message from agent %s on %s: %s",
                    self.agent_id,
                    leaf,
                    err,
                )
            return None
        if envelope.ts < self._timestamps.get(leaf, 0):
            return None
        self._timestamps[leaf] = envelope.ts
        return envelope

    @callback
    def _on_status(self, message: mqtt.ReceiveMessage) -> None:
        self.online = message.payload == STATUS_ONLINE
        if self.online:
            self._async_seen_online()
        self._notify()

    @callback
    def _on_info(self, message: mqtt.ReceiveMessage) -> None:
        if envelope := self._open(message, LEAF_INFO):
            self.info = envelope.body
            self.async_update_agent_device()
            self._async_check_version()
            self._notify()

    @callback
    def _on_state(self, message: mqtt.ReceiveMessage) -> None:
        if envelope := self._open(message, LEAF_STATE):
            self.state = envelope.body
            # A live signed message proves the agent runs. A retained one only
            # shows what it last said, it may be long gone.
            if not message.retain:
                self.online = True
                self._async_seen_online()
            self._notify()

    @callback
    def _on_operation(self, message: mqtt.ReceiveMessage) -> None:
        if envelope := self._open(message, LEAF_OPERATION):
            self.operation = envelope.body
            self._notify()

    @callback
    def _on_result(self, message: mqtt.ReceiveMessage) -> None:
        try:
            envelope = unseal(self._key, message.topic, message.payload)
        except ProtocolError:
            return
        future = self._pending.get(envelope.body.get("command_id", ""))
        if future is not None and not future.done():
            future.set_result(envelope.body)

    async def async_command(self, kind: str, body: dict[str, Any]) -> dict[str, Any]:
        """Send a command and return the answer of the agent."""
        if not self.online:
            raise agent_error_offline()
        target = topic(self.agent_id, LEAF_COMMAND)
        payload = seal(self._key, target, kind, body, ttl=COMMAND_TTL)
        command_id = unseal(self._key, target, payload).id
        future: asyncio.Future[dict[str, Any]] = self.hass.loop.create_future()
        self._pending[command_id] = future
        try:
            await mqtt.async_publish(self.hass, target, payload, qos=_QOS, retain=False)
            async with asyncio.timeout(COMMAND_TIMEOUT):
                result = await future
        except TimeoutError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="no_response"
            ) from err
        finally:
            self._pending.pop(command_id, None)
        if not result.get("ok"):
            raise agent_error(str(result.get("code")))
        return result

    @property
    def operation_running(self) -> bool:
        """Return True while the agent reports a running operation."""
        return bool(self.operation and self.operation.get("status") == OP_RUNNING)

    @callback
    def arm(self) -> None:
        """Allow the shutdown button for a short time."""
        self.disarm()
        self.armed = True

        @callback
        def expire(_now: Any) -> None:
            self._disarm = None
            self.disarm()

        self._disarm = async_call_later(self.hass, ARM_SECONDS, expire)
        self._notify()

    @callback
    def disarm(self) -> None:
        """Lock the shutdown button."""
        if self._disarm is not None:
            self._disarm()
            self._disarm = None
        if self.armed:
            self.armed = False
            self._notify()

    async def async_shutdown_cluster(self, *, dry_run: bool = False) -> dict[str, Any]:
        """Check the shutdown, or run it when dry_run is False."""
        result = await self.async_command(CMD_CLUSTER_SHUTDOWN, {"dry_run": dry_run})
        if not dry_run:
            self.disarm()
        return result

    async def async_start_cluster(self) -> dict[str, Any]:
        """Start the hosts and vCenter."""
        return await self.async_command(CMD_CLUSTER_START, {})

    async def async_cancel(self) -> dict[str, Any]:
        """Cancel the running operation."""
        return await self.async_command(CMD_CANCEL, {})

    async def async_vm_power(self, vm_id: str, action: str) -> dict[str, Any]:
        """Change the power state of one VM."""
        return await self.async_command(
            CMD_VM_POWER, {"vm_id": vm_id, "action": action}
        )

    async def async_start_scenario(self, scenario: dict[str, Any]) -> dict[str, Any]:
        """Run a scenario."""
        return await self.async_command(CMD_SCENARIO_START, {"scenario": scenario})

    async def async_stop_scenario(self, scenario: dict[str, Any]) -> dict[str, Any]:
        """Stop the VMs of a scenario."""
        return await self.async_command(CMD_SCENARIO_STOP, {"scenario": scenario})


def agent_error_offline() -> HomeAssistantError:
    """Return the error for an agent that is not connected."""
    return HomeAssistantError(translation_domain=DOMAIN, translation_key="agent_offline")


def not_armed_error() -> ServiceValidationError:
    """Return the error for a shutdown without arming."""
    return ServiceValidationError(translation_domain=DOMAIN, translation_key="not_armed")
