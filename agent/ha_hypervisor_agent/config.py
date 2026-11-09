"""Agent configuration: a YAML file with ${ENV} references for secrets."""

from __future__ import annotations

import dataclasses
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

import yaml

from .protocol import is_valid_agent_id, is_valid_key

T = TypeVar("T")
_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_MAC = re.compile(r"^([0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}$")


class ConfigError(ValueError):
    """The configuration file is not usable."""


@dataclass(frozen=True)
class MqttConfig:
    host: str
    port: int = 8883
    transport: str = "tcp"  # tcp or websockets
    path: str = "/mqtt"  # websockets only
    tls: bool = True
    verify_tls: bool = True
    ca_file: str | None = None
    username: str | None = None
    password: str | None = None


@dataclass(frozen=True)
class VCenterConfig:
    host: str
    username: str
    password: str
    port: int = 443
    verify_ssl: bool = False
    # Name of the vCenter VM when it runs inside the managed hosts.
    vm_name: str | None = None


@dataclass(frozen=True)
class BmcConfig:
    address: str
    username: str
    password: str
    type: str = "auto"  # auto, redfish or ipmi
    verify_ssl: bool = False


@dataclass(frozen=True)
class WolConfig:
    mac: str
    broadcast: str = "255.255.255.255"
    port: int = 9


@dataclass(frozen=True)
class HostConfig:
    # Must equal the host name shown in vCenter.
    name: str
    address: str
    username: str
    password: str
    port: int = 443
    verify_ssl: bool = False
    bmc: BmcConfig | None = None
    wol: WolConfig | None = None


@dataclass(frozen=True)
class Timeouts:
    guest_stop: float = 300
    vcenter_stop: float = 900
    host_stop: float = 300
    host_start: float = 900
    vcenter_start: float = 1800
    host_sync: float = 300
    vcls_retreat: float = 180
    poll_step: float = 5


@dataclass(frozen=True)
class AgentConfig:
    agent_id: str
    shared_key: str
    mqtt: MqttConfig
    vcenter: VCenterConfig
    hosts: tuple[HostConfig, ...]
    protected_vms: tuple[str, ...] = ()
    force_after_timeout: bool = True
    poll_interval: float = 30
    state_file: str = "/data/state.json"
    log_level: str = "INFO"
    timeouts: Timeouts = field(default_factory=Timeouts)

    def host(self, name: str) -> HostConfig | None:
        """Return the configuration of a host by its vCenter name."""
        return next((host for host in self.hosts if host.name == name), None)


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in os.environ:
                raise ConfigError(f"environment variable {name} is not set")
            return os.environ[name]

        return _ENV.sub(replace, value)
    if isinstance(value, dict):
        return {key: _expand(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand(item) for item in value]
    return value


def _build(cls: type[T], data: Any, where: str, **nested: Any) -> T:
    if not isinstance(data, dict):
        raise ConfigError(f"{where}: expected a mapping")
    known = {item.name: item for item in dataclasses.fields(cls)}  # type: ignore[arg-type]
    unknown = set(data) - set(known)
    if unknown:
        raise ConfigError(f"{where}: unknown keys {sorted(unknown)}")
    values = {**data, **nested}
    missing = [
        name
        for name, item in known.items()
        if name not in values
        and item.default is dataclasses.MISSING
        and item.default_factory is dataclasses.MISSING
    ]
    if missing:
        raise ConfigError(f"{where}: missing keys {missing}")
    return cls(**values)


def _optional(cls: type[T], data: dict[str, Any], key: str, where: str) -> T | None:
    if data.get(key) is None:
        return None
    return _build(cls, data[key], f"{where}.{key}")


def parse(raw: Any) -> AgentConfig:
    """Validate parsed YAML and return the configuration."""
    data = _expand(raw)
    if not isinstance(data, dict):
        raise ConfigError("the configuration must be a mapping")
    for key in ("mqtt", "vcenter", "hosts", "agent_id", "shared_key"):
        if key not in data:
            raise ConfigError(f"missing key {key}")

    hosts = []
    for index, item in enumerate(data["hosts"] or []):
        where = f"hosts[{index}]"
        if not isinstance(item, dict):
            raise ConfigError(f"{where}: expected a mapping")
        hosts.append(
            _build(
                HostConfig,
                item,
                where,
                bmc=_optional(BmcConfig, item, "bmc", where),
                wol=_optional(WolConfig, item, "wol", where),
            )
        )

    config = _build(
        AgentConfig,
        data,
        "config",
        mqtt=_build(MqttConfig, data["mqtt"], "mqtt"),
        vcenter=_build(VCenterConfig, data["vcenter"], "vcenter"),
        hosts=tuple(hosts),
        protected_vms=tuple(data.get("protected_vms") or ()),
        timeouts=_build(Timeouts, data.get("timeouts") or {}, "timeouts"),
    )
    _validate(config)
    return config


def _validate(config: AgentConfig) -> None:
    if not is_valid_agent_id(config.agent_id):
        raise ConfigError("agent_id: use lowercase letters, digits, - and _")
    if not is_valid_key(config.shared_key):
        raise ConfigError("shared_key: too short, run the genkey command")
    if config.mqtt.transport not in ("tcp", "websockets"):
        raise ConfigError("mqtt.transport: use tcp or websockets")
    if not config.hosts:
        raise ConfigError("hosts: at least one host is required")
    names = [host.name for host in config.hosts]
    if len(set(names)) != len(names):
        raise ConfigError("hosts: names must be unique")
    for host in config.hosts:
        if host.bmc is not None and host.bmc.type not in ("auto", "redfish", "ipmi"):
            raise ConfigError(f"hosts.{host.name}.bmc.type: use auto, redfish or ipmi")
        if host.wol is not None and not _MAC.match(host.wol.mac):
            raise ConfigError(f"hosts.{host.name}.wol.mac: invalid MAC address")


def load(path: str | Path) -> AgentConfig:
    """Read and validate a configuration file."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as err:
        raise ConfigError(f"cannot read {path}: {err}") from err
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as err:
        raise ConfigError(f"invalid YAML: {err}") from err
    return parse(raw)
