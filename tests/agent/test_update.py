"""Update from the releases or from a downloaded package, and the daily timer."""

from __future__ import annotations

import hashlib
import http.server
import json
import subprocess
import threading

import pytest

from ha_hypervisor_agent import __version__, cli, update

PACKAGE = b"not really a deb, dpkg-deb is replaced"


def bump(version, step=1):
    parts = [int(part) for part in version.split(".")]
    parts[-1] += step
    return ".".join(str(part) for part in parts)


NEWER = bump(__version__)


class Releases:
    """A tiny GitHub releases API with one release and its files."""

    def __init__(self):
        self.tag = NEWER
        self.package = PACKAGE
        self.checksum = None
        self.requests = []
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):  # noqa: N802
                server.requests.append(self.path)
                name = f"ha-hypervisor-agent_{server.tag}_amd64.deb"
                base = f"http://127.0.0.1:{server.port}"
                if self.path in ("/releases/latest", f"/releases/tags/{server.tag}"):
                    body = json.dumps(
                        {
                            "tag_name": server.tag,
                            "assets": [
                                {"name": name, "browser_download_url": f"{base}/files/{name}"},
                                {"name": f"{name}.sha256", "browser_download_url": f"{base}/files/{name}.sha256"},
                            ],
                        }
                    ).encode()
                elif self.path == f"/files/{name}":
                    body = server.package
                elif self.path == f"/files/{name}.sha256":
                    digest = server.checksum or hashlib.sha256(PACKAGE).hexdigest()
                    body = f"{digest}  {name}\n".encode()
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/releases"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()


@pytest.fixture
def releases():
    server = Releases()
    yield server
    server.httpd.shutdown()


