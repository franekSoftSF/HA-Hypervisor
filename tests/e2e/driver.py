"""Sets up a fresh Home Assistant over its API and exercises the integration."""

from __future__ import annotations

import sys
import time
from typing import Any

import requests

BASE = "http://homeassistant:8123"
CLIENT = f"{BASE}/"
AGENT = "e2e"
KEY = "e2e-shared-key-e2e-shared-key-e2e-shared-key"
session = requests.Session()


def step(text: str) -> None:
    print(f"== {text}", flush=True)


def api(method: str, path: str, **kwargs: Any) -> Any:
    response = session.request(method, f"{BASE}{path}", timeout=60, **kwargs)
    if response.status_code >= 400:
        raise RuntimeError(f"{method} {path}: {response.status_code} {response.text}")
    return response.json() if response.content else None


def wait(what: str, check, timeout: float = 90) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        value = check()
        if value:
            print(f"   ok: {what}", flush=True)
            return value
        if time.monotonic() > deadline:
            raise RuntimeError(f"timed out waiting for {what}")
        time.sleep(1)


def onboard() -> None:
    step("onboarding")
    wait("onboarding API", _onboarding_ready)
    created = requests.post(
        f"{BASE}/api/onboarding/users",
        json={
            "client_id": CLIENT,
            "name": "E2E",
            "username": "e2e",
            "password": "e2e-test-only",
            "language": "en",
        },
        timeout=30,
    ).json()
    token = requests.post(
        f"{BASE}/auth/token",
        data={"grant_type": "authorization_code", "code": created["auth_code"], "client_id": CLIENT},
        timeout=30,
    ).json()
    session.headers["Authorization"] = f"Bearer {token['access_token']}"


def _onboarding_ready() -> bool:
    try:
        return requests.get(f"{BASE}/api/onboarding", timeout=5).status_code == 200
    except requests.RequestException:
        return False


def flow(path: str, handler: Any, *answers: dict[str, Any]) -> dict[str, Any]:
    result = api("POST", path, json={"handler": handler, "show_advanced_options": False})
    for answer in answers:
        assert result["type"] == "form", result
        result = api("POST", f"{path}/{result['flow_id']}", json=answer)
    return result


def states() -> dict[str, dict[str, Any]]:
    return {item["entity_id"]: item for item in api("GET", "/api/states")}


def state(entity_id: str) -> str | None:
    return states().get(entity_id, {}).get("state")


def call(domain: str, service: str, **data: Any) -> Any:
    return api("POST", f"/api/services/{domain}/{service}", json=data)


