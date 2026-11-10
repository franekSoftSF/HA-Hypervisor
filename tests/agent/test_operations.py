"""Cluster sequences and scenarios."""

from __future__ import annotations

import pytest

from conftest import FakeInfra, Lab, make_config
from ha_hypervisor_agent.inventory import build_state, build_view, remember
from ha_hypervisor_agent.operations import Busy, OperationFailed, Orchestrator
from ha_hypervisor_agent.protocol import (
    CMD_CLUSTER_SHUTDOWN as SHUTDOWN,
    CMD_CLUSTER_START as START,
    CMD_SCENARIO_START as SCENARIO_START,
    CMD_SCENARIO_STOP as SCENARIO_STOP,
)
from ha_hypervisor_agent.store import Store


def scenario(*groups, sid="s1", name="Test", **kwargs):
    return {
        "scenario": {
            "id": sid,
            "name": name,
            "steps": [{"vms": list(group), "wait": "tools", "timeout": 1} for group in groups],
            **kwargs,
        }
    }


def test_shutdown_runs_in_order(harness):
    lab = harness.lab
    lab.add("web", "h2")
    lab.add("vcls1", "h2", name="vCLS-1", tools=False)

    op = harness.run(SHUTDOWN)

    assert op["status"] == "completed", op
    assert lab.events == [
        "guest:web:vcenter",
        "vcls:domain-c1:False",
        "guest:vcsa:h1",
        "host:h2",
        "host:h1",
    ]
    assert harness.store.get("retreat_clusters") == ["domain-c1"]
    assert harness.store.get("vcenter_host") == "h1"


def test_dry_run_changes_nothing(harness):
    lab = harness.lab
    lab.add("web", "h2")

    op = harness.run(SHUTDOWN, {"dry_run": True})

    assert op["status"] == "completed"
    assert op["plan"]["vms"] == ["web"]
    assert op["plan"]["vcenter"] == {"vm": "vcsa", "host": "h1"}
    assert op["plan"]["hosts"] == ["h2", "h1"]
    assert lab.events == []


@pytest.mark.parametrize(
    ("setup", "code"),
    [
        (lambda lab: lab.can_wake.update(h2=False), "no_wake_method"),
        (lambda lab: lab.esxi_login_fails.add("h2"), "esxi_unavailable"),
    ],
)
def test_shutdown_refused_before_touching_anything(harness, setup, code):
    harness.lab.add("web", "h2")
    setup(harness.lab)

    op = harness.run(SHUTDOWN)

    assert (op["status"], op["error_code"]) == ("failed", code)
    assert harness.lab.events == []


def test_shutdown_refused_with_protected_vm(lab, tmp_path):
    config = make_config(tmp_path, protected_vms=["db"])
    lab.add("db", "h2")
    orchestrator = Orchestrator(config, FakeInfra(lab), Store(config.state_file), lambda _: None)

    orchestrator.start(SHUTDOWN, {})
    orchestrator.wait(10)

    assert orchestrator.operation["error_code"] == "protected_vm_running"
    assert lab.events == []


def test_shutdown_forces_vm_that_ignores_guest_shutdown(harness):
    harness.lab.add("stuck", "h2", ignores_shutdown=True)

    op = harness.run(SHUTDOWN)

    assert op["status"] == "completed"
    assert "force:stuck:vcenter" in harness.lab.events


def test_vm_without_tools_is_powered_off(harness):
    harness.lab.add("appliance", "h2", tools=False)

    assert harness.run(SHUTDOWN)["status"] == "completed"
    assert "force:appliance:vcenter" in harness.lab.events


def test_vcenter_is_never_forced(harness):
    lab = harness.lab
    lab.vms["vcsa"].ignores_shutdown = True

    op = harness.run(SHUTDOWN)

    assert op["error_code"] == "vcenter_stop_timeout"
    assert not lab.order("force:")
    assert not lab.order("host:")


