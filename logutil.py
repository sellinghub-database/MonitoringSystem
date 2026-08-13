"""File-based logging (no AllocConsole for frozen EXE)."""

from __future__ import annotations

import os
import sys
from datetime import datetime
from typing import Optional

_enabled: bool = True
_log_path: Optional[str] = None


def is_logging_enabled() -> bool:
    return _enabled


def set_log_path(path: str) -> None:
    """Set append log file path (e.g. %LOCALAPPDATA%\\SystemMonitoring\\app.log)."""
    global _log_path
    _log_path = path
    parent = os.path.dirname(path)
    if parent:
        try:
            os.makedirs(parent, exist_ok=True)
        except Exception:
            pass


def ensure_console() -> None:
    """No-op: frozen EXE must not allocate a console window."""
    return


def set_logging(enabled: bool) -> None:
    global _enabled
    _enabled = bool(enabled)


def log(msg: str) -> None:
    if not _enabled:
        return
    stamp = datetime.now().strftime("%H:%M:%S")
    line = f"[{stamp}] {msg}"
    if _log_path:
        try:
            with open(_log_path, "a", encoding="utf-8", errors="replace") as f:
                f.write(line + "\n")
        except Exception:
            pass
    # Dev: also print; frozen EXE has no console
    if not getattr(sys, "frozen", False):
        try:
            print(line, flush=True)
        except Exception:
            pass
