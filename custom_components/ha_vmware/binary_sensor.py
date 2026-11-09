"""Binary sensors: agent, vCenter, hosts and VMware Tools."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import HaVmwareConfigEntry
from .entity import SECTION_HOSTS, SECTION_VMS, ClusterEntity, ItemEntity, track_items
from .link import AgentLink


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: HaVmwareConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the binary sensors."""
    link = entry.runtime_data
    async_add_entities([AgentOnline(link), VCenterReachable(link)])
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


class VCenterReachable(ClusterEntity, BinarySensorEntity):
    """Whether the agent can log in to vCenter."""

    _attr_translation_key = "vcenter_reachable"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, link: AgentLink) -> None:
        """Set the identity."""
        super().__init__(link, "vcenter_reachable")

    @property
    def is_on(self) -> bool:
        """Return True when vCenter answers."""
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
    """Whether VMware Tools run in a guest."""

    _attr_translation_key = "vm_tools"
    _attr_device_class = BinarySensorDeviceClass.RUNNING
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, link: AgentLink, item: dict[str, Any]) -> None:
        """Set the identity."""
        super().__init__(link, SECTION_VMS, item, "tools")

    @property
    def is_on(self) -> bool:
        """Return True when the tools run."""
        return bool(self.item and self.item["tools"])
