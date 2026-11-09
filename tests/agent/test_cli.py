"""The commands that build the configuration file."""

from __future__ import annotations

import io
import stat
from types import SimpleNamespace

import pytest
import yaml

from ha_hypervisor_agent import cli
from ha_hypervisor_agent.config import load
from ha_hypervisor_agent.vim import VimUnavailable

SECRET = "s3cret-value"


@pytest.fixture
def path(tmp_path):
    return str(tmp_path / "etc" / "config.yaml")


@pytest.fixture
def stdin(monkeypatch):
    def feed(*lines):
        monkeypatch.setattr("sys.stdin", io.StringIO("".join(f"{line}\n" for line in lines)))

    feed()
    return feed


def run(path, *args):
    return cli.main([*args, "--config", path])


def data(path):
    with open(path, encoding="utf-8") as stream:
        return yaml.safe_load(stream)


@pytest.fixture
def ready(path, stdin):
    assert run(path, "init", "--agent-id", "lab") == 0
    stdin(SECRET)
    assert run(path, "mqtt", "set", "--host", "broker", "--username", "agent", "--password-stdin") == 0
    stdin(SECRET)
    assert run(path, "vcenter", "set", "--host", "vc", "--username", "u", "--password-stdin", "--vm-name", "vcsa") == 0
    stdin(SECRET, "bmc-secret")
    assert (
        run(
            path, "esxi", "add", "esxi01", "--username", "root", "--password-stdin",
            "--bmc-address", "10.0.0.2", "--bmc-username", "ADMIN", "--bmc-password-stdin",
        )
        == 0
    )
    return path


def test_full_setup_gives_a_valid_configuration(ready):
    config = load(ready)
    assert config.agent_id == "lab"
    assert config.mqtt.port == 8883
    assert config.vcenter.vm_name == "vcsa"
    assert config.hosts[0].address == "esxi01"
    assert config.hosts[0].password == SECRET
    assert config.hosts[0].bmc.password == "bmc-secret"
    assert config.state_file == "/var/lib/ha-hypervisor-agent/state.json"


def test_file_is_not_world_readable(ready):
    import os

    assert stat.S_IMODE(os.stat(ready).st_mode) == 0o640


def test_init_prints_the_key_and_refuses_to_overwrite(path, capsys):
    assert run(path, "init") == 0
    assert data(path)["shared_key"] in capsys.readouterr().out
    assert run(path, "init") == cli.EXIT_CONFIG
    first = data(path)["shared_key"]
    assert run(path, "init", "--force") == 0
    assert data(path)["shared_key"] != first


def test_commands_need_init(path, stdin):
    assert run(path, "mqtt", "set", "--host", "x") == cli.EXIT_CONFIG


def test_show_hides_secrets_and_lists_what_is_missing(path, stdin, capsys):
    run(path, "init")
    stdin(SECRET)
    run(path, "mqtt", "set", "--host", "broker", "--username", "u", "--password-stdin")
    capsys.readouterr()

    assert run(path, "show") == 0
    out = capsys.readouterr().out
    assert SECRET not in out
    assert data(path)["shared_key"] not in out
    assert "vcenter set" in out
    assert "esxi add" in out
    assert "mqtt set" not in out


def test_show_keeps_environment_references_visible(path, capsys):
    run(path, "init")
    run(path, "vcenter", "set", "--host", "vc", "--username", "u", "--password-env", "VC_PASS")
    capsys.readouterr()
    run(path, "show")
    assert "${VC_PASS}" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("arguments", "port"),
    [
        (["--no-tls"], 1883),
        ([], 8883),
        (["--transport", "websockets"], 443),
        (["--transport", "websockets", "--no-tls"], 1884),
        (["--port", "9000"], 9000),
    ],
)
def test_mqtt_default_ports(path, arguments, port):
    run(path, "init")
    assert run(path, "mqtt", "set", "--host", "b", *arguments) == 0
    assert data(path)["mqtt"]["port"] == port


def test_mqtt_needs_a_password_for_a_login(path, stdin):
    run(path, "init")
    assert run(path, "mqtt", "set", "--host", "b", "--username", "u") == cli.EXIT_CONFIG
    assert "mqtt" not in data(path)


def test_mqtt_change_keeps_the_password(ready):
    assert run(ready, "mqtt", "set", "--port", "1234") == 0
    settings = data(ready)["mqtt"]
    assert settings["port"] == 1234
    assert settings["password"] == SECRET


def test_mqtt_no_auth_removes_the_login(ready):
    assert run(ready, "mqtt", "set", "--no-auth") == 0
    assert "username" not in data(ready)["mqtt"]
    assert "password" not in data(ready)["mqtt"]


def test_password_prompt_on_a_terminal(path, monkeypatch):
    run(path, "init")
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(cli.getpass, "getpass", lambda _prompt: "typed")
    assert run(path, "vcenter", "set", "--host", "vc", "--username", "u") == 0
    assert data(path)["vcenter"]["password"] == "typed"


def test_empty_prompt_keeps_the_password(ready, monkeypatch):
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(cli.getpass, "getpass", lambda _prompt: "")
    assert run(ready, "vcenter", "set", "--port", "8443") == 0
    assert data(ready)["vcenter"]["password"] == SECRET


