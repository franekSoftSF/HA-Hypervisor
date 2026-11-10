"""Binary sensors: agent, management API (vCenter or Proxmox), hosts and guest tools."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import HaHypervisorConfigEntry
from .entity import SECTION_HOSTS, SECTION_VMS, ClusterEntity, ItemEntity, track_items
from .link import AgentLink


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: HaHypervisorConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the binary sensors."""
    link = entry.runtime_data
    async_add_entities([AgentOnline(link)])

    added: set[str] = set()

    @callback
    def add_api_sensor() -> None:
        # The platform is known from the first state of the agent.
        if link.state is None:
            return
        key = "proxmox_reachable" if link.is_proxmox else "vcenter_reachable"
        if key not in added:
            added.add(key)
            async_add_entities([ApiReachable(link, key)])

    add_api_sensor()
    entry.async_on_unload(link.add_listener(add_api_sensor))
    track_items(link, entry, SECTION_HOSTS, lambda i: [HostConnected(link, i)], async_add_entities)
    track_items(link, entry, SECTION_VMS, lambda i: [ToolsRunning(link, i)], async_add_entities)


class AgentOnline(ClusterEntity, BinarySensorEntity):
    """Connection of the agent to the broker. It has to work without the agent."""

    _attr_translation_key = "agent_online"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, link: AgentLink) -> None:
        """Set the identity."""
        super().__init__(link, "agent_online")

    @property
    def available(self) -> bool:
        """Always report, offline is a valid answer."""
        return True

    @property
    def is_on(self) -> bool:
        """Return True when the agent is connected."""
        return self.link.online


class ApiReachable(ClusterEntity, BinarySensorEntity):
    """Whether the agent can use vCenter or the Proxmox API."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, link: AgentLink, key: str) -> None:
        """Set the identity."""
        super().__init__(link, key)
        self._attr_translation_key = key

    @property
    def is_on(self) -> bool:
        """Return True when the management API answers."""
        return bool((self.link.state or {}).get("vcenter", {}).get("reachable"))


class HostConnected(ItemEntity, BinarySensorEntity):
    """Whether a host responds."""

    _attr_translation_key = "host_connected"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, link: AgentLink, item: dict[str, Any]) -> None:
        """Set the identity."""
        super().__init__(link, SECTION_HOSTS, item, "connected")

    @property
    def is_on(self) -> bool:
        """Return True when the host is connected."""
        return bool(self.item and self.item["connected"])


class ToolsRunning(ItemEntity, BinarySensorEntity):
    """Whether VMware Tools or the QEMU guest agent answer in a guest."""

    _attr_device_class = BinarySensorDeviceClass.RUNNING
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, link: AgentLink, item: dict[str, Any]) -> None:
        """Set the identity."""
        super().__init__(link, SECTION_VMS, item, "tools")
        self._attr_translation_key = "vm_guest_agent" if link.is_proxmox else "vm_tools"

    @property
    def is_on(self) -> bool:
        """Return True when the tools run."""
        return bool(self.item and self.item["tools"])
