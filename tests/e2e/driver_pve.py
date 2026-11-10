"""Second agent: a simulated Proxmox VE 9 node over HTTPS, next to the vSphere one."""

from __future__ import annotations

# The driver runs as __main__: its logged-in session lives there, not in a
# second copy imported as "driver".
from __main__ import api, call, flow, state, states, step, wait  # type: ignore[attr-defined]

AGENT = "e2epve"
KEY = "e2e-proxmox-key-e2e-proxmox-key-e2e-proxmox"
PREFIX = "hypervisor_e2epve"


def operation_done() -> bool:
    return state(f"sensor.{PREFIX}_operation") in ("completed", "failed")


def last_operation() -> dict:
    return states()[f"sensor.{PREFIX}_operation"]


def run_operation(what: str, service: str, timeout: float, **data) -> dict:
    before = last_operation()["attributes"].get("started")
    call("ha_hypervisor", service, config_entry_id=ENTRY["id"], **data)
    wait(
        what,
        lambda: last_operation()["attributes"].get("started") != before and operation_done(),
        timeout,
    )
    operation = last_operation()
    print(f"   {operation['state']}: plan={operation['attributes'].get('plan')}", flush=True)
    assert operation["state"] == "completed", operation
    return operation


ENTRY: dict[str, str] = {}


def proxmox() -> None:
    step("Proxmox agent: config flow")
    result = flow("/api/config/config_entries/flow", "ha_hypervisor", {"agent_id": AGENT, "shared_key": KEY})
    assert result["type"] == "create_entry", result
    ENTRY["id"] = result["result"]["entry_id"]

    step("Proxmox entities")
    wait("Proxmox cluster on", lambda: state(f"sensor.{PREFIX}_cluster") == "on", 60)
    all_states = states()
    api_sensor = f"binary_sensor.{PREFIX}_proxmox_api"
    assert all_states[api_sensor]["state"] == "on", sorted(all_states)
    assert f"binary_sensor.{PREFIX}_vcenter" not in all_states
    guests = ["ad01", "files01", "media01", "pihole"]
    for name in guests:
        assert f"switch.{name}_power" in all_states, sorted(all_states)
    assert "switch.debian_template_power" not in all_states
    assert all_states["binary_sensor.ad01_guest_agent"]["state"] == "on"
    assert all_states["sensor.ad01_ip_address"]["state"] == "10.0.1.100"
    assert all_states["sensor.pve_cpu_usage"]["state"] not in ("unknown", "unavailable")

    step("Proxmox VM power")
    call("switch", "turn_on", entity_id="switch.media01_power")
    wait("media01 on", lambda: state("switch.media01_power") == "on", 30)
    # No guest agent in media01: a shutdown goes through ACPI.
    call("switch", "turn_off", entity_id="switch.media01_power")
    wait("media01 off", lambda: state("switch.media01_power") == "off", 30)

    step("Proxmox shutdown preview")
    operation = run_operation("preview", "shutdown_cluster", 60, confirm=True, dry_run=True)
    plan = operation["attributes"]["plan"]
    assert plan["vms"] == ["ad01", "files01", "pihole"], plan
    assert plan["hosts"] == ["pve"], plan
    assert "wake_unverified:pve" in plan["warnings"], plan

    step("Proxmox node shutdown, guests first")
    run_operation("shutdown", "shutdown_cluster", 120, confirm=True)
    wait("cluster off", lambda: state(f"sensor.{PREFIX}_cluster") == "off", 60)
    for name in guests:
        assert state(f"switch.{name}_power") == "off", name

    step("Proxmox start through Wake on LAN")
    run_operation("start", "start_cluster", 120)
    wait("cluster on again", lambda: state(f"sensor.{PREFIX}_cluster") == "on", 60)

    step("Proxmox scenario")
    path = "/api/config/config_entries/subentries/flow"
    start = api("POST", path, json={"handler": [ENTRY["id"], "scenario"]})
    group = api("POST", f"{path}/{start['flow_id']}", json={"name": "Homelab", "power_off_empty_hosts": False})
    options = next(f for f in group["data_schema"] if f["name"] == "vms")["selector"]["select"]["options"]
    values = {option["label"]: option["value"] for option in options}
    assert "debian-template" not in values, values
    done = api(
        "POST",
        f"{path}/{group['flow_id']}",
        json={"vms": [values["ad01"], values["pihole"]], "wait": "tools", "delay": 0, "timeout": 60, "add_another": False},
    )
    assert done["type"] == "create_entry", done
    wait("scenario sensor", lambda: state("sensor.homelab_state") == "inactive", 30)
    call("ha_hypervisor", "start_scenario", scenario="Homelab", config_entry_id=ENTRY["id"])
    wait("scenario active", lambda: state("sensor.homelab_state") == "active", 90)
    assert state("switch.ad01_power") == "on" and state("switch.pihole_power") == "on"
    assert state("switch.files01_power") == "off"

