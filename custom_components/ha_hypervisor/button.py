"""Buttons: cluster sequences, forced VM power-off and scenarios."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import HaHypervisorConfigEntry
from .entity import (
    SECTION_VMS,
    ClusterEntity,
    ItemEntity,
    ScenarioEntity,
    scenario_payload,
    scenario_subentries,
    track_items,
)
from .link import AgentLink, not_armed_error
from .protocol import CMD_REFRESH, VM_ACTION_OFF


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: HaHypervisorConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the buttons."""
    link = entry.runtime_data

    async def shutdown() -> None:
        if not link.armed:
            raise not_armed_error()
        await link.async_shutdown_cluster()

    async def preview() -> None:
        await link.async_shutdown_cluster(dry_run=True)

    async def refresh() -> None:
        await link.async_command(CMD_REFRESH, {})

    async_add_entities(
        [
            ClusterButton(link, "shutdown_preview", preview),
            ClusterButton(link, "cluster_shutdown", shutdown),
            ClusterButton(link, "cluster_start", link.async_start_cluster),
            ClusterButton(link, "cancel_operation", link.async_cancel),
            ClusterButton(link, "refresh", refresh),
        ]
    )

    def vm_buttons(item: dict[str, Any]) -> list[VmForceOff]:
        if item.get("system") or item.get("protected"):
            return []
        return [VmForceOff(link, item)]

    track_items(link, entry, SECTION_VMS, vm_buttons, async_add_entities)

    for subentry in scenario_subentries(entry):
        async_add_entities(
            [
                ScenarioButton(link, subentry, "scenario_start"),
                ScenarioButton(link, subentry, "scenario_stop"),
            ],
            config_subentry_id=subentry.subentry_id,
        )


class ClusterButton(ClusterEntity, ButtonEntity):
    """An action on the whole cluster."""

    def __init__(
        self, link: AgentLink, key: str, action: Callable[[], Awaitable[Any]]
    ) -> None:
        """Set the identity and the action."""
        super().__init__(link, key)
        self._attr_translation_key = key
        self._action = action

    async def async_press(self) -> None:
        """Run the action."""
        await self._action()


class VmForceOff(ItemEntity, ButtonEntity):
    """Cuts the power of a VM."""

    _attr_translation_key = "vm_force_off"

    def __init__(self, link: AgentLink, item: dict[str, Any]) -> None:
        """Set the identity."""
        super().__init__(link, SECTION_VMS, item, "force_off")

    async def async_press(self) -> None:
        """Power off."""
        await self.link.async_vm_power(self._item_id, VM_ACTION_OFF)


class ScenarioButton(ScenarioEntity, ButtonEntity):
    """Starts or stops a scenario."""

    def __init__(self, link: AgentLink, subentry: ConfigSubentry, key: str) -> None:
        """Set the identity."""
        super().__init__(link, subentry, key)
        self._attr_translation_key = key
        self._key = key

    async def async_press(self) -> None:
        """Send the scenario to the agent."""
        scenario = scenario_payload(self._subentry)
        if self._key == "scenario_start":
            await self.link.async_start_scenario(scenario)
        else:
            await self.link.async_stop_scenario(scenario)
