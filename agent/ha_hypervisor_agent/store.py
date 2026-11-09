"""Local agent memory: what must survive the cluster being powered off.

Holds no secrets. vCenter is down exactly when this data is needed, so the
VM placement and the vCenter host cannot be looked up at that moment.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)

_DEFAULT: dict[str, Any] = {
    "vms": {},  # vm id -> last known VM record
    "vcenter_host": None,  # host that ran the vCenter VM
    "retreat_clusters": [],  # cluster ids we put into vCLS Retreat Mode
    "active_scenarios": {},  # scenario id -> {"name": str, "vms": [ids]}
    "operation": None,  # last operation
}


class Store:
    """Small JSON document written atomically."""

    def __init__(self, path: str | Path) -> None:
        """Load the file, starting empty when it is missing or damaged."""
        self._path = Path(path)
        self._lock = threading.RLock()
        self._data = copy.deepcopy(_DEFAULT)
        try:
            loaded = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as err:
            _LOGGER.warning("Ignoring unreadable state file %s: %s", self._path, err)
            return
        if isinstance(loaded, dict):
            self._data.update({k: v for k, v in loaded.items() if k in _DEFAULT})

    def get(self, key: str) -> Any:
        """Return a copy of a value."""
        with self._lock:
            return copy.deepcopy(self._data[key])

    def set(self, key: str, value: Any) -> None:
        """Replace a value and save the file."""
        with self._lock:
            self._data[key] = copy.deepcopy(value)
            self._save()

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(self._path.suffix + ".tmp")
        temporary.write_text(json.dumps(self._data, indent=1), encoding="utf-8")
        os.replace(temporary, self._path)
