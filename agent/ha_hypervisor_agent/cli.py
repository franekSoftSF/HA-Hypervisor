"""Command line: run the agent and manage its connections."""

from __future__ import annotations

import argparse
import getpass
import logging
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
from typing import Any

import yaml

from . import __version__
from .config import (
    DEFAULT_CONFIG,
    PACKAGE_STATE_FILE,
    ConfigError,
    PROXMOX_PORT,
    build_host,
    build_mqtt,
    build_proxmox,
    build_vcenter,
    expand,
    load,
)
from .power import HostPower
from .protocol import generate_key, is_valid_agent_id
from .service import AgentService, check, test_mqtt
from .vim import VimClient, VimError, VimUnavailable

_GROUP = "ha-hypervisor-agent"
_SERVICE = "ha-hypervisor-agent"
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_HIDDEN = "********"
_SECRET_KEYS = ("password", "shared_key", "token_secret")
_REQUIRED = {
    "mqtt": "mqtt set",
    "vcenter": "vcenter set",
    "hosts": "esxi add",
}
_REQUIRED_PROXMOX = {
    "mqtt": "mqtt set",
    "proxmox": "proxmox set",
    "hosts": "node add",
}
EXIT_CONFIG = 2
_PLAIN_PORTS = (1883, 1884)


class ConfigFile:
    """The YAML file edited by the commands, ${ENV} references stay as written."""

    def __init__(self, path: str) -> None:
        """Keep the path."""
        self.path = Path(path)

    def load(self) -> dict[str, Any]:
        """Return the file content."""
        try:
            data = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError as err:
            raise ConfigError(
                f"{self.path} does not exist, run: ha-hypervisor-agent init"
            ) from err
        except PermissionError as err:
            raise ConfigError(f"no access to {self.path}, use sudo") from err
        except yaml.YAMLError as err:
            raise ConfigError(f"invalid YAML: {err}") from err
        if not isinstance(data, dict):
            raise ConfigError(f"{self.path} is not a mapping")
        return data

    def save(self, data: dict[str, Any]) -> None:
        """Write atomically, readable only by root and the agent."""
        temporary = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                yaml.safe_dump(data, stream, sort_keys=False, allow_unicode=True)
            os.chmod(temporary, 0o640)
            _give_to_agent_group(temporary)
            os.replace(temporary, self.path)
        except PermissionError as err:
            raise ConfigError(f"no access to {self.path}, use sudo") from err


def _give_to_agent_group(path: Path) -> None:
    import grp  # noqa: PLC0415

    if os.geteuid() != 0:
        return
    try:
        os.chown(path, 0, grp.getgrnam(_GROUP).gr_gid)
    except KeyError:
        pass  # Not installed from the package, there is no such group.


def _env_name(value: str) -> str:
    if not _ENV_NAME.match(value):
        raise argparse.ArgumentTypeError("not a valid variable name")
    return value


def _secret(
    label: str, env: str | None, from_stdin: bool, existing: str | None
) -> str:
    """Return a password. It is never accepted as an argument: ps would show it."""
    if env:
        return f"${{{env}}}"
    if from_stdin:
        line = sys.stdin.readline().rstrip("\r\n")
        if not line:
            raise ConfigError(f"{label}: no password on standard input")
        return line
    if sys.stdin.isatty():
        hint = " (empty keeps the current one)" if existing else ""
        value = getpass.getpass(f"{label}{hint}: ")
        if value:
            return value
    if existing:
        return existing
    raise ConfigError(f"{label}: a password is required")


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _HIDDEN
            if key in _SECRET_KEYS and not str(item).startswith("${")
            else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _apply(target: dict[str, Any], args: argparse.Namespace, keys: tuple[str, ...]) -> None:
    for key in keys:
        value = getattr(args, key)
        if value is not None:
            target[key] = value


def _say(text: str) -> None:
    print(text)  # noqa: T201


