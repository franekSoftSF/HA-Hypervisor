"""Diagnostics download, without the shared key, with the end of the agent log."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from . import HaHypervisorConfigEntry
from .const import CONF_SHARED_KEY


async def async_get_config_entry_diagnostics(
    _hass: HomeAssistant, entry: HaHypervisorConfigEntry
) -> dict[str, Any]:
    """Return what the integration knows and what the agent logged last."""
    link = entry.runtime_data
    data: dict[str, Any] = {
        "entry": async_redact_data(dict(entry.data), {CONF_SHARED_KEY}),
        "online": link.online,
        "info": link.info,
        "state": link.state,
        "operation": link.operation,
    }
    try:
        data["agent_log"] = await link.async_agent_log()
    except HomeAssistantError as err:
        # Offline, or an agent older than 0.10.0 that does not know the command.
        data["agent_log_error"] = str(err) or type(err).__name__
    return data
