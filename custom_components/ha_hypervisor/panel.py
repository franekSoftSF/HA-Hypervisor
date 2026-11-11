"""Sidebar panel: a dashboard built from what the agents report.

The panel itself is a small frontend module (frontend/panel.js) made of the
standard cards. It asks the websocket command below which entities belong to
which cluster, host, VM and scenario, so nothing has to be configured.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import voluptuous as vol

from homeassistant.components import frontend, panel_custom, websocket_api
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.loader import async_get_integration

from .const import DOMAIN
from .entity import scenario_vms

PANEL_URL = "hypervisor"
PANEL_ELEMENT = "ha-hypervisor-panel"
PANEL_TITLE = "Hypervisor"
PANEL_ICON = "mdi:server-network"
STATIC_URL = f"/{DOMAIN}_static"
WS_DASHBOARD = f"{DOMAIN}/dashboard"
_REGISTERED = f"{DOMAIN}_panel"

_ITEM_SECTIONS = {"hosts", "vms", "datastores"}


async def async_setup_panel_support(hass: HomeAssistant) -> None:
    """Serve the frontend module and answer the dashboard command, once."""
    websocket_api.async_register_command(hass, ws_dashboard)
    if hass.http is None:
        return
    await hass.http.async_register_static_paths(
        [StaticPathConfig(STATIC_URL, str(Path(__file__).parent / "frontend"), True)]
    )


async def async_add_panel(hass: HomeAssistant) -> None:
    """Show the panel in the sidebar while at least one agent is set up."""
    if hass.data.get(_REGISTERED) or hass.http is None:
        return
    hass.data[_REGISTERED] = True
    version = (await async_get_integration(hass, DOMAIN)).version
    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_URL,
        webcomponent_name=PANEL_ELEMENT,
        module_url=f"{STATIC_URL}/panel.js?v={version}",
        sidebar_title=PANEL_TITLE,
        sidebar_icon=PANEL_ICON,
        require_admin=False,
    )


@callback
def async_remove_panel(hass: HomeAssistant, leaving_entry_id: str) -> None:
    """Remove the panel when the last agent goes away."""
    if not hass.data.get(_REGISTERED):
        return
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.entry_id != leaving_entry_id and entry.state is ConfigEntryState.LOADED:
            return
    hass.data[_REGISTERED] = False
    frontend.async_remove_panel(hass, PANEL_URL, warn_if_unknown=False)


def _classify(agent_id: str, device: dr.DeviceEntry) -> tuple[str, str] | None:
    """Return (kind, id) of a device of this agent, or None."""
    for domain, identifier in device.identifiers:
        if domain != DOMAIN:
            continue
        if identifier == agent_id:
            return "cluster", agent_id
        parts = identifier.split(":", 2)
        if len(parts) == 3 and parts[0] == agent_id:
            if parts[1] in _ITEM_SECTIONS:
                return parts[1], parts[2]
            if parts[1] == "scenario":
                return "scenarios", parts[2]
    return None


@callback
def build_dashboard(hass: HomeAssistant) -> dict[str, Any]:
    """Describe every loaded agent: its devices and their entities by key."""
    devices = dr.async_get(hass)
    entities = er.async_get(hass)
    agents: list[dict[str, Any]] = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is not ConfigEntryState.LOADED:
            continue
        link = entry.runtime_data
        agent: dict[str, Any] = {
            "entry_id": entry.entry_id,
            "title": entry.title,
            "agent_id": link.agent_id,
            "online": link.online,
            "cluster": {},
            "hosts": [],
            "vms": [],
            "datastores": [],
            "scenarios": [],
        }
        records: dict[str, dict[str, Any]] = {}
        for device in dr.async_entries_for_config_entry(devices, entry.entry_id):
            found = _classify(link.agent_id, device)
            if found is None:
                continue
            kind, item_id = found
            if kind == "cluster":
                records[device.id] = agent["cluster"]
                continue
            record: dict[str, Any] = {
                "id": item_id,
                "device_id": device.id,
                "name": device.name_by_user or device.name or item_id,
                "entities": {},
            }
            if kind == "vms":
                item = link.item("vms", item_id) or {}
                record["system"] = bool(item.get("system"))
                record["protected"] = bool(item.get("protected"))
            agent[kind].append(record)
            records[device.id] = record["entities"]
        for entity in er.async_entries_for_config_entry(entities, entry.entry_id):
            if entity.disabled_by is not None or entity.translation_key is None:
                continue
            target = records.get(entity.device_id or "")
            if target is not None:
                target[entity.translation_key] = entity.entity_id
        # The VMs of each scenario, in start order, as their power switches.
        power = {vm["id"]: vm["entities"].get("vm_power") for vm in agent["vms"]}
        for scenario in agent["scenarios"]:
            subentry = entry.subentries.get(scenario["id"])
            vm_ids = scenario_vms(subentry) if subentry else []
            scenario["vms"] = [power[vm] for vm in vm_ids if power.get(vm)]
        for kind in ("hosts", "vms", "datastores", "scenarios"):
            agent[kind].sort(key=lambda record: record["name"].casefold())
        agents.append(agent)
    agents.sort(key=lambda agent: agent["title"].casefold())
    return {"agents": agents}


@websocket_api.websocket_command({vol.Required("type"): WS_DASHBOARD})
@callback
def ws_dashboard(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Return the layout of the panel."""
    connection.send_result(msg["id"], build_dashboard(hass))
