"""Print what the pytest plugin needs, keeping every package Home Assistant pinned."""

import importlib.metadata as metadata
import re

for requirement in metadata.requires("pytest-homeassistant-custom-component") or []:
    if "extra ==" in requirement:
        continue
    name = re.match(r"[A-Za-z0-9._-]+", requirement).group(0)
    try:
        metadata.version(name)
    except metadata.PackageNotFoundError:
        print(requirement)
