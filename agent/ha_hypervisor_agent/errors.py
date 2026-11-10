"""Errors shared by the platform drivers, so the sequences need not care which one runs."""

from __future__ import annotations


class PlatformUnavailable(Exception):
    """The endpoint cannot be reached or refused the login."""


class PlatformAuthError(PlatformUnavailable):
    """The endpoint refused the credentials."""


class GuestNotFound(Exception):
    """The VM or container does not exist on this endpoint."""


class ToolsUnavailable(Exception):
    """A guest operation needs guest tools (VMware Tools), which are not running."""


class PlatformError(Exception):
    """An API call failed."""
