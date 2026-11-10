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
from homeassistant.helpers import (
    config_validation as cv,
    device_registry as dr,
    entity_registry as er,
)
from homeassistant.helpers.target import (
    TargetSelection,
    async_extract_referenced_entity_ids,
)
import voluptuous as vol

from .const import DOMAIN
from .entity import scenario_payload, scenario_subentries
from .link import AgentLink
from .protocol import (
    CMD_SNAPSHOT_CREATE,
    CMD_SNAPSHOT_DELETE,
    CMD_SNAPSHOT_REVERT,
    SNAPSHOT_NAME,
)

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
ATTR_NAME = "name"
_SNAPSHOT_TARGET = {
    **cv.TARGET_SERVICE_FIELDS,
    vol.Required(ATTR_NAME): cv.string,
}
_SNAPSHOT_CREATE = vol.Schema(
    {
        **_SNAPSHOT_TARGET,
        vol.Optional("description", default=""): cv.string,
        vol.Optional("memory", default=False): cv.boolean,
    }
)
_SNAPSHOT_REVERT = vol.Schema(
    {**_SNAPSHOT_TARGET, vol.Optional("start", default=False): cv.boolean}
)
_SNAPSHOT_DELETE = vol.Schema(_SNAPSHOT_TARGET)


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


def _vms_by_agent(call: ServiceCall) -> dict[str, tuple[AgentLink, list[str]]]:
    """Return the selected VMs grouped by agent: entry id -> (link, vm ids)."""
    hass = call.hass
    name = call.data[ATTR_NAME]
    if not SNAPSHOT_NAME.match(name):
        raise _error("invalid_snapshot_name", name=name)
    selected = async_extract_referenced_entity_ids(hass, TargetSelection(call.data))
    entities = er.async_get(hass)
    device_ids = set(selected.referenced_devices)
    # Devices count directly, an entity such as a VM power switch through its device.
    for entity_id in selected.referenced:
        if (entity := entities.async_get(entity_id)) and entity.device_id:
            device_ids.add(entity.device_id)

    loaded = {
        entry.entry_id: entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED
    }
    devices = dr.async_get(hass)
    grouped: dict[str, tuple[AgentLink, list[str]]] = {}
    for device_id in device_ids:
        device = devices.async_get(device_id)
        if device is None:
            continue
        entry_id = next((item for item in device.config_entries if item in loaded), None)
        if entry_id is None:
            continue
        link: AgentLink = loaded[entry_id].runtime_data
        for domain, identifier in device.identifiers:
            parts = identifier.split(":", 2)
            if domain == DOMAIN and len(parts) == 3 and parts[:2] == [link.agent_id, "vms"]:
                grouped.setdefault(entry_id, (link, []))[1].append(parts[2])
    if not grouped:
        raise _error("no_vm_selected")
    return grouped


async def _snapshot(call: ServiceCall, kind: str, **extra: Any) -> ServiceResponse:
    operations: dict[str, Any] = {}
    for entry_id, (link, vms) in _vms_by_agent(call).items():
        body = {"vms": sorted(vms), "name": call.data[ATTR_NAME], **extra}
        result = await link.async_command(kind, body)
        operations[entry_id] = result.get("operation_id")
    return {"operations": operations}


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

    async def create_snapshot(call: ServiceCall) -> ServiceResponse:
        return await _snapshot(
            call,
            CMD_SNAPSHOT_CREATE,
            description=call.data["description"],
            memory=call.data["memory"],
        )

    async def revert_snapshot(call: ServiceCall) -> ServiceResponse:
        return await _snapshot(call, CMD_SNAPSHOT_REVERT, start=call.data["start"])

    async def delete_snapshot(call: ServiceCall) -> ServiceResponse:
        return await _snapshot(call, CMD_SNAPSHOT_DELETE)

    for name, handler, schema in (
        ("create_snapshot", create_snapshot, _SNAPSHOT_CREATE),
        ("revert_snapshot", revert_snapshot, _SNAPSHOT_REVERT),
        ("delete_snapshot", delete_snapshot, _SNAPSHOT_DELETE),
    ):
        hass.services.async_register(
            DOMAIN, name, handler, schema, supports_response=SupportsResponse.OPTIONAL
        )

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
