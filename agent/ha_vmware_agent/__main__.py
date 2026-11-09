"""Command line: run the agent, test its connections or create a shared key."""

from __future__ import annotations

import argparse
import logging
import signal
import sys

from . import __version__
from .config import ConfigError, load
from .protocol import generate_key
from .service import AgentService, check


def main() -> int:
    """Run the selected command."""
    parser = argparse.ArgumentParser(prog="ha-vmware-agent")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name, text in (
        ("run", "run the agent"),
        ("check", "test vCenter, ESXi and BMC connections without changing anything"),
    ):
        command = commands.add_parser(name, help=text)
        command.add_argument("--config", default="/config/config.yaml")
    commands.add_parser("genkey", help="print a new shared key")
    args = parser.parse_args()

    if args.command == "genkey":
        print(generate_key())  # noqa: T201
        return 0

    try:
        config = load(args.config)
    except ConfigError as err:
        print(f"Configuration error: {err}", file=sys.stderr)  # noqa: T201
        return 2

    logging.basicConfig(
        level=config.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("pyVmomi").setLevel(logging.WARNING)
    if args.command == "check":
        return check(config)

    service = AgentService(config)
    signal.signal(signal.SIGTERM, lambda *_: service.stop())
    signal.signal(signal.SIGINT, lambda *_: service.stop())
    service.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
