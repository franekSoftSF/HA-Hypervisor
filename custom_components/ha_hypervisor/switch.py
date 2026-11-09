"""Switches: VM power and arming of the cluster shutdown."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import HaHypervisorConfigEntry
from .entity import SECTION_VMS, ClusterEntity, ItemEntity, track_items
from .link import AgentLink
from .protocol import VM_ACTION_ON, VM_ACTION_SHUTDOWN


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: HaHypervisorConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the switches."""
    link = entry.runtime_data
    async_add_entities([ShutdownArmed(link)])

    def vm_switches(item: dict[str, Any]) -> list[VmPower]:
        if item.get("system") or item.get("protected"):
            return []
        return [VmPower(link, item)]

    track_items(link, entry, SECTION_VMS, vm_switches, async_add_entities)


class ShutdownArmed(ClusterEntity, SwitchEntity):
    """Safety catch: unlocks the shutdown button for a minute."""

    _attr_translation_key = "shutdown_armed"

    def __init__(self, link: AgentLink) -> None:
        """Set the identity."""
        super().__init__(link, "shutdown_armed")

    @property
    def is_on(self) -> bool:
        """Return True while armed."""
        return self.link.armed

    async def async_turn_on(self, **_kwargs: Any) -> None:
        """Arm."""
        self.link.arm()

    async def async_turn_off(self, **_kwargs: Any) -> None:
        """Disarm."""
        self.link.disarm()


class VmPower(ItemEntity, SwitchEntity):
    """Power of a VM: off asks the guest to shut down."""

    _attr_translation_key = "vm_power"
    _attr_device_class = SwitchDeviceClass.SWITCH

    def __init__(self, link: AgentLink, item: dict[str, Any]) -> None:
        """Set the identity."""
        super().__init__(link, SECTION_VMS, item, "power")

    @property
    def is_on(self) -> bool:
        """Return True when the VM is powered on."""
        return bool(self.item and self.item["power"] == "on")

    async def async_turn_on(self, **_kwargs: Any) -> None:
        """Power on."""
        await self.link.async_vm_power(self._item_id, VM_ACTION_ON)

    async def async_turn_off(self, **_kwargs: Any) -> None:
        """Shut the guest down."""
        await self.link.async_vm_power(self._item_id, VM_ACTION_SHUTDOWN)
