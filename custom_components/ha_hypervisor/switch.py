"""Switches: VM power, scenarios and arming of the cluster shutdown."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import HaHypervisorConfigEntry
from .entity import (
    SCENARIO_ACTIVE,
    SCENARIO_PARTIAL,
    SCENARIO_STARTING,
    SECTION_VMS,
    ClusterEntity,
    ItemEntity,
    ScenarioEntity,
    scenario_payload,
    scenario_status,
    scenario_subentries,
    track_items,
)
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

    for subentry in scenario_subentries(entry):
        async_add_entities(
            [ScenarioPower(link, subentry)], config_subentry_id=subentry.subentry_id
        )


def _gib(mib: float) -> str:
    return f"{mib / 1024:.1f}"


def usage_texts(vm: dict[str, Any]) -> dict[str, str]:
    """Return CPU, memory and disk of a VM as short texts, CPU and memory only while it runs."""
    texts: dict[str, str] = {}
    if vm.get("power") == "on":
        texts["cpu"] = f"CPU {vm.get('cpu_mhz') or 0} MHz"
        used, total = vm.get("mem_mb") or 0, vm.get("mem_total_mb") or 0
        texts["memory"] = (
            f"RAM {_gib(used)} / {_gib(total)} GiB" if total else f"RAM {_gib(used)} GiB"
        )
    if vm.get("storage_gb") is not None:
        texts["disk"] = f"{vm['storage_gb']:.1f} GiB"
    return texts


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
    # Shown on the panel tiles; they change with every poll and would only
    # fill the recorder, the VM sensors keep the history.
    _unrecorded_attributes = frozenset({"cpu", "memory", "disk"})

    def __init__(self, link: AgentLink, item: dict[str, Any]) -> None:
        """Set the identity."""
        super().__init__(link, SECTION_VMS, item, "power")

    @property
    def is_on(self) -> bool:
        """Return True when the VM is powered on."""
        return bool(self.item and self.item["power"] == "on")

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        """Return the usage as short texts for the panel: CPU, memory, disk."""
        return usage_texts(self.item or {})

    async def async_turn_on(self, **_kwargs: Any) -> None:
        """Power on."""
        await self.link.async_vm_power(self._item_id, VM_ACTION_ON)

    async def async_turn_off(self, **_kwargs: Any) -> None:
        """Shut the guest down."""
        await self.link.async_vm_power(self._item_id, VM_ACTION_SHUTDOWN)


class ScenarioPower(ScenarioEntity, SwitchEntity):
    """A scenario as one switch: on starts it, off stops it."""

    _attr_translation_key = "scenario_power"

    def __init__(self, link: AgentLink, subentry) -> None:  # noqa: ANN001
        """Set the identity."""
        super().__init__(link, subentry, "power")

    @property
    def is_on(self) -> bool:
        """Return True while the scenario runs or is starting."""
        return scenario_status(self.link, self._subentry) in (
            SCENARIO_ACTIVE,
            SCENARIO_PARTIAL,
            SCENARIO_STARTING,
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Start the scenario."""
        await self.link.async_start_scenario(scenario_payload(self._subentry))

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Stop the scenario."""
        await self.link.async_stop_scenario(scenario_payload(self._subentry))
