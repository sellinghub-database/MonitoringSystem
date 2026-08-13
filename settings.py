"""Config persistence, Settings window, and Windows autostart."""

from __future__ import annotations

import json
import os
import sys
import winreg
from copy import deepcopy
from typing import Any, Callable, Dict, Optional

import tkinter as tk
from tkinter import messagebox, ttk

APP_NAME = "SystemMonitorOverlay"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

DEFAULT_CONFIG: Dict[str, Any] = {
    "opacity": 0.85,
    "click_through": False,
    "autostart": False,
    "logging": True,
    "perf_mode": True,
    "history_enabled": True,
    "history_interval_s": 10,
    "chart_window_s": 300,
    "refresh_ms": 2000,
    "margin": 20,
    "width": 560,
    "height": 400,
    "pos_x": None,
    "pos_y": None,
    "thresholds": {
        "cpu_temp": 85,
        "gpu_temp": 85,
        "cpu_load": 90,
        "ram_usage": 90,
        "process_cpu": 50,
        "moderate": 70,
    },
}


def app_dir() -> str:
    """Writable directory next to exe (frozen) or project root."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def config_path() -> str:
    return os.path.join(app_dir(), "config.json")


def ensure_workdir() -> Dict[str, str]:
    """Create/verify working files next to the app. Returns paths."""
    from logutil import log
    from tray import ensure_icon_file

    root = app_dir()
    paths = {
        "dir": root,
        "config": config_path(),
        "history": os.path.join(root, "stats_history.csv"),
        "icon": os.path.join(root, "icon.ico"),
    }
    cfg = load_config()
    save_config(cfg)
    hist = paths["history"]
    if not os.path.isfile(hist) or os.path.getsize(hist) == 0:
        with open(hist, "w", encoding="utf-8", newline="") as f:
            f.write("t,cpu,ram,dsk,cpu_t,gpu_t,mb_t,ram_t\n")
    try:
        ensure_icon_file(paths["icon"])
    except Exception as exc:
        log(f"icon init failed: {exc}")
    for key, path in paths.items():
        exists = os.path.exists(path)
        log(f"workdir {key}={'OK' if exists else 'MISSING'} path={path}")
    return paths


def load_config() -> Dict[str, Any]:
    path = config_path()
    cfg = deepcopy(DEFAULT_CONFIG)
    if not os.path.isfile(path):
        save_config(cfg)
        return cfg
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            cfg.update({k: v for k, v in data.items() if k != "thresholds"})
            if isinstance(data.get("thresholds"), dict):
                cfg["thresholds"].update(data["thresholds"])
    except Exception:
        pass
    return cfg


def save_config(config: Dict[str, Any]) -> None:
    path = config_path()
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
    except Exception:
        pass


def autostart_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    script = os.path.join(app_dir(), "main.py")
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.isfile(pythonw):
        pythonw = sys.executable
    return f'"{pythonw}" "{script}"'


def is_autostart_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            winreg.QueryValueEx(key, APP_NAME)
            return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def set_autostart(enabled: bool) -> None:
    if enabled:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, autostart_command())
    else:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, APP_NAME)
        except FileNotFoundError:
            pass


class SettingsWindow:
    """Modal-ish settings dialog."""

    def __init__(
        self,
        parent: tk.Misc,
        config: Dict[str, Any],
        on_save: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        self.parent = parent
        self.config = deepcopy(config)
        self.on_save = on_save

        self.win = tk.Toplevel(parent)
        self.win.title("Settings — System Monitor")
        self.win.configure(bg="#141414")
        self.win.resizable(False, False)
        self.win.attributes("-topmost", True)
        self.win.geometry("340x520")
        self.win.transient(parent)

        pad = {"padx": 12, "pady": 4}
        fg = "#c9d1d9"
        bg = "#0f1419"

        self.win.configure(bg=bg)

        tk.Label(self.win, text="Appearance", bg=bg, fg=fg, font=("Segoe UI", 10, "bold")).pack(anchor="w", **pad)

        self.opacity_var = tk.DoubleVar(value=float(self.config.get("opacity", 0.85)))
        row = tk.Frame(self.win, bg=bg)
        row.pack(fill=tk.X, **pad)
        tk.Label(row, text="Opacity", bg=bg, fg=fg, font=("Segoe UI", 9)).pack(side=tk.LEFT)
        self.opacity_label = tk.Label(row, text=f"{self.opacity_var.get():.0%}", bg=bg, fg=fg, width=5)
        self.opacity_label.pack(side=tk.RIGHT)
        scale = ttk.Scale(
            self.win,
            from_=0.4,
            to=1.0,
            orient=tk.HORIZONTAL,
            variable=self.opacity_var,
            command=self._on_opacity,
        )
        scale.pack(fill=tk.X, padx=12, pady=2)

        self.click_var = tk.BooleanVar(value=bool(self.config.get("click_through")))
        tk.Checkbutton(
            self.win,
            text="Click-through (mouse passes through)",
            variable=self.click_var,
            bg=bg,
            fg=fg,
            selectcolor="#21262d",
            activebackground=bg,
            activeforeground=fg,
            font=("Segoe UI", 9),
        ).pack(anchor="w", **pad)

        self.autostart_var = tk.BooleanVar(value=bool(self.config.get("autostart")) or is_autostart_enabled())
        tk.Checkbutton(
            self.win,
            text="Autostart on Windows logon",
            variable=self.autostart_var,
            bg=bg,
            fg=fg,
            selectcolor="#21262d",
            activebackground=bg,
            activeforeground=fg,
            font=("Segoe UI", 9),
        ).pack(anchor="w", **pad)

        self.logging_var = tk.BooleanVar(value=bool(self.config.get("logging", True)))
        tk.Checkbutton(
            self.win,
            text="Logging (console) True/False",
            variable=self.logging_var,
            bg=bg,
            fg=fg,
            selectcolor="#21262d",
            activebackground=bg,
            activeforeground=fg,
            font=("Segoe UI", 9),
        ).pack(anchor="w", **pad)

        self.perf_var = tk.BooleanVar(value=bool(self.config.get("perf_mode", True)))
        tk.Checkbutton(
            self.win,
            text="Perf mode (pause on drag, slower refresh)",
            variable=self.perf_var,
            bg=bg,
            fg=fg,
            selectcolor="#21262d",
            activebackground=bg,
            activeforeground=fg,
            font=("Segoe UI", 9),
        ).pack(anchor="w", **pad)

        refresh_row = tk.Frame(self.win, bg=bg)
        refresh_row.pack(fill=tk.X, **pad)
        tk.Label(refresh_row, text="Refresh (sec)", bg=bg, fg=fg, font=("Segoe UI", 9)).pack(side=tk.LEFT)
        self.refresh_var = tk.DoubleVar(value=max(1.0, float(self.config.get("refresh_ms", 2000)) / 1000.0))
        self.refresh_label = tk.Label(refresh_row, text=f"{self.refresh_var.get():.1f}s", bg=bg, fg=fg, width=5)
        self.refresh_label.pack(side=tk.RIGHT)
        ttk.Scale(
            self.win,
            from_=1.0,
            to=5.0,
            orient=tk.HORIZONTAL,
            variable=self.refresh_var,
            command=self._on_refresh,
        ).pack(fill=tk.X, padx=12, pady=2)

        tk.Label(self.win, text="Alert thresholds", bg=bg, fg=fg, font=("Segoe UI", 10, "bold")).pack(
            anchor="w", padx=12, pady=(12, 4)
        )

        th = self.config.setdefault("thresholds", deepcopy(DEFAULT_CONFIG["thresholds"]))
        self.entries: Dict[str, tk.Entry] = {}
        fields = [
            ("cpu_temp", "CPU temp °C"),
            ("gpu_temp", "GPU temp °C"),
            ("cpu_load", "CPU load %"),
            ("ram_usage", "RAM usage %"),
            ("process_cpu", "Process CPU %"),
            ("moderate", "Moderate (yellow) %"),
        ]
        for key, label in fields:
            fr = tk.Frame(self.win, bg=bg)
            fr.pack(fill=tk.X, padx=12, pady=2)
            tk.Label(fr, text=label, bg=bg, fg=fg, font=("Segoe UI", 9), width=20, anchor="w").pack(side=tk.LEFT)
            ent = tk.Entry(fr, width=8, bg="#161b22", fg=fg, insertbackground=fg, relief=tk.FLAT)
            ent.insert(0, str(th.get(key, DEFAULT_CONFIG["thresholds"].get(key))))
            ent.pack(side=tk.RIGHT)
            self.entries[key] = ent

        btns = tk.Frame(self.win, bg=bg)
        btns.pack(fill=tk.X, padx=12, pady=16)
        tk.Button(btns, text="Cancel", command=self.win.destroy, width=10).pack(side=tk.RIGHT, padx=4)
        tk.Button(btns, text="Save", command=self._save, width=10, bg="#3d8bfd", fg="#fff").pack(side=tk.RIGHT)

        self.win.grab_set()
        self.win.focus_set()

    def _on_opacity(self, _value: str = "") -> None:
        self.opacity_label.configure(text=f"{self.opacity_var.get():.0%}")

    def _on_refresh(self, _value: str = "") -> None:
        self.refresh_label.configure(text=f"{self.refresh_var.get():.1f}s")

    def _save(self) -> None:
        try:
            th = {}
            for key, ent in self.entries.items():
                th[key] = float(ent.get().strip())
        except ValueError:
            messagebox.showerror("Invalid value", "Thresholds must be numbers.", parent=self.win)
            return

        self.config["opacity"] = round(float(self.opacity_var.get()), 2)
        self.config["click_through"] = bool(self.click_var.get())
        self.config["autostart"] = bool(self.autostart_var.get())
        self.config["logging"] = bool(self.logging_var.get())
        self.config["perf_mode"] = bool(self.perf_var.get())
        self.config["refresh_ms"] = int(round(float(self.refresh_var.get()) * 1000))
        self.config["thresholds"] = th

        try:
            set_autostart(self.config["autostart"])
        except OSError as exc:
            messagebox.showwarning("Autostart", f"Could not update registry:\n{exc}", parent=self.win)

        from logutil import log, set_logging

        set_logging(self.config["logging"])
        save_config(self.config)
        log(
            f"settings saved logging={self.config['logging']} "
            f"perf_mode={self.config['perf_mode']} refresh_ms={self.config['refresh_ms']}"
        )
        if self.on_save:
            self.on_save(self.config)
        self.win.destroy()