def test_start_wakes_hosts_then_vcenter_then_leaves_retreat(harness):
    lab = harness.lab
    lab.add("vcls1", "h2", name="vCLS-1", tools=False)
    harness.run(SHUTDOWN)
    lab.events.clear()

    op = harness.run(START)

    assert op["status"] == "completed", op
    assert lab.events == ["wake:h1", "wake:h2", "on:vcsa:h1", "vcls:domain-c1:True"]
    assert harness.store.get("retreat_clusters") == []


def test_start_reports_wake_failure(harness):
    lab = harness.lab
    harness.run(SHUTDOWN)
    lab.wake_fails.add("h1")

    op = harness.run(START)

    assert op["error_code"] == "wake_failed"


def test_scenario_from_cold_wakes_only_needed_hosts(harness):
    lab = harness.lab
    lab.add("dc", "h1")
    lab.add("app", "h1")
    lab.add("other", "h2")
    harness.run(SHUTDOWN)
    lab.events.clear()

    op = harness.run(SCENARIO_START, scenario(["dc"], ["app"]))

    assert op["status"] == "completed", op
    assert lab.events == ["wake:h1", "on:vcsa:h1", "on:dc:vcenter", "on:app:vcenter"]
    assert "h2" not in lab.hosts_up
    assert lab.vms["other"].power == "off"


def test_scenario_fails_when_vm_does_not_report_ready(harness):
    lab = harness.lab
    lab.add("dc", "h1", power="off")
    lab.add("app", "h1", power="off")
    lab.vms_boot_tools = False
    body = scenario(["dc"], ["app"])
    body["scenario"]["steps"][0]["timeout"] = 0.02

    op = harness.run(SCENARIO_START, body)

    assert (op["status"], op["error_code"]) == ("failed", "vm_start_timeout")
    assert lab.vms["app"].power == "off"
    assert lab.vms["dc"].power == "on"


def test_scenario_unknown_vm(harness):
    op = harness.run(SCENARIO_START, scenario(["ghost"]))
    assert op["error_code"] == "vm_not_found"


def test_scenario_stop_reverses_and_keeps_shared_vms(harness):
    lab = harness.lab
    lab.add("dc", "h1", power="off")
    lab.add("app", "h1", power="off")
    lab.add("db", "h1", power="off")
    harness.run(SCENARIO_START, scenario(["dc"], ["app"], sid="a", name="A"))
    harness.run(SCENARIO_START, scenario(["dc"], ["db"], sid="b", name="B"))
    lab.events.clear()

    op = harness.run(SCENARIO_STOP, scenario(["dc"], ["app"], sid="a", name="A"))

    assert op["status"] == "completed"
    assert lab.events == ["guest:app:vcenter"]
    assert lab.vms["dc"].power == "on"
    assert list(harness.store.get("active_scenarios")) == ["b"]


def test_scenario_stop_powers_off_empty_hosts_only(harness):
    lab = harness.lab
    lab.add("app", "h2", power="off")
    body = scenario(["app"], power_off_empty_hosts=True)
    harness.run(SCENARIO_START, body)
    lab.events.clear()

    op = harness.run(SCENARIO_STOP, body)

    assert op["status"] == "completed", op
    assert lab.events == ["guest:app:vcenter", "host:h2"]
    assert "h1" in lab.hosts_up


def test_host_with_vcenter_stays_on_after_scenario(harness):
    lab = harness.lab
    lab.add("app", "h1", power="off")
    body = scenario(["app"], power_off_empty_hosts=True)
    harness.run(SCENARIO_START, body)

    harness.run(SCENARIO_STOP, body)

    assert "h1" in lab.hosts_up


def test_invalid_scenario_is_refused(harness):
    with pytest.raises(OperationFailed) as err:
        harness.orchestrator.start(SCENARIO_START, {"scenario": {"id": "x", "name": "n", "steps": []}})
    assert err.value.code == "invalid_request"


