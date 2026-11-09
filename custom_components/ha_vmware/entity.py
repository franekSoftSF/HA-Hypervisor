"""Shared entity code."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import CONF_POWER_OFF_EMPTY, CONF_STEPS, DOMAIN, SUBENTRY_SCENARIO
from .link import AgentLink

SECTION_HOSTS = "hosts"
SECTION_VMS = "vms"
SECTION_DATASTORES = "datastores"


def agent_device(link: AgentLink) -> DeviceInfo:
    """Return the device representing the agent and its cluster."""
    return DeviceInfo(
        identifiers={(DOMAIN, link.agent_id)},
        name=f"VMware {link.agent_id}",
        manufacturer="HA VMware",
        model="Agent",
    )


def item_device(link: AgentLink, section: str, item: dict[str, Any]) -> DeviceInfo:
    """Return the device of a host, VM or datastore."""
    identifiers = {(DOMAIN, f"{link.agent_id}:{section}:{item['id']}")}
    if section == SECTION_HOSTS:
        return DeviceInfo(
            identifiers=identifiers,
            name=item["name"],
            manufacturer=item.get("vendor") or "VMware",
            model=item.get("model") or "ESXi",
            sw_version=item.get("version"),
        )
    if section == SECTION_VMS:
        return DeviceInfo(
            identifiers=identifiers,
            name=item["name"],
            manufacturer="VMware",
            model=item.get("guest_os") or "Virtual machine",
        )
    return DeviceInfo(
        identifiers=identifiers,
        name=item["name"],
        manufacturer="VMware",
        model=item.get("type") or "Datastore",
    )


def scenario_subentries(entry: ConfigEntry) -> list[ConfigSubentry]:
    """Return the scenarios of a config entry."""
    return [
        subentry
        for subentry in entry.subentries.values()
        if subentry.subentry_type == SUBENTRY_SCENARIO
    ]


def scenario_payload(subentry: ConfigSubentry) -> dict[str, Any]:
    """Return a scenario in the form the agent expects."""
    return {
        "id": subentry.subentry_id,
        "name": subentry.title,
        "steps": list(subentry.data[CONF_STEPS]),
        CONF_POWER_OFF_EMPTY: subentry.data.get(CONF_POWER_OFF_EMPTY, False),
    }


def scenario_device(link: AgentLink, subentry: ConfigSubentry) -> DeviceInfo:
    """Return the device of a scenario."""
    return DeviceInfo(
        identifiers={(DOMAIN, f"{link.agent_id}:scenario:{subentry.subentry_id}")},
        name=subentry.title,
        manufacturer="HA VMware",
        model="Scenario",
    )


@callback
def track_items(
    link: AgentLink,
    entry: ConfigEntry,
    section: str,
    factory: Callable[[dict[str, Any]], Iterable[Entity]],
    add_entities: Callable[[list[Entity]], None],
) -> None:
    """Create entities for records the agent reports, also for later ones."""
    known: set[str] = set()

    @callback
    def update() -> None:
        new: list[Entity] = []
        for item in link.items(section):
            if item["id"] not in known:
                known.add(item["id"])
                new.extend(factory(item))
        if new:
            add_entities(new)

    update()
    entry.async_on_unload(link.add_listener(update))


class AgentEntity(Entity):
    """Base of all entities: updated by the link, unavailable without the agent."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, link: AgentLink) -> None:
        """Keep the link."""
        self.link = link

    @property
    def available(self) -> bool:
        """Return True when the agent is connected and has reported."""
        return self.link.online and self.link.state is not None

    async def async_added_to_hass(self) -> None:
        """Follow the agent data."""
        self.async_on_remove(self.link.add_listener(self.async_write_ha_state))


class ClusterEntity(AgentEntity):
    """Entity of the agent device."""

    def __init__(self, link: AgentLink, key: str) -> None:
        """Set the identity."""
        super().__init__(link)
        self._attr_unique_id = f"{link.agent_id}_{key}"
        self._attr_device_info = agent_device(link)


class ItemEntity(AgentEntity):
    """Entity of a host, VM or datastore."""

    def __init__(
        self, link: AgentLink, section: str, item: dict[str, Any], key: str
    ) -> None:
        """Set the identity."""
        super().__init__(link)
        self._section = section
        self._item_id = item["id"]
        self._attr_unique_id = f"{link.agent_id}_{section}_{item['id']}_{key}"
        self._attr_device_info = item_device(link, section, item)

    @property
    def item(self) -> dict[str, Any] | None:
        """Return the current record."""
        return self.link.item(self._section, self._item_id)

    @property
    def available(self) -> bool:
        """Return True while the agent still reports the record."""
        return super().available and self.item is not None


class ScenarioEntity(AgentEntity):
    """Entity of a scenario."""

    def __init__(self, link: AgentLink, subentry: ConfigSubentry, key: str) -> None:
        """Set the identity."""
        super().__init__(link)
        self._subentry = subentry
        self._attr_unique_id = f"{link.agent_id}_{subentry.subentry_id}_{key}"
        self._attr_device_info = scenario_device(link, subentry)