def _service_state() -> str | None:
    """Return the systemd state of the agent service, None without systemd."""
    if not Path("/run/systemd/system").is_dir() or not shutil.which("systemctl"):
        return None
    result = subprocess.run(  # noqa: S603
        ["systemctl", "is-active", _SERVICE],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() or None


def _after_change() -> None:
    """Remind that the running agent reads its configuration only at start."""
    state = _service_state()
    if state is None:
        return
    command = f"sudo systemctl restart {_SERVICE}"
    if state == "active":
        _say(f"Restart the agent to apply the change: {command}")
    else:
        _say(f"The agent is not running ({state}), start it: {command}")


# ------------------------------------------------------------------- general


def cmd_init(args: argparse.Namespace) -> int:
    """Create the configuration file with a new shared key."""
    target = ConfigFile(args.config)
    if target.path.exists() and not args.force:
        raise ConfigError(f"{target.path} already exists, use --force to replace it")
    if not is_valid_agent_id(args.agent_id):
        raise ConfigError("agent id: use lowercase letters, digits, - and _")
    key = generate_key()
    target.save(
        {
            "agent_id": args.agent_id,
            "shared_key": key,
            "state_file": args.state_file,
        }
    )
    _say(f"Created {target.path}")
    _say(f"Agent ID:   {args.agent_id}")
    _say(f"Shared key: {key}")
    _say("Enter both when adding the integration in Home Assistant.")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    """Print the configuration without secrets."""
    data = ConfigFile(args.config).load()
    _say(yaml.safe_dump(_redact(data), sort_keys=False, allow_unicode=True).rstrip())
    required = _REQUIRED_PROXMOX if data.get("platform") == "proxmox" else _REQUIRED
    missing = [command for key, command in required.items() if not data.get(key)]
    if missing:
        _say("\nStill missing, run: " + ", ".join(missing))
    return 0


def cmd_key_show(args: argparse.Namespace) -> int:
    """Print the shared key."""
    _say(ConfigFile(args.config).load()["shared_key"])
    return 0


def cmd_key_rotate(args: argparse.Namespace) -> int:
    """Replace the shared key."""
    target = ConfigFile(args.config)
    data = target.load()
    data["shared_key"] = generate_key()
    target.save(data)
    _say(f"New shared key: {data['shared_key']}")
    _say("Enter it in Home Assistant (Reconfigure).")
    _after_change()
    return 0


# ---------------------------------------------------------------------- mqtt


def _default_mqtt_port(settings: dict[str, Any]) -> int:
    if settings.get("transport") == "websockets":
        return 443 if settings.get("tls", True) else 1884
    return 8883 if settings.get("tls", True) else 1883


def cmd_mqtt_set(args: argparse.Namespace) -> int:
    """Set the broker connection."""
    target = ConfigFile(args.config)
    data = target.load()
    settings = dict(data.get("mqtt") or {})
    _apply(
        settings,
        args,
        ("host", "port", "transport", "path", "tls", "verify_tls", "ca_file", "username"),
    )
    if "host" not in settings:
        raise ConfigError("--host is required")
    if args.tls is None and args.port is not None and args.port in _PLAIN_PORTS:
        # 1883 and 1884 are the unencrypted ports of every broker: asking for
        # one of them means no TLS, unless --tls says otherwise.
        settings["tls"] = False
    if args.no_auth:
        settings.pop("username", None)
        settings.pop("password", None)
    elif settings.get("username"):
        settings["password"] = _secret(
            "MQTT password", args.password_env, args.password_stdin, settings.get("password")
        )
    settings.setdefault("port", _default_mqtt_port(settings))
    if settings.get("transport") != "websockets":
        settings.pop("path", None)
    build_mqtt(settings)
    data["mqtt"] = settings
    target.save(data)
    tls = "TLS" if settings.get("tls", True) else "no TLS"
    _say(f"Saved MQTT connection to {settings['host']}:{settings['port']} ({tls})")
    _after_change()
    return 0


def cmd_mqtt_test(args: argparse.Namespace) -> int:
    """Try to log in to the broker."""
    data = ConfigFile(args.config).load()
    if not data.get("mqtt"):
        raise ConfigError("no MQTT connection, run: ha-hypervisor-agent mqtt set")
    settings = build_mqtt(expand(data["mqtt"]))
    problem = test_mqtt(settings, data["agent_id"])
    if problem:
        _say(f"FAIL  MQTT {settings.host}:{settings.port}: {problem}")
        if settings.tls and "SSL" in problem:
            _say("      The broker may not use TLS on this port, try: mqtt set --no-tls")
        return 1
    _say(f"OK    MQTT {settings.host}:{settings.port}")
    return 0


# ------------------------------------------------------------------- vcenter


def cmd_vcenter_set(args: argparse.Namespace) -> int:
    """Set the vCenter connection."""
    target = ConfigFile(args.config)
    data = target.load()
    _require_platform(data, "vmware")
    settings = dict(data.get("vcenter") or {})
    _apply(settings, args, ("host", "port", "username", "verify_ssl", "vm_name"))
    if args.no_vm_name:
        settings.pop("vm_name", None)
    for key, flag in (("host", "--host"), ("username", "--username")):
        if key not in settings:
            raise ConfigError(f"{flag} is required")
    settings["password"] = _secret(
        "vCenter password", args.password_env, args.password_stdin, settings.get("password")
    )
    build_vcenter(settings)
    data["vcenter"] = settings
    target.save(data)
    _say(f"Saved vCenter connection to {settings['host']}")
    _after_change()
    return 0


def cmd_vcenter_test(args: argparse.Namespace) -> int:
    """Log in to vCenter and compare its hosts with the configuration."""
    data = ConfigFile(args.config).load()
    if not data.get("vcenter"):
        raise ConfigError("no vCenter connection, run: ha-hypervisor-agent vcenter set")
    settings = build_vcenter(expand(data["vcenter"]))
    try:
        client = VimClient.connect(
            settings.host, settings.port, settings.username, settings.password, settings.verify_ssl
        )
        inventory = client.collect()
    except (VimUnavailable, VimError) as err:
        _say(f"FAIL  vCenter {settings.host}: {err}")
        return 1
    _say(f"OK    vCenter {settings.host}: {client.version}")
    _say(f"      {len(inventory['vms'])} VMs, {len(inventory['hosts'])} hosts")
    configured = {host["name"] for host in data.get("hosts") or []}
    for host in inventory["hosts"]:
        state = "configured" if host["name"] in configured else "NOT configured, use esxi add"
        _say(f"      host {host['name']}: {state}")
    client.disconnect()
    return 0


# ---------------------------------------------------------------------- esxi


def _merge_bmc(args: argparse.Namespace, entry: dict[str, Any]) -> None:
    if args.no_bmc:
        entry.pop("bmc", None)
        return
    given = (
        args.bmc_address,
        args.bmc_username,
        args.bmc_type,
        args.bmc_verify_ssl,
        args.bmc_password_env,
    )
    if all(value is None for value in given) and not args.bmc_password_stdin:
        return
    bmc = dict(entry.get("bmc") or {})
    for key, value in (
        ("address", args.bmc_address),
        ("username", args.bmc_username),
        ("type", args.bmc_type),
        ("verify_ssl", args.bmc_verify_ssl),
    ):
        if value is not None:
            bmc[key] = value
    for key, flag in (("address", "--bmc-address"), ("username", "--bmc-username")):
        if key not in bmc:
            raise ConfigError(f"{flag} is required")
    bmc["password"] = _secret(
        "BMC password", args.bmc_password_env, args.bmc_password_stdin, bmc.get("password")
    )
    entry["bmc"] = bmc


def _merge_wol(args: argparse.Namespace, entry: dict[str, Any]) -> None:
    if args.no_wol:
        entry.pop("wol", None)
        return
    if args.wol_mac is None and args.wol_broadcast is None:
        return
    wol = dict(entry.get("wol") or {})
    for key, value in (("mac", args.wol_mac), ("broadcast", args.wol_broadcast)):
        if value is not None:
            wol[key] = value
    if "mac" not in wol:
        raise ConfigError("--wol-mac is required")
    entry["wol"] = wol


def _platform(data: dict[str, Any]) -> str:
    return data.get("platform") or "vmware"


def _require_platform(data: dict[str, Any], wanted: str) -> None:
    """One agent drives one platform: a second one needs its own agent."""
    current = _platform(data)
    if current == wanted:
        return
    if wanted == "proxmox" and not data.get("platform") and not _uses_vmware(data):
        raise ConfigError("set the API token first: ha-hypervisor-agent proxmox set --token-id ...")
    commands = {"vmware": "vcenter set and esxi add", "proxmox": "proxmox set and node add"}
    raise ConfigError(
        f"this agent is set up for {current} ({commands[current]}). One agent drives one "
        f"platform: install a second agent for {wanted}, with its own --config and agent id"
    )


def _uses_vmware(data: dict[str, Any]) -> bool:
    return bool(data.get("vcenter")) or (
        _platform(data) == "vmware" and bool(data.get("hosts"))
    )


def cmd_esxi_add(args: argparse.Namespace) -> int:
    """Add an ESXi host, or change one with --update."""
    return _host_add(args, proxmox=False)


def cmd_node_add(args: argparse.Namespace) -> int:
    """Add a Proxmox node, or change one with --update."""
    return _host_add(args, proxmox=True)


def _host_add(args: argparse.Namespace, *, proxmox: bool) -> int:
    target = ConfigFile(args.config)
    data = target.load()
    _require_platform(data, "proxmox" if proxmox else "vmware")
    hosts: list[dict[str, Any]] = list(data.get("hosts") or [])
    index = next((i for i, host in enumerate(hosts) if host["name"] == args.name), None)
    if index is not None and not args.update:
        raise ConfigError(f"{args.name} already exists, use --update to change it")
    if index is None and args.update:
        raise ConfigError(f"{args.name} does not exist")

    entry = dict(hosts[index]) if index is not None else {"name": args.name}
    if proxmox:
        _apply(entry, args, ("address", "port"))
        entry.setdefault("port", PROXMOX_PORT)
    else:
        _apply(entry, args, ("address", "port", "username", "verify_ssl"))
    entry.setdefault("address", args.name)
    if not proxmox:
        if "username" not in entry:
            raise ConfigError("--username is required")
        entry["password"] = _secret(
            f"ESXi password for {args.name}",
            args.password_env,
            args.password_stdin,
            entry.get("password"),
        )
    _merge_bmc(args, entry)
    _merge_wol(args, entry)
    build_host(entry)

    if index is None:
        hosts.append(entry)
    else:
        hosts[index] = entry
    data["hosts"] = hosts
    target.save(data)
    _say(f"Saved host {args.name}")
    if "bmc" not in entry and "wol" not in entry:
        _say("Warning: without --bmc-address or --wol-mac the agent cannot power this host on.")
    _after_change()
    return 0


# ------------------------------------------------------------------- proxmox


def cmd_proxmox_set(args: argparse.Namespace) -> int:
    """Set the Proxmox API token. This makes the agent a Proxmox agent."""
    target = ConfigFile(args.config)
    data = target.load()
    if _uses_vmware(data):
        _require_platform({"platform": "vmware"}, "proxmox")
    settings = dict(data.get("proxmox") or {})
    _apply(settings, args, ("token_id", "verify_ssl"))
    if "token_id" not in settings:
        raise ConfigError("--token-id is required, for example ha-hypervisor@pve!agent")
    if "!" not in settings["token_id"]:
        raise ConfigError("--token-id: use USER@REALM!TOKEN, for example ha-hypervisor@pve!agent")
    settings["token_secret"] = _secret(
        "Token secret", args.secret_env, args.secret_stdin, settings.get("token_secret")
    )
    build_proxmox(settings)
    data["platform"] = "proxmox"
    data["proxmox"] = settings
    target.save(data)
    _say(f"Saved Proxmox token {settings['token_id']}")
    if not data.get("hosts"):
        _say("Next: ha-hypervisor-agent node add NODE --address ADDRESS --bmc-address ...")
    _after_change()
    return 0


def cmd_node_test(args: argparse.Namespace) -> int:
    """Ask every node for its version, cluster, HA and power state."""
    from .config import load as load_config  # noqa: PLC0415
    from .pve import PveInfra, cluster_quorate  # noqa: PLC0415

    config = load_config(args.config)
    if config.proxmox is None:
        raise ConfigError("no Proxmox token, run: ha-hypervisor-agent proxmox set")
    selected = [host for host in config.hosts if args.name in (None, host.name)]
    if not selected:
        raise ConfigError("no such node, see: ha-hypervisor-agent node list")
    infra = PveInfra(config)
    failures = 0
    for host in selected:
        try:
            client = infra.node(host)
            version = client.version()
            status = client.cluster_status()
            guests = client.resources("vm")
            ha = client.ha_resources()
            nodes = [item.get("node") for item in client.resources("node")]
        except (VimUnavailable, VimError) as err:
            failures += 1
            _say(f"FAIL  Proxmox {host.name} ({host.address}): {err}")
        else:
            cluster = next((item for item in status if item.get("type") == "cluster"), None)
            if cluster is None:
                kind = "single node"
            else:
                quorum = "quorate" if cluster_quorate(status) else "NO QUORUM"
                kind = f"cluster {cluster.get('name')}, {quorum}"
            _say(f"OK    Proxmox {host.name}: {version}, {kind}")
            _say(f"      {len(guests)} guests, {len(ha)} HA resources")
            if host.name not in nodes:
                failures += 1
                _say(f"FAIL  {host.name} is not a node name here, the nodes are: {', '.join(nodes)}")
            for name in nodes:
                if config.host(name) is None:
                    _say(f"      node {name}: NOT configured, use node add")
        power = infra.power(host)
        methods = ", ".join(method.name for method in power.methods) or "none"
        if power.verified():
            _say(f"OK    Power {host.name}: {power.state()} (methods: {methods})")
        elif power.can_wake():
            _say(f"WARN  Power {host.name}: only Wake on LAN is left, it cannot be checked")
        else:
            failures += 1
            _say(f"FAIL  Power {host.name}: no working method ({methods})")
    return 1 if failures else 0


def cmd_esxi_list(args: argparse.Namespace) -> int:
    """List the hosts."""
    hosts = ConfigFile(args.config).load().get("hosts") or []
    if not hosts:
        _say("No hosts, run: ha-hypervisor-agent esxi add")
    for host in hosts:
        power = []
        if host.get("bmc"):
            power.append(f"bmc {host['bmc']['address']} ({host['bmc'].get('type', 'auto')})")
        if host.get("wol"):
            power.append(f"wol {host['wol']['mac']}")
        _say(f"{host['name']}  {host['address']}  {', '.join(power) or 'no power control'}")
    return 0


def cmd_esxi_remove(args: argparse.Namespace) -> int:
    """Remove a host."""
    target = ConfigFile(args.config)
    data = target.load()
    hosts = data.get("hosts") or []
    remaining = [host for host in hosts if host["name"] != args.name]
    if len(remaining) == len(hosts):
        raise ConfigError(f"{args.name} does not exist")
    data["hosts"] = remaining
    target.save(data)
    _say(f"Removed host {args.name}")
    _after_change()
    return 0


def cmd_esxi_test(args: argparse.Namespace) -> int:
    """Log in to the hosts and ask their BMCs for the power state."""
    hosts = ConfigFile(args.config).load().get("hosts") or []
    selected = [host for host in hosts if args.name in (None, host["name"])]
    if not selected:
        raise ConfigError("no such host, see: ha-hypervisor-agent esxi list")
    failures = 0
    for raw in selected:
        host = build_host(expand(raw))
        try:
            client = VimClient.connect(
                host.address, host.port, host.username, host.password, host.verify_ssl
            )
            inventory = client.collect(host.name)
        except (VimUnavailable, VimError) as err:
            failures += 1
            _say(f"FAIL  ESXi {host.name}: {err}")
        else:
            _say(f"OK    ESXi {host.name}: {len(inventory['vms'])} VMs")
            client.disconnect()
        power = HostPower.for_host(host)
        methods = ", ".join(method.name for method in power.methods) or "none"
        if power.verified():
            _say(f"OK    Power {host.name}: {power.state()} (methods: {methods})")
        elif power.can_wake():
            _say(f"WARN  Power {host.name}: only Wake on LAN is left, it cannot be checked")
        else:
            failures += 1
            _say(f"FAIL  Power {host.name}: no working method ({methods})")
    return 1 if failures else 0


# ----------------------------------------------------------------- run, check


def cmd_genkey(_args: argparse.Namespace) -> int:
    """Print a new shared key."""
    _say(generate_key())
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """Test every connection."""
    config = load(args.config)
    logging.basicConfig(level=logging.WARNING)
    return check(config)


def cmd_run(args: argparse.Namespace) -> int:
    """Run the agent."""
    config = load(args.config)
    logging.basicConfig(
        level=config.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("pyVmomi").setLevel(logging.WARNING)
    service = AgentService(config)
    signal.signal(signal.SIGTERM, lambda *_: service.stop())
    signal.signal(signal.SIGINT, lambda *_: service.stop())
    service.run()
    return 0


# -------------------------------------------------------------------- parser


def _password_options(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--password-stdin", action="store_true", help="read the password from standard input"
    )
    group.add_argument(
        "--password-env",
        type=_env_name,
        metavar="NAME",
        help="store a reference to this environment variable instead of the password",
    )


def _bool_option(parser: argparse.ArgumentParser, name: str, text: str) -> None:
    parser.add_argument(name, action=argparse.BooleanOptionalAction, default=None, help=text)


def build_parser() -> argparse.ArgumentParser:
    """Return the command line definition."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=DEFAULT_CONFIG, help="configuration file")

    parser = argparse.ArgumentParser(
        prog="ha-hypervisor-agent",
        description="Agent connecting hypervisors and server BMCs to Home Assistant over MQTT.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)

    for name, handler, text in (
        ("run", cmd_run, "run the agent"),
        ("check", cmd_check, "test all connections, nothing is changed"),
        ("show", cmd_show, "print the configuration without secrets"),
    ):
        commands.add_parser(name, parents=[common], help=text).set_defaults(handler=handler)
    commands.add_parser("genkey", help="print a new shared key").set_defaults(handler=cmd_genkey)

    init = commands.add_parser("init", parents=[common], help="create the configuration file")
    init.add_argument("--agent-id", default="lab", help="name of this environment")
    init.add_argument("--state-file", default=PACKAGE_STATE_FILE, help=argparse.SUPPRESS)
    init.add_argument("--force", action="store_true", help="replace an existing file")
    init.set_defaults(handler=cmd_init)

    key = commands.add_parser("key", help="shared key").add_subparsers(dest="action", required=True)
    key.add_parser("show", parents=[common], help="print the key").set_defaults(handler=cmd_key_show)
    key.add_parser("rotate", parents=[common], help="replace the key").set_defaults(handler=cmd_key_rotate)

    _add_mqtt(commands, common)
    _add_vcenter(commands, common)
    _add_esxi(commands, common)
    _add_proxmox(commands, common)
    return parser


def _add_mqtt(commands: Any, common: argparse.ArgumentParser) -> None:
    group = commands.add_parser("mqtt", help="broker connection").add_subparsers(
        dest="action", required=True
    )
    item = group.add_parser("set", parents=[common], help="set the broker connection")
    item.add_argument("--host", help="broker address")
    item.add_argument("--port", type=int, help="default depends on transport and TLS")
    item.add_argument("--transport", choices=("tcp", "websockets"))
    item.add_argument("--path", help="WebSocket path, default /mqtt")
    _bool_option(item, "--tls", "use TLS, default on")
    _bool_option(item, "--verify-tls", "verify the broker certificate, default on")
    item.add_argument("--ca-file", help="CA certificate of the broker")
    item.add_argument("--username")
    item.add_argument("--no-auth", action="store_true", help="remove the login")
    _password_options(item)
    item.set_defaults(handler=cmd_mqtt_set)
    group.add_parser("test", parents=[common], help="try to log in").set_defaults(handler=cmd_mqtt_test)


def _add_vcenter(commands: Any, common: argparse.ArgumentParser) -> None:
    group = commands.add_parser("vcenter", help="vCenter connection").add_subparsers(
        dest="action", required=True
    )
    item = group.add_parser("set", parents=[common], help="set the vCenter connection")
    item.add_argument("--host")
    item.add_argument("--port", type=int)
    item.add_argument("--username")
    _bool_option(item, "--verify-ssl", "verify the certificate, default off")
    item.add_argument("--vm-name", help="name of the vCenter VM when it runs on the managed hosts")
    item.add_argument("--no-vm-name", action="store_true", help="vCenter runs elsewhere")
    _password_options(item)
    item.set_defaults(handler=cmd_vcenter_set)
    group.add_parser("test", parents=[common], help="log in and list the hosts").set_defaults(
        handler=cmd_vcenter_test
    )


def _add_esxi(commands: Any, common: argparse.ArgumentParser) -> None:
    _add_hosts(commands, common, proxmox=False)


def _add_proxmox(commands: Any, common: argparse.ArgumentParser) -> None:
    group = commands.add_parser("proxmox", help="Proxmox VE API token").add_subparsers(
        dest="action", required=True
    )
    item = group.add_parser("set", parents=[common], help="set the API token")
    item.add_argument("--token-id", help="USER@REALM!TOKEN, for example ha-hypervisor@pve!agent")
    _bool_option(item, "--verify-ssl", "verify the node certificates, default off")
    secret = item.add_mutually_exclusive_group()
    secret.add_argument(
        "--secret-stdin", action="store_true", help="read the token secret from standard input"
    )
    secret.add_argument(
        "--secret-env",
        type=_env_name,
        metavar="NAME",
        help="store a reference to this environment variable instead of the secret",
    )
    item.set_defaults(handler=cmd_proxmox_set)
    test = group.add_parser("test", parents=[common], help="ask every node, nothing is changed")
    test.set_defaults(handler=cmd_node_test, name=None)
    _add_hosts(commands, common, proxmox=True)


def _add_hosts(commands: Any, common: argparse.ArgumentParser, *, proxmox: bool) -> None:
    noun = "node" if proxmox else "host"
    group = commands.add_parser(
        "node" if proxmox else "esxi", help="Proxmox nodes" if proxmox else "ESXi hosts"
    ).add_subparsers(dest="action", required=True)
    item = group.add_parser("add", parents=[common], help=f"add or change a {noun}")
    item.add_argument(
        "name", help="node name exactly as in Proxmox" if proxmox else "host name exactly as in vCenter"
    )
    item.add_argument("--update", action="store_true", help=f"change an existing {noun}")
    item.add_argument("--address", help="address used by the agent, default the name")
    item.add_argument("--port", type=int, help="default 8006" if proxmox else "default 443")
    if not proxmox:
        item.add_argument("--username")
        _bool_option(item, "--verify-ssl", "verify the certificate, default off")
        _password_options(item)

    bmc = item.add_argument_group("BMC (Redfish, IPMI)")
    bmc.add_argument("--bmc-address")
    bmc.add_argument("--bmc-username")
    bmc.add_argument("--bmc-type", choices=("auto", "redfish", "ipmi"))
    _bool_option(bmc, "--bmc-verify-ssl", "verify the certificate, default off")
    secret = bmc.add_mutually_exclusive_group()
    secret.add_argument(
        "--bmc-password-stdin",
        action="store_true",
        help="read the BMC password from standard input, after the ESXi one if any",
    )
    secret.add_argument("--bmc-password-env", type=_env_name, metavar="NAME")
    bmc.add_argument("--no-bmc", action="store_true", help="remove the BMC")

    wol = item.add_argument_group("Wake on LAN")
    wol.add_argument("--wol-mac")
    wol.add_argument("--wol-broadcast")
    wol.add_argument("--no-wol", action="store_true", help="remove Wake on LAN")
    item.set_defaults(handler=cmd_node_add if proxmox else cmd_esxi_add)

    group.add_parser("list", parents=[common], help=f"list the {noun}s").set_defaults(
        handler=cmd_esxi_list
    )
    remove = group.add_parser("remove", parents=[common], help=f"remove a {noun}")
    remove.add_argument("name")
    remove.set_defaults(handler=cmd_esxi_remove)
    test = group.add_parser("test", parents=[common], help="log in and ask the BMC")
    test.add_argument("name", nargs="?", help=f"only this {noun}")
    test.set_defaults(handler=cmd_node_test if proxmox else cmd_esxi_test)


def main(argv: list[str] | None = None) -> int:
    """Run the selected command."""
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except ConfigError as err:
        # 2 also tells systemd not to restart a service that cannot work.
        print(f"Configuration error: {err}", file=sys.stderr)  # noqa: T201
        return EXIT_CONFIG
