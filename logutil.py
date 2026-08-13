"""Lightweight console logging helpers."""

from __future__ import annotations

import ctypes
import sys
from datetime import datetime
from typing import Optional

_enabled: bool = True
_console_allocated: bool = False


def is_logging_enabled() -> bool:
    return _enabled


def ensure_console() -> None:
    """Allocate a console for frozen EXE so print is visible."""
    global _console_allocated
    if _console_allocated or not getattr(sys, "frozen", False):
        return
    if sys.platform != "win32":
        return
    try:
        kernel32 = ctypes.windll.kernel32
        if kernel32.AllocConsole():
            sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace")  # noqa: SIM115
            sys.stderr = open("CONOUT$", "w", encoding="utf-8", errors="replace")  # noqa: SIM115
            _console_allocated = True
    except Exception:
        pass


def set_logging(enabled: bool) -> None:
    global _enabled
    _enabled = bool(enabled)
    if _enabled:
        ensure_console()


def log(msg: str) -> None:
    if not _enabled:
        return
    stamp = datetime.now().strftime("%H:%M:%S")
    try:
        print(f"[{stamp}] {msg}", flush=True)
    except Exception:
        pass
