"""An agent that drives Proxmox VE: names and devices follow the platform."""

from __future__ import annotations

from homeassistant.helpers import device_registry as dr, entity_registry as er

from conftest import AGENT, make_entry, state_doc
from custom_components.ha_hypervisor.const import DOMAIN


def proxmox_doc() -> dict:
    document = state_doc()
    document["platform"] = "proxmox"
    document["source"] = "proxmox"
    document["vcenter"] = {"reachable": True, "version": "Proxmox VE 9.0.10"}
    for host in document["hosts"]:
        host.update(vendor="Proxmox", model="Intel(R) Xeon(R) D-1528")
    for vm in document["vms"]:
        vm["id"] = f"qemu-{vm['id'].split('-')[1]}"
    return document


async def _setup(hass, agent):
    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    agent.online()
    agent.fire("state", "state", proxmox_doc())
    await hass.async_block_till_done()
    return entry


async def test_proxmox_api_sensor_replaces_vcenter(hass, agent):
    await _setup(hass, agent)
    registry = er.async_get(hass)
    api = registry.async_get_entity_id("binary_sensor", DOMAIN, f"{AGENT}_proxmox_reachable")
    assert api is not None
    assert hass.states.get(api).state == "on"
    assert hass.states.get(api).attributes["friendly_name"].endswith("Proxmox API")


async def test_proxmox_devices_and_guest_agent(hass, agent):
    entry = await _setup(hass, agent)
    devices = dr.async_get(hass)
    vm = next(
        device
        for device in dr.async_entries_for_config_entry(devices, entry.entry_id)
        if (DOMAIN, f"{AGENT}:vms:qemu-1") in device.identifiers
    )
    assert vm is not None
    assert vm.manufacturer == "Proxmox"
    registry = er.async_get(hass)
    tools = registry.async_get_entity_id("binary_sensor", DOMAIN, f"{AGENT}_vms_qemu-1_tools")
    assert hass.states.get(tools).attributes["friendly_name"] == "web Guest agent"
    switch = registry.async_get_entity_id("switch", DOMAIN, f"{AGENT}_vms_qemu-1_power")
    assert switch is not None
    assert entry.title
