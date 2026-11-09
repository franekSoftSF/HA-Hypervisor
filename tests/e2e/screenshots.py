"""Builds a demo lab on a fresh Home Assistant and captures docs/images."""

from __future__ import annotations

import json
from pathlib import Path
import time
from typing import Any

from playwright.sync_api import Page, sync_playwright
from pyVim.connect import SmartConnect
from pyVmomi import vim
import requests
import yaml

import driver
from driver import AGENT, BASE, CLIENT, KEY, api, flow, state, step, wait

ROOT = Path("/src")
IMAGES = ROOT / "docs" / "images"
VM_NAMES = ["dc01", "sql01", "web01"]
DASHBOARD = "ha-hypervisor"
DASHBOARD_PL = {
    "Cluster": "Klaster",
    "Control": "Sterowanie",
    "Virtual machines": "Maszyny wirtualne",
    "Scenarios": "Scenariusze",
}
LANGUAGES = {
    "en": {"add": "Add scenario", "pick": "Add", "submit": "Submit", "scenario": "VDI test"},
    "pl": {"add": "Dodaj scenariusz", "pick": "Utwórz", "submit": "Zatwierdź", "scenario": "Test VDI"},
}


def rename_vms() -> None:
    step("naming the simulated VMs")
    si = SmartConnect(host="vcsim", port=443, user="user", pwd="pass", disableSslCertValidation=True)
    content = si.RetrieveContent()
    view = content.viewManager.CreateContainerView(content.rootFolder, [vim.VirtualMachine], True)
    machines = sorted(view.view, key=lambda vm: vm.name)
    for machine, name in zip(machines, VM_NAMES, strict=True):
        machine.Rename_Task(newName=name)
    time.sleep(5)


def finish_onboarding() -> None:
    for path, body in (
        ("core_config", {}),
        ("analytics", {}),
        ("integration", {"client_id": CLIENT, "redirect_uri": CLIENT}),
    ):
        api("POST", f"/api/onboarding/{path}", json=body)


def tokens() -> dict[str, Any]:
    login = {"client_id": CLIENT, "handler": ["homeassistant", None], "redirect_uri": CLIENT}
    started = requests.post(f"{BASE}/auth/login_flow", json=login, timeout=30).json()
    finished = requests.post(
        f"{BASE}/auth/login_flow/{started['flow_id']}",
        json={"client_id": CLIENT, "username": "e2e", "password": "e2e-test-only"},
        timeout=30,
    ).json()
    token = requests.post(
        f"{BASE}/auth/token",
        data={"grant_type": "authorization_code", "code": finished["result"], "client_id": CLIENT},
        timeout=30,
    ).json()
    return {
        **token,
        "hassUrl": BASE,
        "clientId": CLIENT,
        "expires": int(time.time() * 1000) + 3_000_000,
    }


def add_scenario(entry_id: str, name: str, groups: list[list[str]], ids: dict[str, str]) -> None:
    path = "/api/config/config_entries/subentries/flow"
    result = api("POST", path, json={"handler": [entry_id, "scenario"]})
    result = api("POST", f"{path}/{result['flow_id']}", json={"name": name, "power_off_empty_hosts": False})
    for index, group in enumerate(groups):
        result = api(
            "POST",
            f"{path}/{result['flow_id']}",
            json={
                "vms": [ids[vm] for vm in group],
                "wait": "power",
                "delay": 0,
                "timeout": 30,
                "add_another": index + 1 < len(groups),
            },
        )
    assert result["type"] == "create_entry", result


def build_lab() -> str:
    driver.onboard()
    finish_onboarding()
    rename_vms()

    step("integrations")
    other = {"set_client_cert": False, "set_ca_cert": "off"}
    flow("/api/config/config_entries/flow", "mqtt", {"broker": "mosquitto", "port": 1883, "other_settings": other})
    result = flow("/api/config/config_entries/flow", "ha_hypervisor", {"agent_id": AGENT, "shared_key": KEY})
    assert result["type"] == "create_entry", result
    entry_id = result["result"]["entry_id"]
    wait("cluster data", lambda: state("sensor.hypervisor_e2e_cluster") == "on")

    step("scenarios")
    start = api("POST", "/api/config/config_entries/subentries/flow", json={"handler": [entry_id, "scenario"]})
    group = api(
        "POST",
        f"/api/config/config_entries/subentries/flow/{start['flow_id']}",
        json={"name": "probe", "power_off_empty_hosts": False},
    )
    options = next(f for f in group["data_schema"] if f["name"] == "vms")["selector"]["select"]["options"]
    api("DELETE", f"/api/config/config_entries/subentries/flow/{group['flow_id']}")
    ids = {option["label"]: option["value"] for option in options}
    # Each new scenario reloads the entry, the next flow has to wait for that.
    add_scenario(entry_id, "VDI test", [["dc01"], ["sql01"], ["web01"]], ids)
    wait("first scenario", lambda: state("sensor.vdi_test_state") == "inactive", 30)
    add_scenario(entry_id, "Only AD", [["dc01"]], ids)
    wait("second scenario", lambda: state("sensor.only_ad_state") == "inactive", 30)

    driver.call("ha_hypervisor", "start_scenario", scenario="VDI test")
    wait("scenario active", lambda: state("sensor.vdi_test_state") == "active", 60)
    driver.call("button", "press", entity_id="button.hypervisor_e2e_preview_shutdown")
    wait("preview", lambda: state("sensor.hypervisor_e2e_operation") == "completed", 30)
    return entry_id


