"""System tray icon using pystray + Pillow."""

from __future__ import annotations

import os
import threading
from typing import Callable, Optional

from PIL import Image, ImageDraw
import pystray
from pystray import MenuItem as Item


def create_icon_image(size: int = 64) -> Image.Image:
    """Simple monitor-style tray/app icon."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    margin = size // 8
    # Screen body
    draw.rounded_rectangle(
        [margin, margin, size - margin, size - margin - size // 10],
        radius=size // 10,
        fill=(13, 13, 13, 255),
        outline=(0, 255, 136, 255),
        width=max(2, size // 32),
    )
    # Inner glow bar
    bar_y0 = size // 3
    bar_y1 = size // 2
    draw.rectangle(
        [margin + size // 6, bar_y0, size - margin - size // 6, bar_y1],
        fill=(0, 255, 136, 220),
    )
    # Stand
    stand_w = size // 5
    draw.rectangle(
        [size // 2 - stand_w // 2, size - margin - size // 10, size // 2 + stand_w // 2, size - margin // 2],
        fill=(30, 30, 30, 255),
    )
    return img


def ensure_icon_file(path: str) -> str:
    """Write icon.ico if missing; return path."""
    if os.path.isfile(path):
        return path
    img = create_icon_image(256)
    img.save(
        path,
        format="ICO",
        sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    return path


class TrayIcon:
    def __init__(
        self,
        on_show_hide: Callable[[], None],
        on_settings: Callable[[], None],
        on_exit: Callable[[], None],
        icon_path: Optional[str] = None,
    ) -> None:
        self.on_show_hide = on_show_hide
        self.on_settings = on_settings
        self.on_exit = on_exit
        if icon_path and os.path.isfile(icon_path):
            image = Image.open(icon_path)
        else:
            image = create_icon_image(64)

        menu = pystray.Menu(
            Item("Show / Hide", self._show_hide, default=True),
            Item("Settings", self._settings),
            pystray.Menu.SEPARATOR,
            Item("Exit", self._exit),
        )
        self.icon = pystray.Icon("SystemMonitorOverlay", image, "System Monitor Overlay", menu)
        self._thread: Optional[threading.Thread] = None

    def _show_hide(self, _icon=None, _item=None) -> None:
        self.on_show_hide()

    def _settings(self, _icon=None, _item=None) -> None:
        self.on_settings()

    def _exit(self, _icon=None, _item=None) -> None:
        self.on_exit()

    def start(self) -> None:
        self._thread = threading.Thread(target=self.icon.run, name="TrayIcon", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        try:
            self.icon.stop()
        except Exception:
            pass
