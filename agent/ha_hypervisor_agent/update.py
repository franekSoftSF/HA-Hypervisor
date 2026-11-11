"""Update of the installed package: from the GitHub releases or from a downloaded .deb.

Every download is checked against the .sha256 file published with the same
release. An update never interrupts a running operation: a cluster that is
half shut down would be left without its agent.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any

import requests

from . import __version__

PACKAGE = "ha-hypervisor-agent"
RELEASES = "https://api.github.com/repos/franekSoftSF/HA-Hypervisor/releases"
CACHE_DIR = Path("/var/cache/ha-hypervisor-agent")
STATE_DIR = Path("/var/lib/ha-hypervisor-agent")
TIMEOUT = 30
MAX_SIZE = 300 * 1024 * 1024
_CHUNK = 1024 * 1024
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_VERSION = re.compile(r"^v?\d+(\.\d+){0,3}$")


class UpdateError(Exception):
    """The update cannot be done, the message says why."""


@dataclass(frozen=True)
class Release:
    """One release with the package for this machine."""

    version: str
    deb_url: str
    sha256_url: str
    deb_name: str


def parse_version(value: str) -> tuple[int, ...]:
    """Return a comparable version, 'v0.9.1' and '0.9.1' are equal."""
    value = value.strip()
    if not _VERSION.match(value):
        raise UpdateError(f"not a version: {value}")
    return tuple(int(part) for part in value.lstrip("v").split("."))


def is_newer(candidate: str, current: str = __version__) -> bool:
    """Return True when candidate is a later version than current."""
    return parse_version(candidate) > parse_version(current)


def run_command(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """Run a program, replaced in tests."""
    return subprocess.run(command, text=True, check=False, capture_output=True, **kwargs)  # noqa: S603


def deb_arch() -> str:
    """Return the Debian architecture of this machine, amd64 or arm64."""
    if not shutil.which("dpkg"):
        raise UpdateError("dpkg is missing: updates work only for the .deb package")
    result = run_command(["dpkg", "--print-architecture"])
    if result.returncode != 0:
        raise UpdateError(f"dpkg --print-architecture failed: {result.stderr.strip()}")
    return result.stdout.strip()


def deb_fields(path: Path) -> dict[str, str]:
    """Return Package, Version and Architecture of a .deb file."""
    result = run_command(["dpkg-deb", "--field", str(path), "Package", "Version", "Architecture"])
    if result.returncode != 0:
        raise UpdateError(f"{path} is not a .deb package: {result.stderr.strip()}")
    fields = {}
    for line in result.stdout.splitlines():
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields


def running_operations(state_dir: Path | None = None) -> list[str]:
    """Return the agents whose state file says an operation is running."""
    state_dir = state_dir or STATE_DIR
    busy = []
    for path in sorted(state_dir.glob("*.state.json")) + sorted(state_dir.glob("state.json")):
        try:
            operation = json.loads(path.read_text(encoding="utf-8")).get("operation")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(operation, dict) and operation.get("status") == "running":
            name = "main" if path.name == "state.json" else path.name.split(".")[0]
            busy.append(f"{name} ({operation.get('kind')}, step {operation.get('step')})")
    return busy


class Updater:
    """Finds, downloads, checks and installs packages."""

    def __init__(self, source: str = RELEASES, arch: str | None = None) -> None:
        """Keep the release source; the architecture is asked from dpkg when needed."""
        self.source = source.rstrip("/")
        self._arch = arch
        self._session = requests.Session()
        self._session.headers.update(
            {"Accept": "application/vnd.github+json", "User-Agent": f"{PACKAGE}/{__version__}"}
        )

    @property
    def arch(self) -> str:
        """Return the Debian architecture, read once."""
        if self._arch is None:
            self._arch = deb_arch()
        return self._arch

    def release(self, version: str | None = None) -> Release:
        """Return the latest release, or the given one, with its package for this machine."""
        if version is not None:
            parse_version(version)
            url = f"{self.source}/tags/{version.lstrip('v')}"
        else:
            url = f"{self.source}/latest"
        try:
            response = self._session.get(url, timeout=TIMEOUT)
        except requests.RequestException as err:
            raise UpdateError(f"cannot reach {url}: {err}") from err
        if response.status_code == 404:
            raise UpdateError(f"no release {version}" if version else "no release published yet")
        if response.status_code != 200:
            raise UpdateError(f"{url} answered {response.status_code}")
        try:
            data = response.json()
            tag = str(data["tag_name"])
            assets = {item["name"]: item["browser_download_url"] for item in data.get("assets") or []}
        except (ValueError, KeyError, TypeError) as err:
            raise UpdateError(f"unexpected answer from {url}") from err
        parse_version(tag)
        name = f"{PACKAGE}_{tag.lstrip('v')}_{self.arch}.deb"
        if name not in assets or f"{name}.sha256" not in assets:
            raise UpdateError(
                f"release {tag} has no package {name} with its checksum, "
                "the build may still be running: try again in a few minutes"
            )
        return Release(tag.lstrip("v"), assets[name], assets[f"{name}.sha256"], name)

    def download(self, release: Release, directory: Path) -> Path:
        """Download the package and its checksum, return the verified file."""
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / release.deb_name
        expected = self._checksum(release.sha256_url)
        partial = target.with_name(target.name + ".part")
        digest = hashlib.sha256()
        size = 0
        try:
            with self._session.get(release.deb_url, stream=True, timeout=TIMEOUT) as response:
                if response.status_code != 200:
                    raise UpdateError(f"download answered {response.status_code}")
                with partial.open("wb") as stream:
                    for chunk in response.iter_content(_CHUNK):
                        size += len(chunk)
                        if size > MAX_SIZE:
                            raise UpdateError("the package is larger than expected, stopped")
                        digest.update(chunk)
                        stream.write(chunk)
        except requests.RequestException as err:
            partial.unlink(missing_ok=True)
            raise UpdateError(f"download failed: {err}") from err
        except UpdateError:
            partial.unlink(missing_ok=True)
            raise
        if digest.hexdigest() != expected:
            partial.unlink(missing_ok=True)
            raise UpdateError("checksum does not match, the download is damaged or altered")
        os.replace(partial, target)
        target.with_name(target.name + ".sha256").write_text(
            f"{expected}  {target.name}\n", encoding="utf-8"
        )
        return target

    def _checksum(self, url: str) -> str:
        try:
            response = self._session.get(url, timeout=TIMEOUT)
        except requests.RequestException as err:
            raise UpdateError(f"cannot download the checksum: {err}") from err
        if response.status_code != 200:
            raise UpdateError(f"checksum download answered {response.status_code}")
        return parse_checksum(response.text)


def parse_checksum(text: str) -> str:
    """Return the hash from a sha256sum line."""
    value = text.strip().split()[0] if text.strip() else ""
    if not _SHA256.match(value):
        raise UpdateError("the checksum file is not a sha256sum line")
    return value.lower()


def file_sha256(path: Path) -> str:
    """Return the SHA-256 of a file."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_file(path: Path, arch: str) -> tuple[str, bool]:
    """Check a downloaded package; return its version and whether a checksum was compared."""
    if not path.is_file():
        raise UpdateError(f"{path} does not exist")
    checked = False
    checksum = path.with_name(path.name + ".sha256")
    if checksum.is_file():
        if file_sha256(path) != parse_checksum(checksum.read_text(encoding="utf-8")):
            raise UpdateError(f"{path} does not match {checksum.name}, the file is damaged or altered")
        checked = True
    fields = deb_fields(path)
    if fields.get("Package") != PACKAGE:
        raise UpdateError(f"{path} is the package {fields.get('Package')}, not {PACKAGE}")
    if fields.get("Architecture") not in (arch, "all"):
        raise UpdateError(f"{path} is for {fields.get('Architecture')}, this machine is {arch}")
    version = fields.get("Version", "")
    parse_version(version)
    return version, checked


def install(path: Path) -> None:
    """Install the package; its scripts restart the running agents."""
    env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
    if shutil.which("apt-get"):
        command = [
            "apt-get", "install", "-y", "-q", "--reinstall", "--allow-downgrades", "--no-install-recommends",
            str(path.resolve()),
        ]
    else:
        command = ["dpkg", "-i", str(path.resolve())]
    result = run_command(command, env=env)
    if result.returncode != 0:
        output = (result.stderr or result.stdout).strip().splitlines()
        raise UpdateError("installation failed: " + (output[-1] if output else command[0]))
