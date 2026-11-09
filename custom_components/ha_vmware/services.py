"""Actions for automations."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
import voluptuous as vol

from .const import DOMAIN
from .entity import scenario_payload, scenario_subentries
from .link import AgentLink

ATTR_CONFIG_ENTRY = "config_entry_id"
ATTR_SCENARIO = "scenario"
ATTR_CONFIRM = "confirm"
ATTR_DRY_RUN = "dry_run"

_ENTRY = {vol.Optional(ATTR_CONFIG_ENTRY): cv.string}
_SCENARIO = vol.Schema({**_ENTRY, vol.Required(ATTR_SCENARIO): cv.string})
_START = vol.Schema(_ENTRY)
_SHUTDOWN = vol.Schema(
    {
        **_ENTRY,
        vol.Optional(ATTR_CONFIRM, default=False): cv.boolean,
        vol.Optional(ATTR_DRY_RUN, default=False): cv.boolean,
    }
)


def _error(key: str, **placeholders: str) -> ServiceValidationError:
    return ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key=key,
        translation_placeholders=placeholders,
    )


def _entry(call: ServiceCall) -> Any:
    entries = [
        entry
        for entry in call.hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED
    ]
    wanted = call.data.get(ATTR_CONFIG_ENTRY)
    if wanted:
        entries = [entry for entry in entries if entry.entry_id == wanted]
    if len(entries) != 1:
        raise _error("agent_ambiguous")
    return entries[0]


def _link(call: ServiceCall) -> AgentLink:
    return _entry(call).runtime_data


def _scenario(call: ServiceCall) -> dict[str, Any]:
    entry = _entry(call)
    wanted = call.data[ATTR_SCENARIO]
    for subentry in scenario_subentries(entry):
        if wanted in (subentry.subentry_id, subentry.title):
            return scenario_payload(subentry)
    raise _error("scenario_not_found", name=wanted)


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the actions of the integration."""

    async def start_scenario(call: ServiceCall) -> None:
        await _link(call).async_start_scenario(_scenario(call))

    async def stop_scenario(call: ServiceCall) -> None:
        await _link(call).async_stop_scenario(_scenario(call))

    async def start_cluster(call: ServiceCall) -> None:
        await _link(call).async_start_cluster()

    async def shutdown_cluster(call: ServiceCall) -> ServiceResponse:
        dry_run = call.data[ATTR_DRY_RUN]
        if not dry_run and not call.data[ATTR_CONFIRM]:
            raise _error("confirm_required")
        result = await _link(call).async_shutdown_cluster(dry_run=dry_run)
        return {"operation_id": result.get("operation_id")}

    hass.services.async_register(DOMAIN, "start_scenario", start_scenario, _SCENARIO)
    hass.services.async_register(DOMAIN, "stop_scenario", stop_scenario, _SCENARIO)
    hass.services.async_register(DOMAIN, "start_cluster", start_cluster, _START)
    hass.services.async_register(
        DOMAIN,
        "shutdown_cluster",
        shutdown_cluster,
        _SHUTDOWN,
        supports_response=SupportsResponse.OPTIONAL,
    )
