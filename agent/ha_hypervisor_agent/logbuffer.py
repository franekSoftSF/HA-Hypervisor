"""The last lines of the agent log, kept in memory for Home Assistant.

A failed shutdown should be explainable from Home Assistant, without SSH to
the agent machine. Passwords are never logged, so the lines can be sent as
they are.
"""

from __future__ import annotations

from collections import deque
import logging
import threading

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
CAPACITY = 2000


class LogBuffer(logging.Handler):
    """Keeps the newest formatted records, the oldest drop out."""

    def __init__(self, capacity: int = CAPACITY) -> None:
        """Start empty."""
        super().__init__()
        self._lines: deque[str] = deque(maxlen=capacity)
        self._guard = threading.Lock()
        self.setFormatter(logging.Formatter(FORMAT))

    def emit(self, record: logging.LogRecord) -> None:
        """Store one record, with its traceback when it has one."""
        try:
            text = self.format(record)
        except Exception:  # noqa: BLE001
            self.handleError(record)
            return
        with self._guard:
            self._lines.extend(text.splitlines())

    def lines(self, count: int) -> list[str]:
        """Return up to count newest lines, oldest first."""
        with self._guard:
            if count <= 0:
                return []
            return list(self._lines)[-count:]


BUFFER = LogBuffer()


def install() -> None:
    """Collect everything the root logger lets through."""
    root = logging.getLogger()
    if BUFFER not in root.handlers:
        root.addHandler(BUFFER)
