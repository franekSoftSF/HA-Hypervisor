"""Signed messages, configuration, power chain and command intake."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import KEY, make_config
from ha_vmware_agent import config as config_module
from ha_vmware_agent import power as power_module
from ha_vmware_agent.config import BmcConfig, ConfigError, WolConfig
from ha_vmware_agent.power import HostPower, PowerError, Redfish, WakeOnLan
from ha_vmware_agent.protocol import (
    COMMAND_TTL,
    ERROR_CODES,
    ProtocolError,
    generate_key,
    seal,
    topic,
    unseal,
)
from ha_vmware_agent.service import AgentService, ReplayGuard

ROOT = Path(__file__).resolve().parents[2]
TARGET = topic("lab", "cmd")


def test_protocol_copies_are_identical():
    agent = ROOT / "agent" / "ha_vmware_agent" / "protocol.py"
    integration = ROOT / "custom_components" / "ha_vmware" / "protocol.py"
    assert agent.read_bytes() == integration.read_bytes()


def test_round_trip_with_unicode():
    payload = seal(KEY, TARGET, "x", {"name": "Zażółć"}, ttl=COMMAND_TTL)
    envelope = unseal(KEY, TARGET, payload, require_fresh=True)
    assert envelope.kind == "x"
    assert envelope.body == {"name": "Zażółć"}


def test_generated_key_is_accepted():
    assert len(generate_key()) >= 32


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d["body"].update(action="off"),
        lambda d: d.update(kind="other"),
        lambda d: d.update(exp=d["exp"] + 9999),
        lambda d: d.update(sig="0" * 64),
        lambda d: d.pop("sig"),
    ],
)
def test_tampering_is_detected(change):
    data = json.loads(seal(KEY, TARGET, "x", {"action": "on"}, ttl=60))
    change(data)
    with pytest.raises(ProtocolError) as err:
        unseal(KEY, TARGET, json.dumps(data))
    assert err.value.code == "bad_signature"


def test_other_key_and_other_topic_are_rejected():
    payload = seal(KEY, TARGET, "x", {}, ttl=60)
    with pytest.raises(ProtocolError):
        unseal("z" * 40, TARGET, payload)
    with pytest.raises(ProtocolError):
        unseal(KEY, topic("other", "cmd"), payload)


def test_expired_and_future_commands_are_rejected():
    old = seal(KEY, TARGET, "x", {}, ttl=60, now=1000)
    with pytest.raises(ProtocolError) as err:
        unseal(KEY, TARGET, old, require_fresh=True, now=1061)
    assert err.value.code == "expired"

    future = seal(KEY, TARGET, "x", {}, ttl=600, now=2000)
    with pytest.raises(ProtocolError) as err:
        unseal(KEY, TARGET, future, require_fresh=True, now=1000)
    assert err.value.code == "clock_skew"


def test_message_without_expiry_is_not_fresh():
    with pytest.raises(ProtocolError):
        unseal(KEY, TARGET, seal(KEY, TARGET, "x", {}), require_fresh=True)


@pytest.mark.parametrize("payload", ["not json", "[]", b"\xff"])
def test_malformed(payload):
    with pytest.raises(ProtocolError):
        unseal(KEY, TARGET, payload)


def test_replay_guard():
    guard = ReplayGuard()
    assert guard.accept("a", 9e12)
    assert not guard.accept("a", 9e12)
    assert guard.accept("b", 0)


def test_all_error_codes_are_unique():
    assert len(set(ERROR_CODES)) == len(ERROR_CODES)


def test_example_configuration_is_valid(monkeypatch):
    for name in (
        "HA_VMWARE_SHARED_KEY",
        "MQTT_PASSWORD",
        "VCENTER_PASSWORD",
        "ESXI_PASSWORD",
        "BMC_PASSWORD",
    ):
        monkeypatch.setenv(name, "s" * 40)
    config = config_module.load(ROOT / "agent" / "config.example.yaml")
    assert config.hosts[0].bmc.type == "auto"
    assert config.mqtt.transport == "websockets"


def test_missing_environment_variable(tmp_path):
    with pytest.raises(ConfigError, match="NOPE"):
        make_config(tmp_path, shared_key="${NOPE}")


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"unknown_key": 1}, "unknown"),
        ({"agent_id": "Bad/Id"}, "agent_id"),
        ({"shared_key": "short"}, "too short"),
        ({"hosts": []}, "at least one"),
        (
            {"hosts": [{"name": "h", "address": "a", "username": "u", "password": "p", "wol": {"mac": "zz"}}]},
            "MAC",
        ),
    ],
)
def test_invalid_configuration(tmp_path, override, message):
    with pytest.raises(ConfigError, match=message):
        make_config(tmp_path, **override)


class Method:
    def __init__(self, name, answer=None, fails=False):
        self.name = name
        self.answer = answer
        self.fails = fails
        self.calls = 0

    def state(self):
        if self.fails or self.answer is None:
            raise PowerError("no")
        return self.answer

    def power_on(self):
        self.calls += 1
        if self.fails:
            raise PowerError("no")


def test_power_falls_through_the_chain():
    redfish, ipmi, wol = Method("redfish", fails=True), Method("ipmi", "off"), Method("wol")
    power = HostPower([redfish, ipmi, wol])
    assert power.state() == "off"
    assert power.power_on() == "ipmi"
    assert wol.calls == 0


def test_power_all_methods_fail():
    power = HostPower([Method("redfish", fails=True)])
    assert power.state() == "unknown"
    assert not power.can_wake()
    with pytest.raises(PowerError):
        power.power_on()


def test_wake_on_lan_counts_as_a_way_back_but_is_unverified():
    power = HostPower([WakeOnLan(WolConfig(mac="00:11:22:33:44:55"))])
    assert power.can_wake()
    assert not power.verified()


def test_magic_packet_format():
    packet = WakeOnLan(WolConfig(mac="00-11-22-33-44-55")).packet
    assert packet == b"\xff" * 6 + bytes.fromhex("001122334455") * 16


def test_redfish_requests(monkeypatch):
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs.get("json")))
        body = (
            {"Members": [{"@odata.id": "/redfish/v1/Systems/1"}]}
            if url.endswith("/Systems")
            else {"PowerState": "Off"}
        )
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: body)

    redfish = Redfish(BmcConfig(address="bmc", username="u", password="p"))
    monkeypatch.setattr(redfish._session, "request", request)  # noqa: SLF001

    assert redfish.state() == "off"
    redfish.power_on()
    assert calls[-1] == (
        "POST",
        "https://bmc/redfish/v1/Systems/1/Actions/ComputerSystem.Reset",
        {"ResetType": "On"},
    )


def test_tcp_open_on_closed_port():
    assert not power_module.tcp_open("127.0.0.1", 1, timeout=0.2)


@pytest.fixture
def service(tmp_path):
    instance = AgentService(make_config(tmp_path))
    instance.executed = []
    instance._workers = SimpleNamespace(  # noqa: SLF001
        submit=lambda _fn, envelope: instance.executed.append(envelope.kind)
    )
    return instance


def message(payload, retain=False):
    return SimpleNamespace(topic=TARGET, payload=payload, retain=retain)


def test_service_accepts_signed_command(service):
    service._on_message(None, None, message(seal(KEY, TARGET, "refresh", {}, ttl=60)))  # noqa: SLF001
    assert service.executed == ["refresh"]


def test_service_rejects_replay(service):
    payload = seal(KEY, TARGET, "refresh", {}, ttl=60)
    service._on_message(None, None, message(payload))  # noqa: SLF001
    service._on_message(None, None, message(payload))  # noqa: SLF001
    assert service.executed == ["refresh"]


def test_service_rejects_retained_command(service):
    payload = seal(KEY, TARGET, "cluster_shutdown", {}, ttl=60)
    service._on_message(None, None, message(payload, retain=True))  # noqa: SLF001
    assert service.executed == []


def test_service_rejects_expired_command(service):
    payload = seal(KEY, TARGET, "cluster_shutdown", {}, ttl=1, now=1000)
    service._on_message(None, None, message(payload))  # noqa: SLF001
    assert service.executed == []


def test_service_rejects_wrong_key(service):
    payload = seal("z" * 40, TARGET, "cluster_shutdown", {}, ttl=60)
    service._on_message(None, None, message(payload))  # noqa: SLF001
    assert service.executed == []


def test_broker_settings_never_queue_commands(service):
    assert service._client._clean_session is True  # noqa: SLF001
