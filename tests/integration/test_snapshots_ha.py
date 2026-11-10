"""Snapshot actions, the VM snapshot sensor and the scenario option."""

from __future__ import annotations

from homeassistant.config_entries import SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er
import pytest

from conftest import AGENT, setup_entry, state_doc
from custom_components.ha_hypervisor.const import DOMAIN


def vm_device(hass, entry, vm_id):
    return next(
        device
        for device in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
        if (DOMAIN, f"{AGENT}:vms:{vm_id}") in device.identifiers
    )


async def test_snapshot_sensor_lists_the_names(hass, agent):
    document = state_doc()
    document["vms"][0]["snapshots"] = ["Base", "Demo"]
    await setup_entry(hass, agent)
    agent.fire("state", "state", document)
    await hass.async_block_till_done()
    sensor = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{AGENT}_vms_vm-1_snapshots")
    state = hass.states.get(sensor)
    assert state.state == "2"
    assert state.attributes["snapshots"] == ["Base", "Demo"]
    # vCLS VMs get none.
    assert er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{AGENT}_vms_vm-4_snapshots") is None


async def test_snapshot_actions_by_device_and_entity(hass, agent):
    entry = await setup_entry(hass, agent)
    agent.answers["snapshot_create"] = {"operation_id": "op-create"}
    web = vm_device(hass, entry, "vm-1")
    db_switch = er.async_get(hass).async_get_entity_id("switch", DOMAIN, f"{AGENT}_vms_vm-2_power")

    response = await hass.services.async_call(
        DOMAIN,
        "create_snapshot",
        {"device_id": web.id, "entity_id": db_switch, "name": "Demo", "memory": True},
        blocking=True,
        return_response=True,
    )
    assert response == {"operations": {entry.entry_id: "op-create"}}
    command = agent.commands()[-1]
    assert command.kind == "snapshot_create"
    assert command.body == {"vms": ["vm-1", "vm-2"], "name": "Demo", "description": "", "memory": True}

    await hass.services.async_call(
        DOMAIN, "revert_snapshot", {"device_id": web.id, "name": "Demo", "start": True}, blocking=True
    )
    assert agent.commands()[-1].body == {"vms": ["vm-1"], "name": "Demo", "start": True}

    await hass.services.async_call(DOMAIN, "delete_snapshot", {"device_id": web.id, "name": "Demo"}, blocking=True)
    assert agent.commands()[-1].kind == "snapshot_delete"


async def test_snapshot_action_checks_name_and_selection(hass, agent):
    entry = await setup_entry(hass, agent)
    web = vm_device(hass, entry, "vm-1")
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "create_snapshot", {"device_id": web.id, "name": "bad name"}, blocking=True
        )
    agent_device = next(
        device
        for device in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
        if (DOMAIN, AGENT) in device.identifiers
    )
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "create_snapshot", {"device_id": agent_device.id, "name": "Demo"}, blocking=True
        )
    assert [c.kind for c in agent.commands()] == []


async def test_agent_refusal_is_translated(hass, agent):
    entry = await setup_entry(hass, agent)
    agent.answers["snapshot_revert"] = {"ok": False, "code": "snapshot_not_found"}
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            DOMAIN, "revert_snapshot", {"device_id": vm_device(hass, entry, "vm-1").id, "name": "Demo"},
            blocking=True,
        )
    assert err.value.translation_key == "agent_snapshot_not_found"


async def test_scenario_wizard_keeps_the_snapshot(hass, agent):
    entry = await setup_entry(hass, agent)
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "scenario"), context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": "Demo lab", "power_off_empty_hosts": False, "snapshot": "no good"}
    )
    assert result["errors"] == {"snapshot": "invalid_snapshot_name"}
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": "Demo lab", "power_off_empty_hosts": False, "snapshot": "Clean"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {"vms": ["vm-1", "vm-2"], "wait": "power", "delay": 0, "timeout": 100, "add_another": False},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    subentry = next(iter(entry.subentries.values()))
    assert subentry.data["snapshot"] == "Clean"
    # The entry reloaded with the new scenario; the agent answers the refresh.
    agent.online()
    agent.fire("state", "state", state_doc())
    await hass.async_block_till_done()

    await hass.services.async_call(DOMAIN, "start_scenario", {"scenario": "Demo lab"}, blocking=True)
    assert agent.commands()[-1].body["scenario"]["snapshot"] == "Clean"
