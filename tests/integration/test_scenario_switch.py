"""A scenario as a switch, its usage sensors and its VMs in the panel."""

from __future__ import annotations

from types import MappingProxyType

from homeassistant.config_entries import ConfigSubentryData
from homeassistant.helpers import entity_registry as er

from conftest import AGENT, setup_entry, state_doc
from custom_components.ha_hypervisor.const import DOMAIN

SCENARIO = {
    "power_off_empty_hosts": False,
    "steps": [
        {"vms": ["vm-2"], "wait": "power", "delay": 0, "timeout": 100},
        {"vms": ["vm-1", "vm-2"], "wait": "tools", "delay": 0, "timeout": 100},
    ],
}


def subentry():
    return [
        ConfigSubentryData(
            data=MappingProxyType(SCENARIO), subentry_type="scenario", title="Lab", unique_id=None
        )
    ]


def entity(hass, domain, unique_id):
    found = er.async_get(hass).async_get_entity_id(domain, DOMAIN, unique_id)
    assert found, unique_id
    return found


async def test_switch_starts_and_stops_and_follows_the_state(hass, agent):
    entry = await setup_entry(hass, agent, subentry())
    sid = next(iter(entry.subentries))
    switch = entity(hass, "switch", f"{AGENT}_{sid}_power")
    assert hass.states.get(switch).state == "off"

    await hass.services.async_call("switch", "turn_on", {"entity_id": switch}, blocking=True)
    command = agent.commands()[-1]
    assert command.kind == "scenario_start"
    assert command.body["scenario"]["id"] == sid

    agent.fire("operation", "operation", {"status": "running", "step": "start_vms", "kind": "scenario_start", "scenario": sid})
    await hass.async_block_till_done()
    state_sensor = entity(hass, "sensor", f"{AGENT}_{sid}_state")
    assert hass.states.get(state_sensor).state == "starting"
    assert hass.states.get(switch).state == "on"  # starting counts as on

    agent.fire("operation", "operation", {"status": "completed", "kind": "scenario_start", "scenario": sid})
    document = state_doc()
    document["scenarios"] = {sid: {"name": "Lab", "vms_on": 1, "vms_total": 2}}
    agent.fire("state", "state", document)
    await hass.async_block_till_done()
    assert hass.states.get(switch).state == "on"  # partial is on too

    await hass.services.async_call("switch", "toggle", {"entity_id": switch}, blocking=True)
    assert agent.commands()[-1].kind == "scenario_stop"


async def test_usage_sums_the_vms_of_the_scenario(hass, agent):
    entry = await setup_entry(hass, agent, subentry())
    sid = next(iter(entry.subentries))
    document = state_doc()
    vms = {vm["id"]: vm for vm in document["vms"]}
    vms["vm-1"].update(cpu_mhz=300, mem_mb=1024, storage_gb=10.5)
    vms["vm-2"].update(cpu_mhz=0, mem_mb=0, storage_gb=20.25)
    agent.fire("state", "state", document)
    await hass.async_block_till_done()

    cpu = hass.states.get(entity(hass, "sensor", f"{AGENT}_{sid}_scenario_cpu"))
    assert cpu.state == "300"
    assert cpu.attributes["vms"] == {"db": 0, "web": 300}
    assert hass.states.get(entity(hass, "sensor", f"{AGENT}_{sid}_scenario_memory")).state == "1024"
    assert float(hass.states.get(entity(hass, "sensor", f"{AGENT}_{sid}_scenario_storage")).state) == 30.75


async def test_panel_lists_the_scenario_vms_in_start_order(hass, agent, hass_ws_client):
    entry = await setup_entry(hass, agent, subentry())
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": f"{DOMAIN}/dashboard"})
    (lab,) = (await client.receive_json())["result"]["agents"]
    (scenario,) = lab["scenarios"]
    db = entity(hass, "switch", f"{AGENT}_vms_vm-2_power")
    web = entity(hass, "switch", f"{AGENT}_vms_vm-1_power")
    assert scenario["vms"] == [db, web]
    assert scenario["entities"]["scenario_power"].startswith("switch.")
    assert scenario["entities"]["scenario_cpu"].startswith("sensor.")
    assert entry.entry_id == lab["entry_id"]


async def test_vm_switch_carries_usage_for_the_panel(hass, agent):
    await setup_entry(hass, agent)
    document = state_doc()
    vms = {vm["id"]: vm for vm in document["vms"]}
    vms["vm-1"].update(cpu_mhz=300, mem_mb=1024, mem_total_mb=4096, storage_gb=10.5)
    agent.fire("state", "state", document)
    await hass.async_block_till_done()
    web = hass.states.get(entity(hass, "switch", f"{AGENT}_vms_vm-1_power"))
    assert web.attributes["cpu"] == "CPU 300 MHz"
    assert web.attributes["memory"] == "RAM 1.0 / 4.0 GiB"
    assert web.attributes["disk"] == "10.5 GiB"
    db = hass.states.get(entity(hass, "switch", f"{AGENT}_vms_vm-2_power"))
    assert "cpu" not in db.attributes and "disk" in db.attributes  # off: only the disk


async def test_panel_lists_scenario_members_with_names(hass, agent, hass_ws_client):
    await setup_entry(hass, agent, subentry())
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": f"{DOMAIN}/dashboard"})
    (lab,) = (await client.receive_json())["result"]["agents"]
    (scenario,) = lab["scenarios"]
    assert [(m["id"], m["name"]) for m in scenario["members"]] == [("vm-2", "db"), ("vm-1", "web")]
    assert scenario["members"][0]["power"] == entity(hass, "switch", f"{AGENT}_vms_vm-2_power")


async def test_vm_switch_numbers_and_decimal_comma(hass, agent):
    hass.config.language = "pl"
    await setup_entry(hass, agent)
    document = state_doc()
    vms = {vm["id"]: vm for vm in document["vms"]}
    vms["vm-1"].update(cpu_mhz=300, mem_mb=1536, mem_total_mb=4096, storage_gb=15.0)
    agent.fire("state", "state", document)
    await hass.async_block_till_done()
    web = hass.states.get(entity(hass, "switch", f"{AGENT}_vms_vm-1_power"))
    assert web.attributes["memory"] == "RAM 1,5 / 4,0 GiB"
    assert web.attributes["disk"] == "15,0 GiB"
    assert web.attributes["mem_mb"] == 1536 and web.attributes["storage_gb"] == 15.0
    assert web.attributes["host"] == "esxi1"  # the panel groups the VMs by server