@pytest.fixture
def system(monkeypatch, tmp_path):
    """Root on a Debian amd64 machine: dpkg, apt-get and systemctl are recorded."""
    calls = []
    fields = {"Package": "ha-hypervisor-agent", "Version": NEWER, "Architecture": "amd64"}
    enabled = {"value": "disabled"}

    def run(command, **_kwargs):
        calls.append(command)
        out, code = "", 0
        if command[:2] == ["dpkg", "--print-architecture"]:
            out = "amd64\n"
        elif command[0] == "dpkg-deb":
            out = "".join(f"{key}: {value}\n" for key, value in fields.items())
        elif command[:2] == ["systemctl", "is-enabled"]:
            out = enabled["value"] + "\n"
        elif command[:2] == ["systemctl", "enable"]:
            enabled["value"] = "enabled"
        return subprocess.CompletedProcess(command, code, out, "")

    monkeypatch.setattr(update, "run_command", run)
    monkeypatch.setattr(update.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(update, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(update, "STATE_DIR", tmp_path / "state")
    (tmp_path / "state").mkdir()
    return type("System", (), {"calls": calls, "fields": fields, "tmp": tmp_path, "enabled": enabled})


def installs(system):
    return [command for command in system.calls if command[:2] == ["apt-get", "install"]]


def test_check_reports_a_newer_release(releases, system, capsys):
    assert cli.main(["update", "--check", "--source", releases.url]) == cli.EXIT_UPDATE_AVAILABLE
    assert f"{__version__} -> {NEWER}" in capsys.readouterr().out
    releases.tag = __version__
    assert cli.main(["update", "--check", "--source", releases.url]) == 0
    assert "Up to date" in capsys.readouterr().out
    assert not installs(system)


def test_update_downloads_verifies_installs_and_cleans_up(releases, system, capsys):
    assert cli.main(["update", "--source", releases.url]) == 0
    (command,) = installs(system)
    assert command[-1].endswith(f"ha-hypervisor-agent_{NEWER}_amd64.deb")
    assert f"Updated to {NEWER}" in capsys.readouterr().out
    assert not list((system.tmp / "cache").iterdir())


def test_a_damaged_download_is_never_installed(releases, system, capsys):
    releases.checksum = "0" * 64
    assert cli.main(["update", "--source", releases.url]) == 1
    assert "checksum does not match" in capsys.readouterr().err
    assert not installs(system)
    assert not list((system.tmp / "cache").iterdir())


def test_same_or_older_needs_force(releases, system, capsys):
    releases.tag = __version__
    assert cli.main(["update", "--source", releases.url]) == 0
    assert "already installed" in capsys.readouterr().out
    assert not installs(system)
    system.fields["Version"] = __version__
    assert cli.main(["update", "--source", releases.url, "--force"]) == 0
    assert len(installs(system)) == 1


def test_a_specific_version(releases, system):
    older = bump(__version__, -1) if __version__.split(".")[-1] != "0" else "0.0.1"
    releases.tag = older
    system.fields["Version"] = older
    assert cli.main(["update", "--version", older, "--source", releases.url]) == 0
    assert not installs(system)  # a downgrade needs --force
    assert cli.main(["update", "--version", older, "--force", "--source", releases.url]) == 0
    assert releases.requests[0] == f"/releases/tags/{older}"
    assert len(installs(system)) == 1


def test_download_only_then_install_the_file(releases, system, capsys, tmp_path):
    target = tmp_path / "downloads"
    assert cli.main(["update", "--download-only", "--dir", str(target), "--source", releases.url]) == 0
    path = target / f"ha-hypervisor-agent_{NEWER}_amd64.deb"
    assert path.read_bytes() == PACKAGE
    assert (target / f"{path.name}.sha256").is_file()
    assert not installs(system)
    capsys.readouterr()

    assert cli.main(["update", "--file", str(path)]) == 0
    assert "Checksum OK" in capsys.readouterr().out
    assert installs(system)[-1][-1] == str(path.resolve())


def test_a_file_that_does_not_match_its_checksum(system, tmp_path, capsys):
    path = tmp_path / "ha-hypervisor-agent_9.9.9_amd64.deb"
    path.write_bytes(PACKAGE)
    (tmp_path / f"{path.name}.sha256").write_text("1" * 64 + f"  {path.name}\n")
    assert cli.main(["update", "--file", str(path)]) == 1
    assert "damaged or altered" in capsys.readouterr().err
    assert not installs(system)


def test_a_file_without_checksum_warns_and_wrong_package_is_refused(system, tmp_path, capsys):
    path = tmp_path / "package.deb"
    path.write_bytes(PACKAGE)
    assert cli.main(["update", "--file", str(path)]) == 0
    assert "checksum was not compared" in capsys.readouterr().out
    assert len(installs(system)) == 1
    system.fields["Package"] = "something-else"
    assert cli.main(["update", "--file", str(path)]) == 1
    assert "not ha-hypervisor-agent" in capsys.readouterr().err
    system.fields.update(Package="ha-hypervisor-agent", Architecture="arm64")
    assert cli.main(["update", "--file", str(path)]) == 1
    assert "this machine is amd64" in capsys.readouterr().err
    assert len(installs(system)) == 1


def test_a_running_operation_blocks_the_update(releases, system, capsys):
    state = system.tmp / "state" / "pve.state.json"
    state.write_text(json.dumps({"operation": {"status": "running", "kind": "cluster_shutdown", "step": "stop_vms"}}))
    assert cli.main(["update", "--source", releases.url]) == 1
    assert "pve (cluster_shutdown, step stop_vms)" in capsys.readouterr().err
    assert cli.main(["update", "--auto", "--source", releases.url]) == 0
    assert "next run tries again" in capsys.readouterr().out
    assert not installs(system)
    state.write_text(json.dumps({"operation": {"status": "completed"}}))
    assert cli.main(["update", "--auto", "--source", releases.url]) == 0
    assert len(installs(system)) == 1


def test_auto_never_reinstalls_or_downgrades(releases, system, capsys):
    releases.tag = __version__
    assert cli.main(["update", "--auto", "--force", "--source", releases.url]) == 0
    assert "Up to date" in capsys.readouterr().out
    assert not installs(system)
    assert cli.main(["update", "--auto", "--version", NEWER]) == 1


def test_install_needs_root(releases, system, monkeypatch, capsys):
    monkeypatch.setattr(cli.os, "geteuid", lambda: 1000, raising=False)
    assert cli.main(["update", "--source", releases.url]) == 1
    assert "needs root" in capsys.readouterr().err
    assert cli.main(["update", "--check", "--source", releases.url]) == cli.EXIT_UPDATE_AVAILABLE


def test_unreachable_source(system, capsys):
    assert cli.main(["update", "--check", "--source", "http://127.0.0.1:9/releases"]) == 1
    assert "cannot reach" in capsys.readouterr().err


def test_versions():
    assert update.is_newer("0.10.0", "0.9.9")
    assert update.is_newer("v1.0", "0.9.9")
    assert not update.is_newer("0.8.0", "0.8.0")
    with pytest.raises(update.UpdateError):
        update.parse_version("latest")


def test_autoupdate_timer(system, monkeypatch, tmp_path, capsys):
    schedule = tmp_path / "timer.d" / "schedule.conf"
    monkeypatch.setattr(cli, "_UPDATE_SCHEDULE", schedule)
    monkeypatch.setattr(cli, "_require_systemd", lambda: None)
    assert cli.main(["autoupdate", "status"]) == 0
    assert "off" in capsys.readouterr().out

    assert cli.main(["autoupdate", "enable", "--at", "05:15"]) == 0
    assert "daily at 05:15" in capsys.readouterr().out
    assert "OnCalendar=\nOnCalendar=*-*-* 05:15:00" in schedule.read_text()
    assert ["systemctl", "enable", "--now", "ha-hypervisor-agent-update.timer"] in system.calls
    assert cli.main(["autoupdate", "status"]) == 0
    assert "on, daily at 05:15" in capsys.readouterr().out

    assert cli.main(["autoupdate", "enable", "--at", "25:00"]) == 1
    assert cli.main(["autoupdate", "disable"]) == 0
    assert ["systemctl", "disable", "--now", "ha-hypervisor-agent-update.timer"] in system.calls
