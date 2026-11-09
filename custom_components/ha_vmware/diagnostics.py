"""Diagnostics download, without the shared key."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import HaVmwareConfigEntry
from .const import CONF_SHARED_KEY


async def async_get_config_entry_diagnostics(
    _hass: HomeAssistant, entry: HaVmwareConfigEntry
) -> dict[str, Any]:
    """Return what the integration knows."""
    link = entry.runtime_data
    return {
        "entry": async_redact_data(dict(entry.data), {CONF_SHARED_KEY}),
        "online": link.online,
        "info": link.info,
        "state": link.state,
        "operation": link.operation,
    }
