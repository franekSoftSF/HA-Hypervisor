"""Config flow, entities, commands and scenarios against the mocked broker."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import MappingProxyType
from unittest.mock import patch

from homeassistant.config_entries import SOURCE_USER, ConfigEntryState, ConfigSubentryData
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er
import pytest

from conftest import AGENT, KEY, setup_entry, state_doc
from custom_components.ha_hypervisor import config_flow
from custom_components.ha_hypervisor.const import DOMAIN
from custom_components.ha_hypervisor.protocol import ERROR_CODES, seal, topic, STEPS

ROOT = Path(__file__).resolve().parents[2]
COMPONENT = ROOT / "custom_components" / "ha_hypervisor"
SCENARIO_DATA = {
    "power_off_empty_hosts": False,
    "steps": [{"vms": ["vm-1"], "wait": "tools", "delay": 0, "timeout": 600}],
}


def scenario_subentry(title="Test VDI"):
    return [
        ConfigSubentryData(
            data=MappingProxyType(SCENARIO_DATA),
            subentry_type="scenario",
            title=title,
            unique_id=None,
        )
    ]


def entity_id(hass, unique_id, domain):
    result = er.async_get(hass).async_get_entity_id(domain, DOMAIN, unique_id)
    assert result is not None, unique_id
    return result


def test_translations_cover_every_code_in_both_languages():
    def keys(node, prefix=""):
        result = set()
        for key, value in node.items():
            result |= keys(value, f"{prefix}{key}.") if isinstance(value, dict) else {prefix + key}
        return result

    english = json.loads((COMPONENT / "translations/en.json").read_text(encoding="utf-8"))
    polish = json.loads((COMPONENT / "translations/pl.json").read_text(encoding="utf-8"))
    assert keys(english) == keys(polish)
    for code in ERROR_CODES:
        assert f"agent_{code}" in english["exceptions"]
    for step in STEPS:
        assert step in english["entity"]["sensor"]["operation_step"]["state"]


def test_manifest_has_marketplace_fields():
    manifest = json.loads((COMPONENT / "manifest.json").read_text(encoding="utf-8"))
    for key in ("domain", "documentation", "issue_tracker", "codeowners", "name", "version"):
        assert manifest[key]
    assert manifest["dependencies"] == ["mqtt"]
    assert (COMPONENT / "brand" / "icon.png").exists()


@pytest.mark.parametrize(
    ("probe", "error"),
    [("agent_not_found", "agent_not_found"), ("invalid_key", "invalid_key")],
)
async def test_flow_shows_probe_errors(hass, agent, probe, error):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    with patch.object(config_flow, "_async_probe_agent", return_value=probe):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"agent_id": AGENT, "shared_key": KEY}
        )
    assert result["errors"] == {"base": error}


async def test_flow_creates_entry(hass, agent):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    with patch.object(config_flow, "_async_probe_agent", return_value=None):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"agent_id": AGENT, "shared_key": KEY}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {"agent_id": AGENT, "shared_key": KEY}


async def test_flow_rejects_short_key_and_bad_id(hass, agent):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"agent_id": AGENT, "shared_key": "short"}
    )
    assert result["errors"] == {"base": "invalid_key_format"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"agent_id": "Bad/Id", "shared_key": KEY}
    )
    assert result["errors"] == {"base": "invalid_agent_id"}


@pytest.mark.parametrize(
    ("key", "expected"), [(KEY, None), ("z" * 40, "invalid_key")]
)
async def test_probe_verifies_the_signature(hass, agent, key, expected):
    task = hass.async_create_task(config_flow._async_probe_agent(hass, AGENT, KEY))  # noqa: SLF001
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    agent.fire("info", "info", {"protocol": 1}, key=key)
    assert await task == expected


def device_with(hass, entry, identifier):
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    return next(d for d in devices if (DOMAIN, identifier) in d.identifiers)


async def test_entities_are_created(hass, agent):
    entry = await setup_entry(hass, agent)

    state = hass.states.get(entity_id(hass, "lab_cluster_state", "sensor"))
    assert state.state == "on"
    cpu = hass.states.get(entity_id(hass, "lab_hosts_esxi1_host_cpu_percent", "sensor"))
    assert float(cpu.state) == 50.0
    assert cpu.attributes["state_class"] == "measurement"
    used = hass.states.get(entity_id(hass, "lab_datastores_ds1_datastore_used_percent", "sensor"))
    assert float(used.state) == 75.0
    assert hass.states.get(entity_id(hass, "lab_vms_running", "sensor")).state == "2"
    assert hass.states.get(entity_id(hass, "lab_vms_total", "sensor")).state == "3"
    assert device_with(hass, entry, "lab:hosts:esxi1").manufacturer == "Supermicro"


async def test_infrastructure_vms_have_no_power_controls(hass, agent):
    await setup_entry(hass, agent)
    registry = er.async_get(hass)
    assert registry.async_get_entity_id("switch", DOMAIN, "lab_vms_vm-1_power")
    assert registry.async_get_entity_id("switch", DOMAIN, "lab_vms_vm-3_power") is None
    assert registry.async_get_entity_id("switch", DOMAIN, "lab_vms_vm-4_power") is None
    assert registry.async_get_entity_id("button", DOMAIN, "lab_vms_vm-3_force_off") is None


async def test_new_vm_appears_without_reload(hass, agent):
    await setup_entry(hass, agent)
    document = state_doc()
    document["vms"].append({**document["vms"][0], "id": "vm-9", "name": "new"})
    agent.fire("state", "state", document)
    await hass.async_block_till_done()
    assert er.async_get(hass).async_get_entity_id("switch", DOMAIN, "lab_vms_vm-9_power")


async def test_forged_state_is_ignored(hass, agent):
    await setup_entry(hass, agent)
    document = state_doc()
    document["cluster"] = "off"
    agent.fire("state", "state", document, key="z" * 40)
    await hass.async_block_till_done()
    assert hass.states.get(entity_id(hass, "lab_cluster_state", "sensor")).state == "on"


async def test_older_retained_state_does_not_replace_newer(hass, agent):
    await setup_entry(hass, agent)
    document = state_doc()
    document["cluster"] = "off"
    agent.fire("state", "state", document, ts=1000)
    await hass.async_block_till_done()
    assert hass.states.get(entity_id(hass, "lab_cluster_state", "sensor")).state == "on"


async def test_agent_offline_makes_entities_unavailable(hass, agent):
    from pytest_homeassistant_custom_component.common import async_fire_mqtt_message

    await setup_entry(hass, agent)
    async_fire_mqtt_message(hass, topic(AGENT, "status"), "offline")
    await hass.async_block_till_done()

    assert hass.states.get(entity_id(hass, "lab_cluster_state", "sensor")).state == "unavailable"
    assert hass.states.get(entity_id(hass, "lab_agent_online", "binary_sensor")).state == "off"


async def test_start_asks_the_agent_for_its_status(hass, agent):
    await setup_entry(hass, agent)
    assert [c.kind for c in agent.all_commands()] == ["refresh"]


def _state_message(retain):
    target = topic(AGENT, "state")
    return target, seal(KEY, target, "state", state_doc()), retain


async def test_live_state_proves_the_agent_is_online(hass, agent):
    from pytest_homeassistant_custom_component.common import async_fire_mqtt_message

    entry = await setup_entry(hass, agent)
    link = entry.runtime_data
    link.online = False

    target, payload, _ = _state_message(False)
    async_fire_mqtt_message(hass, target, payload)
    await hass.async_block_till_done()

    assert link.online


async def test_retained_state_does_not_prove_the_agent_is_online(hass, agent):
    from pytest_homeassistant_custom_component.common import async_fire_mqtt_message

    entry = await setup_entry(hass, agent)
    link = entry.runtime_data
    link.online = False

    target, payload, _ = _state_message(True)
    async_fire_mqtt_message(hass, target, payload, retain=True)
    await hass.async_block_till_done()

    assert not link.online


async def test_agent_version_is_shown_on_the_device(hass, agent):
    entry = await setup_entry(hass, agent)
    assert device_with(hass, entry, AGENT).sw_version == "1.0"


async def test_vm_switch_sends_signed_commands(hass, agent):
    await setup_entry(hass, agent)
    switch = entity_id(hass, "lab_vms_vm-1_power", "switch")

    await hass.services.async_call("switch", "turn_off", {"entity_id": switch}, blocking=True)
    await hass.services.async_call("switch", "turn_on", {"entity_id": switch}, blocking=True)

    commands = agent.commands()
    assert [c.body["action"] for c in commands] == ["shutdown", "on"]
    assert all(c.exp is not None for c in commands)


async def test_agent_error_is_translated(hass, agent):
    await setup_entry(hass, agent)
    agent.answers["vm_power"] = {"ok": False, "code": "tools_unavailable"}
    switch = entity_id(hass, "lab_vms_vm-1_power", "switch")

    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call("switch", "turn_off", {"entity_id": switch}, blocking=True)
    assert err.value.translation_key == "agent_tools_unavailable"


async def test_command_refused_while_agent_offline(hass, agent):
    from pytest_homeassistant_custom_component.common import async_fire_mqtt_message

    entry = await setup_entry(hass, agent)
    async_fire_mqtt_message(hass, topic(AGENT, "status"), "offline")
    await hass.async_block_till_done()
    with pytest.raises(HomeAssistantError) as err:
        await entry.runtime_data.async_start_cluster()
    assert err.value.translation_key == "agent_offline"
    assert agent.commands() == []


async def test_shutdown_button_needs_arming(hass, agent):
    await setup_entry(hass, agent)
    button = entity_id(hass, "lab_cluster_shutdown", "button")
    armed = entity_id(hass, "lab_shutdown_armed", "switch")

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call("button", "press", {"entity_id": button}, blocking=True)
    assert agent.commands() == []

    await hass.services.async_call("switch", "turn_on", {"entity_id": armed}, blocking=True)
    assert hass.states.get(armed).state == "on"
    await hass.services.async_call("button", "press", {"entity_id": button}, blocking=True)
    await hass.async_block_till_done()

    assert [c.kind for c in agent.commands()] == ["cluster_shutdown"]
    assert agent.commands()[0].body == {"dry_run": False}
    assert hass.states.get(armed).state == "off"


async def test_arming_expires(hass, agent):
    from datetime import timedelta

    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    await setup_entry(hass, agent)
    armed = entity_id(hass, "lab_shutdown_armed", "switch")
    await hass.services.async_call("switch", "turn_on", {"entity_id": armed}, blocking=True)

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
    await hass.async_block_till_done()

    assert hass.states.get(armed).state == "off"


async def test_preview_does_not_need_arming(hass, agent):
    await setup_entry(hass, agent)
    button = entity_id(hass, "lab_shutdown_preview", "button")
    await hass.services.async_call("button", "press", {"entity_id": button}, blocking=True)
    assert agent.commands()[0].body == {"dry_run": True}


async def test_shutdown_action_requires_confirm(hass, agent):
    await setup_entry(hass, agent)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "shutdown_cluster", {}, blocking=True)
    assert agent.commands() == []

    agent.answers["cluster_shutdown"] = {"operation_id": "op1"}
    response = await hass.services.async_call(
        DOMAIN, "shutdown_cluster", {"confirm": True}, blocking=True, return_response=True
    )
    assert response == {"operation_id": "op1"}


async def test_operation_sensors(hass, agent):
    await setup_entry(hass, agent)
    agent.fire(
        "operation",
        "operation",
        {"status": "running", "step": "wake_hosts", "kind": "cluster_start", "detail": "esxi1"},
    )
    await hass.async_block_till_done()

    operation = hass.states.get(entity_id(hass, "lab_operation", "sensor"))
    assert operation.state == "running"
    assert operation.attributes["detail"] == "esxi1"
    assert hass.states.get(entity_id(hass, "lab_operation_step", "sensor")).state == "wake_hosts"


async def test_scenario_entities_and_commands(hass, agent):
    entry = await setup_entry(hass, agent, scenario_subentry())
    subentry_id = next(iter(entry.subentries))
    start = entity_id(hass, f"lab_{subentry_id}_scenario_start", "button")
    state_id = entity_id(hass, f"lab_{subentry_id}_state", "sensor")
    assert hass.states.get(state_id).state == "inactive"

    await hass.services.async_call("button", "press", {"entity_id": start}, blocking=True)

    command = agent.commands()[0]
    assert command.kind == "scenario_start"
    assert command.body["scenario"]["id"] == subentry_id
    assert command.body["scenario"]["steps"] == SCENARIO_DATA["steps"]

    agent.fire(
        "operation",
        "operation",
        {"status": "running", "step": "start_vms", "kind": "scenario_start", "scenario": subentry_id},
    )
    await hass.async_block_till_done()
    assert hass.states.get(state_id).state == "starting"

    agent.fire("operation", "operation", {"status": "completed", "scenario": subentry_id, "kind": "scenario_start"})
    document = state_doc()
    document["scenarios"] = {subentry_id: {"name": "Test VDI", "vms_on": 1, "vms_total": 1}}
    agent.fire("state", "state", document)
    await hass.async_block_till_done()
    assert hass.states.get(state_id).state == "active"

    assert device_with(hass, entry, f"lab:scenario:{subentry_id}") is not None


async def test_scenario_action_by_name(hass, agent):
    await setup_entry(hass, agent, scenario_subentry())
    await hass.services.async_call(DOMAIN, "start_scenario", {"scenario": "Test VDI"}, blocking=True)
    await hass.services.async_call(DOMAIN, "stop_scenario", {"scenario": "Test VDI"}, blocking=True)
    assert [c.kind for c in agent.commands()] == ["scenario_start", "scenario_stop"]

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "start_scenario", {"scenario": "Nope"}, blocking=True)


async def test_scenario_wizard_creates_and_reloads(hass, agent):
    entry = await setup_entry(hass, agent)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "scenario"), context={"source": SOURCE_USER}
    )
    assert result["step_id"] == "user"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": "VDI", "power_off_empty_hosts": True}
    )
    assert result["step_id"] == "group"
    offered = [o["value"] for o in result["data_schema"].schema["vms"].config["options"]]
    assert offered == ["vm-1", "vm-2"]

    group = {"vms": ["vm-1"], "wait": "tools", "delay": 5, "timeout": 100, "add_another": True}
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], group)
    assert result["step_id"] == "group"
    assert result["description_placeholders"] == {"number": "2"}
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {**group, "vms": ["vm-2"], "wait": "power", "add_another": False}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    subentry = next(iter(entry.subentries.values()))
    assert subentry.title == "VDI"
    assert [s["vms"] for s in subentry.data["steps"]] == [["vm-1"], ["vm-2"]]
    assert "add_another" not in subentry.data["steps"][0]
    assert entry.state is ConfigEntryState.LOADED
    assert er.async_get(hass).async_get_entity_id(
        "button", DOMAIN, f"lab_{subentry.subentry_id}_scenario_start"
    )


async def test_scenario_wizard_needs_a_vm(hass, agent):
    entry = await setup_entry(hass, agent)
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "scenario"), context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": "VDI", "power_off_empty_hosts": False}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {"vms": [], "wait": "tools", "delay": 0, "timeout": 100, "add_another": False},
    )
    assert result["errors"] == {"vms": "no_vms_selected"}


async def test_scenario_reconfigure(hass, agent):
    entry = await setup_entry(hass, agent, scenario_subentry())
    subentry_id = next(iter(entry.subentries))
    result = await entry.start_subentry_reconfigure_flow(hass, subentry_id)
    assert result["step_id"] == "reconfigure"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": "Renamed", "power_off_empty_hosts": False}
    )
    assert result["step_id"] == "group"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {"vms": ["vm-2"], "wait": "none", "delay": 0, "timeout": 100, "add_another": False},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert entry.subentries[subentry_id].title == "Renamed"
    assert entry.subentries[subentry_id].data["steps"][0]["vms"] == ["vm-2"]
    assert entry.state is ConfigEntryState.LOADED


async def test_unload(hass, agent):
    entry = await setup_entry(hass, agent)
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_diagnostics_hide_the_key(hass, agent):
    from custom_components.ha_hypervisor.diagnostics import async_get_config_entry_diagnostics

    entry = await setup_entry(hass, agent)
    data = await async_get_config_entry_diagnostics(hass, entry)
    assert KEY not in json.dumps(data)


def test_seal_is_importable_without_homeassistant_state():
    assert seal(KEY, "t", "k", {})


async def test_cluster_not_configured_state(hass, agent):
    await setup_entry(hass, agent)
    document = state_doc()
    document["cluster"] = "not_configured"
    document["hosts"] = []
    document["vms"] = []
    agent.fire("state", "state", document)
    await hass.async_block_till_done()
    cluster = hass.states.get(entity_id(hass, "lab_cluster_state", "sensor"))
    assert cluster.state == "not_configured"