def test_second_operation_is_busy_and_cancel_stops_it(harness):
    lab = harness.lab
    lab.add("app", "h1", power="off")
    lab.vms_boot_tools = False
    body = scenario(["app"])
    body["scenario"]["steps"][0]["timeout"] = 30

    harness.orchestrator.start(SCENARIO_START, body)
    with pytest.raises(Busy):
        harness.orchestrator.start(START, {})
    with pytest.raises(Busy):
        harness.orchestrator.vm_power("app", "off")

    assert harness.orchestrator.cancel()
    harness.orchestrator.wait(10)
    assert harness.orchestrator.operation["status"] == "cancelled"
    assert not harness.orchestrator.busy


def test_interrupted_operation_is_closed_on_restart(harness, lab):
    harness.store.set("operation", {"id": "x", "status": "running", "step": "stop_vms"})

    restarted = Orchestrator(
        harness.config, FakeInfra(lab), Store(harness.config.state_file), lambda _: None
    )

    assert restarted.operation["status"] == "failed"
    assert restarted.operation["error_code"] == "interrupted"


@pytest.mark.parametrize(
    ("vm_name", "code"),
    [("vcsa", "vm_protected"), ("vCLS-9", "vm_protected")],
)
def test_vm_power_refuses_infrastructure(harness, vm_name, code):
    vm = harness.lab.add("x", "h1", name=vm_name) if vm_name != "vcsa" else harness.lab.vms["vcsa"]
    with pytest.raises(OperationFailed) as err:
        harness.orchestrator.vm_power(vm.id, "off")
    assert err.value.code == code


def test_vm_power_tools_missing(harness):
    harness.lab.add("x", "h1", tools=False)
    with pytest.raises(OperationFailed) as err:
        harness.orchestrator.vm_power("x", "shutdown")
    assert err.value.code == "tools_unavailable"


def test_state_keeps_vms_while_cluster_is_off(harness):
    lab = harness.lab
    lab.add("app", "h2")
    infra = FakeInfra(lab)
    view = build_view(harness.config, infra)
    remember(view, harness.config, harness.store)

    lab.hosts_up.clear()
    state = build_state(harness.config, infra, harness.store, build_view(harness.config, infra))

    assert state["cluster"] == "off"
    assert state["source"] == "none"
    assert [h["power"] for h in state["hosts"]] == ["off", "off"]
    assert {vm["name"]: vm["power"] for vm in state["vms"]} == {"app": "off", "vcsa": "off"}
    assert next(vm for vm in state["vms"] if vm["name"] == "vcsa")["protected"]


def test_state_degraded_without_vcenter(harness):
    lab = harness.lab
    lab.vms["vcsa"].power = "off"
    infra = FakeInfra(lab)

    state = build_state(harness.config, infra, harness.store, build_view(harness.config, infra))

    assert state["cluster"] == "degraded"
    assert state["source"] == "esxi"


def _mqtt_only(tmp_path):
    from ha_hypervisor_agent.config import parse

    return parse(
        {
            "agent_id": "lab",
            "shared_key": "k" * 40,
            "mqtt": {"host": "broker"},
            "state_file": str(tmp_path / "state.json"),
        }
    )


def test_mqtt_alone_is_a_valid_configuration(tmp_path):
    config = _mqtt_only(tmp_path)
    assert not config.configured
    assert config.vcenter is None
    assert config.vcenter_vm_name is None


def test_agent_without_vcenter_reports_not_configured(tmp_path):
    from ha_hypervisor_agent.vim import Infra

    config = _mqtt_only(tmp_path)
    infra = Infra(config)
    store = Store(config.state_file)
    state = build_state(config, infra, store, build_view(config, infra))
    assert state["cluster"] == "not_configured"
    assert state["hosts"] == []
    assert state["vms"] == []


def test_operations_need_a_configuration(tmp_path):
    config = _mqtt_only(tmp_path)
    orchestrator = Orchestrator(config, FakeInfra(Lab()), Store(config.state_file), lambda _: None)
    for kind in (SHUTDOWN, START):
        with pytest.raises(OperationFailed) as err:
            orchestrator.start(kind, {})
        assert err.value.code == "not_configured"


