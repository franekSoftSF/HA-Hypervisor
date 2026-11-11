"""Sidebar panel and the agent checks made after every start."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from homeassistant.components.frontend import DATA_PANELS
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    async_fire_mqtt_message,
    async_fire_time_changed,
)

from conftest import AGENT, make_entry, setup_entry
from custom_components.ha_hypervisor.const import DOMAIN
from custom_components.ha_hypervisor.panel import PANEL_URL
from custom_components.ha_hypervisor.protocol import topic

COMPONENT = Path(__file__).resolve().parents[2] / "custom_components" / "ha_hypervisor"


async def test_panel_is_in_the_sidebar_while_an_agent_is_loaded(hass, agent):
    entry = await setup_entry(hass, agent)
    panel = hass.data[DATA_PANELS][PANEL_URL]
    assert panel.sidebar_title == "Hypervisor"
    assert panel.config["_panel_custom"]["name"] == "ha-hypervisor-panel"
    assert "/panel.js?v=" in panel.config["_panel_custom"]["module_url"]
    assert (COMPONENT / "frontend" / "panel.js").is_file()

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert PANEL_URL not in hass.data[DATA_PANELS]
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert PANEL_URL in hass.data[DATA_PANELS]


async def test_dashboard_command_describes_the_lab(hass, agent, hass_ws_client):
    await setup_entry(hass, agent)
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": f"{DOMAIN}/dashboard"})
    reply = await client.receive_json()
    assert reply["success"]
    (lab,) = reply["result"]["agents"]
    assert lab["agent_id"] == AGENT
    assert lab["online"] is True
    assert lab["cluster"]["cluster_state"].startswith("sensor.")
    assert lab["cluster"]["cluster_shutdown"].startswith("button.")
    assert lab["cluster"]["shutdown_armed"].startswith("switch.")
    assert [host["name"] for host in lab["hosts"]] == ["esxi1"]
    assert "host_power" in lab["hosts"][0]["entities"]
    vms = {vm["name"]: vm for vm in lab["vms"]}
    assert vms["web"]["entities"]["vm_power"].startswith("switch.")
    assert "vm_power" not in vms["vcsa"]["entities"]
    assert vms["vcsa"]["protected"] is True
    assert vms["vCLS-1"]["system"] is True
    assert lab["datastores"][0]["entities"]["datastore_used_percent"]
    assert list(lab) == [
        "entry_id", "title", "agent_id", "online", "cluster",
        "hosts", "vms", "datastores", "scenarios",
    ]


async def test_older_agent_raises_a_repair(hass, agent):
    await setup_entry(hass, agent)
    issues = ir.async_get(hass)
    assert issues.async_get_issue(DOMAIN, f"agent_outdated_{AGENT}") is None

    agent.fire("info", "info", {"agent_version": "0.1.0", "protocol": 1, "agent_id": AGENT})
    await hass.async_block_till_done()
    issue = issues.async_get_issue(DOMAIN, f"agent_outdated_{AGENT}")
    assert issue is not None
    assert issue.translation_placeholders["agent_version"] == "0.1.0"

    agent.fire("info", "info", {"agent_version": "99.0.0", "protocol": 1, "agent_id": AGENT})
    await hass.async_block_till_done()
    assert issues.async_get_issue(DOMAIN, f"agent_outdated_{AGENT}") is None


async def test_silent_agent_after_start_raises_a_repair(hass, agent):
    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    # The refresh request went out at start.
    assert [c.kind for c in agent.all_commands()] == ["refresh"]

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
    await hass.async_block_till_done()
    issues = ir.async_get(hass)
    assert issues.async_get_issue(DOMAIN, f"agent_unreachable_{AGENT}") is not None

    async_fire_mqtt_message(hass, topic(AGENT, "status"), "online")
    await hass.async_block_till_done()
    assert issues.async_get_issue(DOMAIN, f"agent_unreachable_{AGENT}") is None


async def test_answering_agent_raises_no_repair(hass, agent):
    await setup_entry(hass, agent)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"agent_unreachable_{AGENT}") is None


async def test_removing_the_agent_clears_its_repairs(hass, agent):
    entry = await setup_entry(hass, agent)
    agent.fire("info", "info", {"agent_version": "0.1.0", "protocol": 1, "agent_id": AGENT})
    await hass.async_block_till_done()
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"agent_outdated_{AGENT}") is None


async def test_agent_log_for_the_download_button(hass, agent, hass_ws_client):
    entry = await setup_entry(hass, agent)
    agent.answers["logs"] = {"lines": ["first", "Operation failed: esxi_unavailable"]}
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": f"{DOMAIN}/agent_log", "entry_id": entry.entry_id, "lines": 50})
    response = await client.receive_json()
    assert response["success"], response
    assert response["result"]["lines"] == ["first", "Operation failed: esxi_unavailable"]
    assert response["result"]["agent_id"] == AGENT
    (command,) = [c for c in agent.commands() if c.kind == "logs"]
    assert command.body == {"lines": 50}

    # An agent older than the command answers unknown_command.
    agent.answers["logs"] = {"ok": False, "code": "unknown_command"}
    await client.send_json_auto_id({"type": f"{DOMAIN}/agent_log", "entry_id": entry.entry_id})
    response = await client.receive_json()
    assert not response["success"]
    assert response["error"]["code"] == "agent_error"

    await client.send_json_auto_id({"type": f"{DOMAIN}/agent_log", "entry_id": "nothing"})
    assert (await client.receive_json())["error"]["code"] == "not_found"


async def test_diagnostics_carry_the_agent_log(hass, agent):
    from custom_components.ha_hypervisor.diagnostics import async_get_config_entry_diagnostics

    entry = await setup_entry(hass, agent)
    agent.answers["logs"] = {"lines": ["Operation failed: no_wake_method"]}
    data = await async_get_config_entry_diagnostics(hass, entry)
    assert data["agent_log"] == ["Operation failed: no_wake_method"]
    agent.answers["logs"] = {"ok": False, "code": "unknown_command"}
    data = await async_get_config_entry_diagnostics(hass, entry)
    assert "agent_log" not in data and data["agent_log_error"]
