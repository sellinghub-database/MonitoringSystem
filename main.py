"""System Monitor Overlay — entry point."""

from __future__ import annotations

import os
import sys
import tkinter as tk
from typing import Any, Dict, List, Optional

from history import HistoryPoint, StatsHistory
from logutil import log, set_logging
from monitor import MetricsSnapshot, SystemMonitor
from settings import (
    SettingsWindow,
    app_dir,
    config_path,
    ensure_workdir,
    load_config,
    save_config,
    set_autostart,
)
from tray import TrayIcon, ensure_icon_file
from ui import OverlayWindow


class App:
    def __init__(self) -> None:
        paths = ensure_workdir()
        self.config = load_config()
        set_logging(bool(self.config.get("logging", True)))
        log(f"app start config={config_path()}")
        log(f"workdir init complete: {paths}")
        log(f"logging={bool(self.config.get('logging', True))} perf_mode={bool(self.config.get('perf_mode', True))}")

        try:
            set_autostart(bool(self.config.get("autostart")))
            log(f"autostart={bool(self.config.get('autostart'))}")
        except OSError as exc:
            log(f"autostart sync failed: {exc}")

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("System Monitor Overlay")

        icon_path = os.path.join(app_dir(), "icon.ico")
        try:
            ensure_icon_file(icon_path)
            if os.path.isfile(icon_path):
                self.root.iconbitmap(icon_path)
            log(f"icon={icon_path}")
        except Exception as exc:
            icon_path = None
            log(f"icon failed: {exc}")

        self._settings_win: Optional[SettingsWindow] = None
        self._pending: Optional[MetricsSnapshot] = None
        self._seeded_history = False

        refresh = int(self.config.get("refresh_ms", 2000))
        if self.config.get("perf_mode", True):
            refresh = max(refresh, 2000)

        hist_path = os.path.join(app_dir(), "stats_history.csv")
        self.history = StatsHistory(
            hist_path,
            interval_s=10,
            enabled=bool(self.config.get("history_enabled", True)),
        )
        log(f"history={hist_path} points={len(self.history.points)}")

        self.monitor = SystemMonitor(
            refresh_ms=refresh,
            on_update=self._on_metrics,
            perf_mode=bool(self.config.get("perf_mode", True)),
        )

        self.overlay = OverlayWindow(
            self.root,
            self.config,
            on_close=self.hide_overlay,
            on_drag_start=self._on_drag_start,
            on_drag_end=self._on_drag_end,
            history_points=self._history_series,
        )
        log("overlay ready")
        log(f"monitor ready refresh_ms={refresh}")

        self.tray = TrayIcon(
            on_show_hide=lambda: self.root.after(0, self.toggle_overlay),
            on_settings=lambda: self.root.after(0, self.open_settings),
            on_exit=lambda: self.root.after(0, self.exit_app),
            icon_path=icon_path,
        )
        log("tray ready")

        self.root.after(200, self._ui_tick)

    def _history_series(self, window_s: Optional[float] = None) -> List[HistoryPoint]:
        if window_s is None:
            window_s = float(self.config.get("chart_window_s", 300))
        return self.history.series(window_s=float(window_s))

    def _on_drag_start(self) -> None:
        if self.config.get("perf_mode", True):
            self.monitor.pause()

    def _on_drag_end(self) -> None:
        if self.config.get("perf_mode", True):
            self.monitor.resume(collect_now=True)

    def _on_metrics(self, snap: MetricsSnapshot) -> None:
        self._pending = snap
        try:
            if not self._seeded_history:
                self.history.force_append(snap)
                self._seeded_history = True
            else:
                self.history.maybe_append(snap)
        except Exception as exc:
            log(f"history append error: {exc}")

    def _ui_tick(self) -> None:
        snap = self._pending
        if snap is not None and not self.overlay.dragging:
            try:
                self.overlay.update_metrics(snap)
                log(
                    f"metrics cpu={snap.cpu_percent:.1f}% ram={snap.ram_percent:.1f}% "
                    f"procs={len(snap.processes_cpu)} tab={self.overlay._active_tab} "
                    f"hist={len(self.history.points)}"
                )
            except Exception as exc:
                log(f"update_metrics error: {exc}")
            self._pending = None
        self.root.after(300, self._ui_tick)

    def toggle_overlay(self) -> None:
        log("tray: show/hide")
        self.overlay.toggle_visibility()

    def hide_overlay(self) -> None:
        log("overlay close -> hide")
        self.overlay.hide()

    def open_settings(self) -> None:
        log("open settings")
        if self._settings_win is not None:
            try:
                if self._settings_win.win.winfo_exists():
                    self._settings_win.win.lift()
                    return
            except Exception:
                pass
        self._settings_win = SettingsWindow(self.root, self.config, on_save=self.apply_config)

    def apply_config(self, config: Dict[str, Any]) -> None:
        self.config = config
        set_logging(bool(config.get("logging", True)))
        save_config(config)
        self.overlay.apply_config(config)
        if hasattr(self.overlay, "_chart_window_s"):
            self.overlay._chart_window_s = int(config.get("chart_window_s", 300))
            if hasattr(self.overlay, "_paint_interval_btns"):
                self.overlay._paint_interval_btns()
        refresh = max(500, int(config.get("refresh_ms", 2000)))
        if config.get("perf_mode", True):
            refresh = max(refresh, 2000)
        self.monitor.refresh_ms = refresh
        self.monitor.perf_mode = bool(config.get("perf_mode", True))
        self.history.enabled = bool(config.get("history_enabled", True))
        log(f"config applied refresh_ms={refresh} perf_mode={self.monitor.perf_mode}")

    def exit_app(self) -> None:
        log("exit requested")
        try:
            self.history.compact()
        except Exception:
            pass
        try:
            self.monitor.stop()
        except Exception as exc:
            log(f"monitor stop error: {exc}")
        try:
            self.tray.stop()
        except Exception as exc:
            log(f"tray stop error: {exc}")
        try:
            self.overlay.destroy()
        except Exception as exc:
            log(f"overlay destroy error: {exc}")
        try:
            self.root.quit()
            self.root.destroy()
        except Exception as exc:
            log(f"root destroy error: {exc}")
        os._exit(0)

    def run(self) -> None:
        self.monitor.start()
        self.tray.start()
        log("mainloop start")
        self.root.mainloop()


def main() -> None:
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("SystemMonitorOverlay")
        except Exception:
            pass
    app = App()
    app.run()


if __name__ == "__main__":
    main()