def settle(page: Page, seconds: float = 3) -> None:
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(int(seconds * 1000))


def hass_ws(page: Page, message: dict[str, Any]) -> Any:
    return page.evaluate(
        "message => document.querySelector('home-assistant').hass.callWS(message)", message
    )


def shot(page: Page, language: str, name: str) -> None:
    target = IMAGES / language / f"{name}.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(target))
    print(f"   {target.relative_to(ROOT)}: {target.stat().st_size // 1024} KiB", flush=True)


def device_id(page: Page, name: str) -> str:
    devices = hass_ws(page, {"type": "config/device_registry/list"})
    return next(device["id"] for device in devices if device["name"] == name)


def set_language(page: Page, language: str, entry_id: str) -> None:
    """Switch the interface and the entity names, which follow the system language."""
    hass_ws(
        page,
        {
            "type": "frontend/set_user_data",
            "key": "language",
            "value": {"language": language, "number_format": "language"},
        },
    )
    hass_ws(page, {"type": "config/core/update", "language": language, "country": "PL"})
    api("POST", f"/api/config/config_entries/entry/{entry_id}/reload")
    wait("entities after language change", lambda: state("sensor.hypervisor_e2e_cluster") == "on", 30)


def save_dashboard(page: Page, language: str) -> None:
    text = (ROOT / "docs" / "dashboard.example.yaml").read_text(encoding="utf-8")
    text = text.replace("hypervisor_lab_", "hypervisor_e2e_")
    if language == "pl":
        for english, polish in DASHBOARD_PL.items():
            text = text.replace(f"heading: {english}", f"heading: {polish}")
    hass_ws(
        page,
        {"type": "lovelace/config/save", "url_path": DASHBOARD, "config": yaml.safe_load(text)},
    )


def capture(page: Page, language: str, entry_id: str) -> None:
    step(f"capturing {language}")
    labels = LANGUAGES[language]
    set_language(page, language, entry_id)
    save_dashboard(page, language)

    page.set_viewport_size({"width": 1500, "height": 820})
    page.goto(f"{BASE}/{DASHBOARD}/hypervisor")
    settle(page, 6)
    shot(page, language, "dashboard")

    page.set_viewport_size({"width": 1400, "height": 1320})
    page.goto(f"{BASE}/config/integrations/integration/ha_hypervisor")
    settle(page)
    shot(page, language, "integration")

    page.set_viewport_size({"width": 1400, "height": 1020})
    page.goto(f"{BASE}/config/devices/device/{device_id(page, 'Hypervisor e2e')}")
    settle(page)
    shot(page, language, "cluster-device")

    page.set_viewport_size({"width": 1400, "height": 860})
    page.goto(f"{BASE}/config/devices/device/{device_id(page, 'dc01')}")
    settle(page)
    shot(page, language, "vm-device")

    page.goto(f"{BASE}/config/integrations/integration/ha_hypervisor")
    settle(page)
    page.get_by_text(labels["add"], exact=True).first.click()
    settle(page, 2)
    dialog = page.locator("dialog-data-entry-flow")
    dialog.locator("input").first.fill(labels["scenario"])
    page.wait_for_timeout(500)
    dialog.get_by_text(labels["submit"], exact=True).click()
    settle(page, 2)
    dialog.get_by_text(labels["pick"], exact=True).first.click()
    page.wait_for_timeout(1500)
    page.get_by_text("dc01", exact=True).last.click()
    page.wait_for_timeout(1000)
    shot(page, language, "scenario-wizard")


def main() -> None:
    entry_id = build_lab()
    credentials = tokens()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport={"width": 1400, "height": 900}, locale="en-US")
        context.add_init_script(
            f"window.localStorage.setItem('hassTokens', {json.dumps(json.dumps(credentials))});"
        )
        page = context.new_page()
        page.goto(BASE)
        settle(page, 5)
        hass_ws(
            page,
            {
                "type": "lovelace/dashboards/create",
                "url_path": DASHBOARD,
                "title": "Hypervisor",
                "icon": "mdi:server",
                "show_in_sidebar": True,
                "require_admin": False,
                "mode": "storage",
            },
        )
        for language in LANGUAGES:
            capture(page, language, entry_id)
        browser.close()
    print("SCREENSHOTS DONE", flush=True)


if __name__ == "__main__":
    main()
