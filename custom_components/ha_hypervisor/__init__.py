"""Hypervisor lab control through an agent connected over MQTT."""

from __future__ import annotations

from homeassistant.components import mqtt
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.typing import ConfigType
from homeassistant.loader import async_get_integration

from .const import CONF_AGENT_ID, CONF_SHARED_KEY, DOMAIN
from .link import AgentLink
from .panel import async_add_panel, async_remove_panel, async_setup_panel_support
from .services import async_setup_services

PLATFORMS = [Platform.BINARY_SENSOR, Platform.BUTTON, Platform.SENSOR, Platform.SWITCH]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type HaHypervisorConfigEntry = ConfigEntry[AgentLink]


async def async_setup(hass: HomeAssistant, _config: ConfigType) -> bool:
    """Register the actions and what the sidebar panel needs."""
    async_setup_services(hass)
    await async_setup_panel_support(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: HaHypervisorConfigEntry) -> None:
    """Connect to the agent."""
    if not await mqtt.async_wait_for_mqtt_client(hass):
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN, translation_key="mqtt_not_ready"
        )
    integration = await async_get_integration(hass, DOMAIN)
    link = AgentLink(
        hass,
        entry.entry_id,
        entry.data[CONF_AGENT_ID],
        entry.data[CONF_SHARED_KEY],
        str(integration.version) if integration.version else None,
    )
    await link.async_start()
    entry.runtime_data = link
    entry.async_on_unload(link.async_stop)
    # Scenario changes arrive here. Because of this listener the flows must use
    # async_update_and_abort, never the reloading methods.
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    link.async_update_agent_device()
    await async_add_panel(hass)


async def _async_reload(hass: HomeAssistant, entry: HaHypervisorConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: HaHypervisorConfigEntry) -> None:
    """Disconnect from the agent."""
    async_remove_panel(hass, entry.entry_id)
    await hass.config_entries.async_unload_platforms_or_raise(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: HaHypervisorConfigEntry) -> None:
    """Forget the repairs of a removed agent."""
    agent_id = entry.data[CONF_AGENT_ID]
    for issue in (f"agent_unreachable_{agent_id}", f"agent_outdated_{agent_id}"):
        ir.async_delete_issue(hass, DOMAIN, issue)


async def async_remove_config_entry_device(
    _hass: HomeAssistant, entry: HaHypervisorConfigEntry, device: DeviceEntry
) -> bool:
    """Allow removing a host, VM or datastore the agent no longer reports."""
    link = entry.runtime_data
    for domain, identifier in device.identifiers:
        if domain != DOMAIN:
            continue
        parts = identifier.split(":", 2)
        if len(parts) == 3 and parts[1] in ("hosts", "vms", "datastores"):
            return link.item(parts[1], parts[2]) is None
    return False