def test_check_lists_what_is_missing(tmp_path, monkeypatch, capsys):
    from ha_hypervisor_agent import service

    monkeypatch.setattr(service, "test_mqtt", lambda *_: None)
    assert service.check(_mqtt_only(tmp_path)) == 1
    out = capsys.readouterr().out
    assert "OK    MQTT" in out
    assert "MISSING vCenter" in out
    assert "MISSING ESXi hosts" in out


def test_state_on(harness):
    infra = FakeInfra(harness.lab)
    state = build_state(harness.config, infra, harness.store, build_view(harness.config, infra))
    assert state["cluster"] == "on"


def test_store_never_holds_secrets(harness):
    harness.run(SHUTDOWN)
    text = open(harness.config.state_file, encoding="utf-8").read()
    assert harness.config.shared_key not in text


# ------------------------------------------------- powered on by hand


def _manual_harness(lab, tmp_path, timeouts=None):
    from conftest import Harness

    hosts = [
        {"name": "h1", "address": "h1", "username": "u", "password": "p"},
        {"name": "h2", "address": "h2", "username": "u", "password": "p", "manual_power_on": True},
    ]
    config = make_config(tmp_path, hosts=hosts)
    if timeouts:
        import dataclasses

        config = dataclasses.replace(config, timeouts=dataclasses.replace(config.timeouts, **timeouts))
    store = Store(config.state_file)
    updates = []
    return Harness(lab, config, store, Orchestrator(config, FakeInfra(lab), store, updates.append), updates)


def test_host_without_bmc_can_be_shut_down_when_powered_on_by_hand(lab, tmp_path):
    lab.can_wake["h2"] = False
    lab.add("web", "h2")
    harness = _manual_harness(lab, tmp_path)
    op = harness.run(SHUTDOWN)
    assert op["status"] == "completed", op
    assert "manual_power_on:h2" in op["plan"]["warnings"]
    assert "h2" not in lab.hosts_up


def test_start_waits_for_a_host_powered_on_by_hand(lab, tmp_path):
    import threading

    lab.can_wake["h2"] = False
    harness = _manual_harness(lab, tmp_path, {"host_start": 3})
    harness.run(SHUTDOWN)
    lab.events.clear()
    # Someone presses the power button a moment later.
    threading.Timer(0.3, lambda: lab.hosts_up.add("h2")).start()
    op = harness.run(START)
    assert op["status"] == "completed", op
    assert "wake:h2" not in lab.events and "wake:h1" in lab.events


def test_start_times_out_when_nobody_powers_the_host_on(lab, tmp_path):
    lab.can_wake["h2"] = False
    harness = _manual_harness(lab, tmp_path, {"host_start": 0.2})
    harness.run(SHUTDOWN)
    op = harness.run(START)
    assert (op["status"], op["error_code"], op["error"]) == ("failed", "host_start_timeout", "h2")


def test_manual_power_on_flag_in_the_cli(tmp_path, capsys):
    from ha_hypervisor_agent import cli
    from ha_hypervisor_agent.config import load

    path = str(tmp_path / "c.yaml")
    cli.main(["init", "--config", path])
    cli.main(["mqtt", "set", "--host", "b", "--no-tls", "--config", path])
    import io
    import sys

    sys.stdin = io.StringIO("secret\n")
    try:
        assert cli.main(["esxi", "add", "h1", "--username", "root", "--password-stdin",
                         "--manual-power-on", "--config", path]) == 0
    finally:
        sys.stdin = sys.__stdin__
    assert "a person powers it on" in capsys.readouterr().out
    assert load(path).hosts[0].manual_power_on is True
    cli.main(["esxi", "list", "--config", path])
    assert "manual power on" in capsys.readouterr().out
    cli.main(["esxi", "add", "h1", "--update", "--no-manual-power-on", "--config", path])
    assert load(path).hosts[0].manual_power_on is False
