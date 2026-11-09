"""Out-of-band host power: Redfish, then IPMI, then Wake on LAN."""

from __future__ import annotations

import logging
import re
import socket
from typing import Protocol

import requests
import urllib3

from .config import BmcConfig, HostConfig, WolConfig

_LOGGER = logging.getLogger(__name__)
_TIMEOUT = 15

POWER_ON = "on"
POWER_OFF = "off"
POWER_UNKNOWN = "unknown"


class PowerError(Exception):
    """A power method failed."""


class PowerMethod(Protocol):
    """One way of controlling the power of a server."""

    name: str

    def state(self) -> str:
        """Return on or off, or raise PowerError."""

    def power_on(self) -> None:
        """Turn the server on, or raise PowerError."""


class Redfish:
    """DMTF Redfish, available on current BMCs such as Supermicro X11 and newer."""

    name = "redfish"

    def __init__(self, config: BmcConfig) -> None:
        """Prepare an HTTP session."""
        self._base = f"https://{config.address}"
        self._session = requests.Session()
        self._session.auth = (config.username, config.password)
        self._session.verify = config.verify_ssl
        self._system: str | None = None
        if not config.verify_ssl:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    def _request(self, method: str, path: str, **kwargs: object) -> requests.Response:
        try:
            response = self._session.request(
                method, self._base + path, timeout=_TIMEOUT, **kwargs
            )
            response.raise_for_status()
        except requests.RequestException as err:
            raise PowerError(f"redfish {method} {path}: {err}") from err
        return response

    def _system_path(self) -> str:
        if self._system is None:
            try:
                members = self._request("GET", "/redfish/v1/Systems").json()["Members"]
                self._system = members[0]["@odata.id"]
            except (ValueError, KeyError, IndexError, TypeError) as err:
                raise PowerError(f"redfish: no system found: {err}") from err
        return self._system

    def state(self) -> str:
        """Return the PowerState of the system."""
        try:
            value = self._request("GET", self._system_path()).json()["PowerState"]
        except (ValueError, KeyError, TypeError) as err:
            raise PowerError(f"redfish: no power state: {err}") from err
        return POWER_ON if str(value).lower() == "on" else POWER_OFF

    def power_on(self) -> None:
        """Send a ComputerSystem.Reset with ResetType On."""
        self._request(
            "POST",
            f"{self._system_path()}/Actions/ComputerSystem.Reset",
            json={"ResetType": "On"},
        )


class Ipmi:
    """IPMI over LAN, the fallback for BMCs without usable Redfish."""

    name = "ipmi"

    def __init__(self, config: BmcConfig) -> None:
        """Keep the connection settings."""
        self._config = config

    def _command(self):  # noqa: ANN202
        from pyghmi.exceptions import IpmiException  # noqa: PLC0415
        from pyghmi.ipmi.command import Command  # noqa: PLC0415

        try:
            return Command(
                bmc=self._config.address,
                userid=self._config.username,
                password=self._config.password,
            )
        except (IpmiException, OSError) as err:
            raise PowerError(f"ipmi: {err}") from err

    def state(self) -> str:
        """Return the chassis power state."""
        from pyghmi.exceptions import IpmiException  # noqa: PLC0415

        try:
            value = self._command().get_power()["powerstate"]
        except (IpmiException, OSError, KeyError) as err:
            raise PowerError(f"ipmi: {err}") from err
        return POWER_ON if value == "on" else POWER_OFF

    def power_on(self) -> None:
        """Send a chassis power on."""
        from pyghmi.exceptions import IpmiException  # noqa: PLC0415

        try:
            self._command().set_power("on", wait=False)
        except (IpmiException, OSError) as err:
            raise PowerError(f"ipmi: {err}") from err


class WakeOnLan:
    """Magic packet. It cannot report anything, so state() always fails."""

    name = "wol"

    def __init__(self, config: WolConfig) -> None:
        """Build the packet."""
        self._config = config
        mac = bytes.fromhex(re.sub(r"[:-]", "", config.mac))
        self.packet = b"\xff" * 6 + mac * 16

    def state(self) -> str:
        """Wake on LAN has no way of reading the power state."""
        raise PowerError("wol: state is not available")

    def power_on(self) -> None:
        """Broadcast the magic packet a few times, UDP may lose one."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                for _ in range(3):
                    sock.sendto(
                        self.packet, (self._config.broadcast, self._config.port)
                    )
        except OSError as err:
            raise PowerError(f"wol: {err}") from err


class HostPower:
    """Tries the configured methods of one host in order."""

    def __init__(self, methods: list[PowerMethod]) -> None:
        """Keep the ordered methods."""
        self.methods = methods

    @classmethod
    def for_host(cls, host: HostConfig) -> HostPower:
        """Build the chain for a host: Redfish, IPMI, Wake on LAN."""
        methods: list[PowerMethod] = []
        if host.bmc is not None:
            if host.bmc.type in ("auto", "redfish"):
                methods.append(Redfish(host.bmc))
            if host.bmc.type in ("auto", "ipmi"):
                methods.append(Ipmi(host.bmc))
        if host.wol is not None:
            methods.append(WakeOnLan(host.wol))
        return cls(methods)

    def state(self) -> str:
        """Return the first answer, or unknown when no method can tell."""
        for method in self.methods:
            try:
                return method.state()
            except PowerError as err:
                _LOGGER.debug("%s", err)
        return POWER_UNKNOWN

    def can_wake(self) -> bool:
        """Return True when a later power-on has a chance of working.

        A BMC has to answer now. Wake on LAN cannot be checked, so its mere
        presence counts and the shutdown plan carries a warning.
        """
        return self.verified() or any(m.name == WakeOnLan.name for m in self.methods)

    def verified(self) -> bool:
        """Return True when a method that can report state has answered."""
        return self.state() != POWER_UNKNOWN

    def power_on(self) -> str:
        """Turn the host on and return the name of the method that worked."""
        errors = []
        for method in self.methods:
            try:
                method.power_on()
            except PowerError as err:
                _LOGGER.warning("Power on failed: %s", err)
                errors.append(str(err))
            else:
                return method.name
        raise PowerError("; ".join(errors) or "no power method configured")


def tcp_open(address: str, port: int = 443, timeout: float = 3) -> bool:
    """Return True when a TCP port accepts connections."""
    try:
        with socket.create_connection((address, port), timeout=timeout):
            return True
    except OSError:
        return False
