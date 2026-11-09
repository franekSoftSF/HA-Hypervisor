"""Fixtures for the Home Assistant side."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
)

from custom_components.ha_vmware.const import DOMAIN
from custom_components.ha_vmware.protocol import seal, topic, unseal

AGENT = "lab"
KEY = "k" * 40


@pytest.fixture(autouse=True)
def _enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture
def expected_lingering_timers() -> bool:
    """The mocked MQTT client keeps its periodic timer."""
    return True


def state_doc() -> dict[str, Any]:
    """Return a typical agent state."""
    return {
        "source": "vcenter",
        "cluster": "on",
        "vcenter": {"reachable": True, "version": "8"},
        "hosts": [
            {
                "id": "esxi1",
                "name": "esxi1",
                "connected": True,
                "power": "on",
                "maintenance": False,
                "cpu_used_mhz": 500,
                "cpu_total_mhz": 1000,
                "mem_used_mb": 200,
                "mem_total_mb": 1000,
                "uptime_s": 100,
                "vendor": "Supermicro",
                "model": "X11",
                "version": "8.0",
            }
        ],
        "vms": [
            _vm("vm-1", "web", "on"),
            _vm("vm-2", "db", "off"),
            _vm("vm-3", "vcsa", "on", protected=True),
            _vm("vm-4", "vCLS-1", "on", system=True),
        ],
        "datastores": [
            {"id": "ds1", "name": "ds1", "type": "VMFS", "capacity_gb": 100.0, "free_gb": 25.0}
        ],
        "scenarios": {},
    }


def _vm(vm_id: str, name: str, power: str, **flags: Any) -> dict[str, Any]:
    return {
        "id": vm_id,
        "name": name,
        "power": power,
        "host": "esxi1",
        "cpu_mhz": 10,
        "mem_mb": 20,
        "mem_total_mb": 1024,
        "cpus": 2,
        "storage_gb": 5.5,
        "tools": power == "on",
        "ip": "10.0.0.1",
        "guest_os": "Linux",
        "uptime_s": 5,
        "system": False,
        "protected": False,
        **flags,
    }


class FakeAgent:
    """Plays the agent on the mocked broker."""

    def __init__(self, hass, mqtt_mock) -> None:
        self.hass = hass
        self.mqtt_mock = mqtt_mock
        self.answers: dict[str, dict[str, Any]] = {}

    def fire(self, leaf: str, kind: str, body: dict[str, Any], key: str = KEY, ts: float | None = None) -> None:
        target = topic(AGENT, leaf)
        async_fire_mqtt_message(self.hass, target, seal(key, target, kind, body, now=ts))

    def online(self) -> None:
        async_fire_mqtt_message(self.hass, topic(AGENT, "status"), "online")
        self.fire("info", "info", {"agent_version": "1.0", "protocol": 1, "agent_id": AGENT})

    def commands(self) -> list[Any]:
        """Return what was sent, without the status request made at every start."""
        return [c for c in self.all_commands() if c.kind != "refresh"]

    def all_commands(self) -> list[Any]:
        target = topic(AGENT, "cmd")
        return [
            unseal(KEY, target, call.args[1], require_fresh=True)
            for call in self.mqtt_mock.async_publish.call_args_list
            if call.args[0] == target
        ]

    def install_responder(self) -> None:
        """Answer every command, as the agent would."""
        target = topic(AGENT, "cmd")

        async def respond(topic_name, payload, qos=0, retain=False, **_kwargs):
            if topic_name != target:
                return
            assert retain is False
            assert qos == 1
            envelope = unseal(KEY, target, payload)
            result = {"command_id": envelope.id, "ok": True, "code": None}
            result.update(self.answers.get(envelope.kind, {}))
            self.hass.loop.call_soon(self.fire, "result", "result", result)

        self.mqtt_mock.async_publish.side_effect = respond


@pytest.fixture
async def agent(hass, mqtt_mock) -> FakeAgent:
    fake = FakeAgent(hass, mqtt_mock)
    fake.install_responder()
    return fake


def make_entry(subentries=None) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="VMware lab",
        unique_id=AGENT,
        data={"agent_id": AGENT, "shared_key": KEY},
        subentries_data=subentries or (),
    )


async def setup_entry(hass, agent: FakeAgent, subentries=None) -> MockConfigEntry:
    entry = make_entry(subentries)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    agent.online()
    agent.fire("state", "state", state_doc())
    await hass.async_block_till_done()
    return entry


__all__ = ["AGENT", "KEY", "json", "patch"]