def main() -> None:
    onboard()

    step("MQTT integration")
    result = flow(
        "/api/config/config_entries/flow",
        "mqtt",
        {
            "broker": "mosquitto",
            "port": 1883,
            "other_settings": {"set_client_cert": False, "set_ca_cert": "off"},
        },
    )
    assert result["type"] == "create_entry", result

    step("HA VMware config flow: wrong key is refused")
    result = flow(
        "/api/config/config_entries/flow",
        "ha_vmware",
        {"agent_id": AGENT, "shared_key": "x" * 40},
    )
    assert result["errors"] == {"base": "invalid_key"}, result
    api("DELETE", f"/api/config/config_entries/flow/{result['flow_id']}")

    step("HA VMware config flow")
    result = flow(
        "/api/config/config_entries/flow",
        "ha_vmware",
        {"agent_id": AGENT, "shared_key": KEY},
    )
    assert result["type"] == "create_entry", result
    entry_id = result["result"]["entry_id"]

    step("entities")
    cluster = wait(
        "cluster sensor with data",
        lambda: next(
            (
                e
                for e, s in states().items()
                if e == "sensor.vmware_e2e_cluster" and s["state"] in ("on", "degraded")
            ),
            None,
        ),
    )
    print(f"   {cluster} = {state(cluster)}")
    all_states = states()
    switches = sorted(e for e in all_states if e.startswith("switch.dc0_c0_rp0_vm"))
    print(f"   VM switches: {switches}")
    assert len(switches) == 3, sorted(all_states)
    datastores = sorted(e for e in all_states if e.endswith("_used_space"))
    print(f"   datastores: {datastores}")
    assert datastores, sorted(all_states)
    host = sorted(e for e in all_states if e.endswith("_cpu_usage") and "dc0_c0_h0" in e)
    assert host, sorted(all_states)

    step("VM power through the switch")
    switch = switches[0]
    assert state(switch) == "on"
    call("switch", "turn_off", entity_id=switch)
    wait(f"{switch} off", lambda: state(switch) == "off", 30)
    call("switch", "turn_on", entity_id=switch)
    wait(f"{switch} on", lambda: state(switch) == "on", 30)

    step("force off button")
    button = switch.replace("switch.", "button.").replace("_power", "_force_power_off")
    call("button", "press", entity_id=button)
    wait(f"{switch} off after force", lambda: state(switch) == "off", 30)
    call("switch", "turn_on", entity_id=switch)
    wait(f"{switch} on again", lambda: state(switch) == "on", 30)

    step("shutdown button is locked until armed")
    response = session.post(
        f"{BASE}/api/services/button/press",
        json={"entity_id": "button.vmware_e2e_shut_down_cluster"},
        timeout=30,
    )
    assert response.status_code == 400, response.text
    assert state("sensor.vmware_e2e_operation") != "running"

    step("shutdown preview")
    call("button", "press", entity_id="button.vmware_e2e_preview_shutdown")
    wait(
        "operation finished",
        lambda: state("sensor.vmware_e2e_operation") in ("completed", "failed"),
        30,
    )
    operation = states()["sensor.vmware_e2e_operation"]
    print(f"   {operation['state']}: {operation['attributes']}")
    assert operation["state"] == "completed", operation
    plan = operation["attributes"]["plan"]
    assert len(plan["vms"]) == 3 and plan["hosts"] == ["DC0_C0_H0"], plan
    assert "wake_unverified:DC0_C0_H0" in plan["warnings"], plan
    assert all(state(s) == "on" for s in switches), "preview must change nothing"

    step("scenario wizard")
    path = "/api/config/config_entries/subentries/flow"
    start = api("POST", path, json={"handler": [entry_id, "scenario"]})
    assert start["step_id"] == "user", start
    group = api(
        "POST",
        f"{path}/{start['flow_id']}",
        json={"name": "Test", "power_off_empty_hosts": False},
    )
    assert group["step_id"] == "group", group
    options = next(f for f in group["data_schema"] if f["name"] == "vms")["selector"]["select"]["options"]
    assert len(options) == 3, options
    vms = [option["value"] for option in options]
    second = api(
        "POST",
        f"{path}/{group['flow_id']}",
        json={"vms": vms[:1], "wait": "power", "delay": 1, "timeout": 30, "add_another": True},
    )
    done = api(
        "POST",
        f"{path}/{second['flow_id']}",
        json={"vms": vms[1:], "wait": "power", "delay": 0, "timeout": 30, "add_another": False},
    )
    assert done["type"] == "create_entry", done
    wait("scenario sensor", lambda: state("sensor.test_state") == "inactive", 30)

    step("power everything off, then run the scenario")
    for switch_id in switches:
        call("button", "press", entity_id=switch_id.replace("switch.", "button.").replace("_power", "_force_power_off"))
    wait("all VMs off", lambda: all(state(s) == "off" for s in switches), 30)
    call("ha_vmware", "start_scenario", scenario="Test")
    wait("scenario active", lambda: state("sensor.test_state") == "active", 60)
    assert all(state(s) == "on" for s in switches)

    step("stop scenario")
    call("button", "press", entity_id="button.test_stop")
    wait("all VMs off", lambda: all(state(s) == "off" for s in switches), 60)
    wait("scenario inactive", lambda: state("sensor.test_state") == "inactive", 30)

    step("action without confirm is refused")
    response = session.post(
        f"{BASE}/api/services/ha_vmware/shutdown_cluster", json={}, timeout=30
    )
    assert response.status_code == 400, response.text

    step("real vSphere API calls that the mocks stand in for")
    from ha_vmware_agent.vim import VimClient, VimError  # noqa: PLC0415

    esxi = VimClient.connect("esxi", 443, "user", "pass", False)
    data = esxi.collect("configured-name")
    assert [h["id"] for h in data["hosts"]] == ["configured-name"], data["hosts"]
    vm = data["vms"][0]
    assert vm["host"] == "configured-name", vm
    assert esxi.vm_status(vm["id"])["power"] == "on"
    esxi.power_off(vm["id"])
    assert esxi.vm_status(vm["id"])["power"] == "off"
    esxi.power_on(vm["id"])
    vcenter = VimClient.connect("vcsim", 443, "user", "pass", False)
    cluster_id = vcenter.collect()["clusters"][0]["id"]
    vcenter.set_vcls_enabled(cluster_id, False)
    vcenter.set_vcls_enabled(cluster_id, True)
    try:
        esxi.shutdown_host()
    except VimError as err:
        # vcsim has no such method: the call is well formed, the effect is not tested.
        assert "ShutdownHost_Task" in str(err), err
    print("   ok: collect, power, vCLS option, host shutdown request")

    step("reload keeps entities")
    api("POST", f"/api/config/config_entries/entry/{entry_id}/reload")
    wait("cluster sensor back", lambda: state(cluster) in ("on", "degraded"), 30)

    step("log check")
    log = session.get(f"{BASE}/api/error_log", timeout=30).text
    relevant = [
        line
        for line in log.splitlines()
        if "ha_vmware" in line and ("ERROR" in line or "WARNING" in line)
        and "custom integration" not in line
        and "Ignoring message" not in line
    ]
    print("\n".join(relevant) or "   clean")
    assert not relevant
    print("E2E PASSED", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as err:  # noqa: BLE001
        print(f"E2E FAILED: {err}", flush=True)
        sys.exit(1)