def test_password_is_not_accepted_as_an_argument(path):
    run(path, "init")
    with pytest.raises(SystemExit):
        cli.main(["vcenter", "set", "--host", "v", "--username", "u", "--password", "x", "--config", path])


def test_environment_reference_is_stored_not_resolved(path, monkeypatch):
    run(path, "init")
    monkeypatch.setenv("VC_PASS", "real")
    run(path, "vcenter", "set", "--host", "vc", "--username", "u", "--password-env", "VC_PASS")
    assert data(path)["vcenter"]["password"] == "${VC_PASS}"


def test_esxi_duplicate_needs_update(ready, stdin):
    stdin(SECRET)
    assert run(ready, "esxi", "add", "esxi01", "--username", "x", "--password-stdin") == cli.EXIT_CONFIG
    assert data(ready)["hosts"][0]["username"] == "root"


def test_esxi_update_changes_only_what_is_given(ready):
    assert run(ready, "esxi", "add", "esxi01", "--update", "--address", "10.0.0.9") == 0
    host = data(ready)["hosts"][0]
    assert host["address"] == "10.0.0.9"
    assert host["password"] == SECRET
    assert host["bmc"]["password"] == "bmc-secret"


def test_esxi_update_of_unknown_host(ready):
    assert run(ready, "esxi", "add", "nope", "--update") == cli.EXIT_CONFIG


def test_esxi_invalid_mac_changes_nothing(ready, stdin):
    before = data(ready)
    stdin(SECRET)
    assert (
        run(ready, "esxi", "add", "esxi02", "--username", "r", "--password-stdin", "--wol-mac", "zz")
        == cli.EXIT_CONFIG
    )
    assert data(ready) == before


def test_esxi_wol_and_removing_power_methods(ready):
    assert run(ready, "esxi", "add", "esxi01", "--update", "--wol-mac", "00:11:22:33:44:55", "--no-bmc") == 0
    host = data(ready)["hosts"][0]
    assert host["wol"]["mac"] == "00:11:22:33:44:55"
    assert "bmc" not in host
    assert run(ready, "esxi", "add", "esxi01", "--update", "--no-wol") == 0
    assert "wol" not in data(ready)["hosts"][0]


def test_esxi_without_power_control_is_warned(path, stdin, capsys):
    run(path, "init")
    stdin(SECRET)
    run(path, "esxi", "add", "h1", "--username", "r", "--password-stdin")
    assert "cannot power this host on" in capsys.readouterr().out


def test_esxi_list_and_remove(ready, capsys):
    capsys.readouterr()
    assert run(ready, "esxi", "list") == 0
    out = capsys.readouterr().out
    assert "esxi01" in out
    assert "10.0.0.2" in out
    assert SECRET not in out
    assert run(ready, "esxi", "remove", "esxi01") == 0
    assert data(ready)["hosts"] == []
    assert run(ready, "esxi", "remove", "esxi01") == cli.EXIT_CONFIG


def test_key_show_and_rotate(ready, capsys):
    first = data(ready)["shared_key"]
    capsys.readouterr()
    run(ready, "key", "show")
    assert capsys.readouterr().out.strip() == first
    run(ready, "key", "rotate")
    assert data(ready)["shared_key"] != first


def test_mqtt_test_reports_the_problem(ready, monkeypatch, capsys):
    monkeypatch.setattr(cli, "test_mqtt", lambda *_: "Not authorized")
    assert run(ready, "mqtt", "test") == 1
    assert "Not authorized" in capsys.readouterr().out
    monkeypatch.setattr(cli, "test_mqtt", lambda *_: None)
    assert run(ready, "mqtt", "test") == 0


def _fake_client(host_names):
    return SimpleNamespace(
        version="VMware vCenter 8",
        collect=lambda _name=None: {
            "hosts": [{"name": name} for name in host_names],
            "vms": [{}],
        },
        disconnect=lambda: None,
    )


def test_vcenter_test_points_out_unconfigured_hosts(ready, monkeypatch, capsys):
    monkeypatch.setattr(cli.VimClient, "connect", lambda *_: _fake_client(["esxi01", "esxi02"]))
    assert run(ready, "vcenter", "test") == 0
    out = capsys.readouterr().out
    assert "esxi01: configured" in out
    assert "esxi02: NOT configured" in out


def test_vcenter_test_failure(ready, monkeypatch, capsys):
    def refuse(*_):
        raise VimUnavailable("refused")

    monkeypatch.setattr(cli.VimClient, "connect", refuse)
    assert run(ready, "vcenter", "test") == 1
    assert "refused" in capsys.readouterr().out


def test_esxi_test_reads_environment_secrets(path, monkeypatch, capsys):
    seen = {}

    def connect(_address, _port, _user, password, _verify):
        seen["password"] = password
        return _fake_client(["h1"])

    run(path, "init")
    run(path, "esxi", "add", "h1", "--username", "r", "--password-env", "ESX_PASS")
    monkeypatch.setenv("ESX_PASS", "real")
    monkeypatch.setattr(cli.VimClient, "connect", connect)
    assert run(path, "esxi", "test") == 1  # no way to power it on
    assert seen["password"] == "real"
    assert "OK    ESXi h1" in capsys.readouterr().out
