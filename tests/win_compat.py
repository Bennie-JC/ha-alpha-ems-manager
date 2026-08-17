"""Windows compatibility shim, loaded before the Home Assistant test plugin.

Home Assistant's ``homeassistant.runner`` imports ``fcntl`` at module scope, and
``pytest-homeassistant-custom-component`` imports ``runner`` while its own plugin
is being loaded. ``fcntl`` is POSIX-only, so on Windows the session dies during
plugin import -- before any ``conftest.py`` gets a chance to run, which is why
this lives in a ``-p``-loaded plugin instead.

Everything Home Assistant actually calls on ``fcntl`` sits on the Unix daemon
startup path that no test exercises, so an inert stub is sufficient. On Linux
and macOS (including CI) this module does nothing at all.
"""

from __future__ import annotations

import sys
import types

if sys.platform == "win32" and "fcntl" not in sys.modules:
    _stub = types.ModuleType("fcntl")

    # Values mirror the POSIX constants; nothing here is ever exercised, but
    # keeping them realistic avoids surprising an unexpected consumer.
    _stub.LOCK_SH = 1
    _stub.LOCK_EX = 2
    _stub.LOCK_NB = 4
    _stub.LOCK_UN = 8
    _stub.F_GETFL = 3
    _stub.F_SETFL = 4

    def _unsupported(*_args: object, **_kwargs: object) -> int:
        """Stand in for a file-control call that Windows cannot make."""
        return 0

    _stub.fcntl = _unsupported
    _stub.flock = _unsupported
    _stub.ioctl = _unsupported
    _stub.lockf = _unsupported

    sys.modules["fcntl"] = _stub

if sys.platform == "win32" and "resource" not in sys.modules:
    # ``homeassistant.util.resource`` raises the open-file-descriptor limit at
    # startup. Windows has no such limit to raise, and the test harness never
    # depends on the outcome.
    _resource = types.ModuleType("resource")
    _resource.RLIMIT_NOFILE = 7
    _resource.RLIM_INFINITY = -1

    def _getrlimit(_which: int) -> tuple[int, int]:
        """Report an already-generous limit so nothing tries to raise it."""
        return (1024, 1024)

    def _setrlimit(_which: int, _limits: tuple[int, int]) -> None:
        """Accept and ignore a limit change."""

    _resource.getrlimit = _getrlimit
    _resource.setrlimit = _setrlimit
    _resource.error = OSError

    sys.modules["resource"] = _resource
