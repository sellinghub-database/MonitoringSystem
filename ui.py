"""Frameless always-on-top system monitor overlay (tabs + Treeview)."""

from __future__ import annotations

import os
import time
import ctypes
from ctypes import wintypes
from typing import Any, Callable, Dict, List, Optional

import tkinter as tk
from tkinter import font as tkfont
from tkinter import messagebox, ttk

from logutil import log
from monitor import MetricsSnapshot, terminate_process
from icons import IconCache
from chart import TempChart
from history import HistoryPoint

# Slate theme
BG = "#0f1419"
PANEL = "#161b22"
BORDER = "#21262d"
FG = "#c9d1d9"
MUTED = "#8b949e"
ACCENT = "#3d8bfd"
WARN = "#d29922"
CRIT = "#f85149"
OK = "#3fb950"
ROW_ALT = "#12181f"

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_ROUND = 2

user32 = ctypes.windll.user32
dwmapi = ctypes.windll.dwmapi

TABS = ("CPU", "RAM", "Disk", "Показатели")


def _hwnd(window: tk.Misc) -> int:
    window.update_idletasks()
    return int(window.winfo_id())


def apply_toolwindow(hwnd: int) -> None:
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    style = (style | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)


def set_click_through_hwnd(hwnd: int, enabled: bool) -> None:
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    style |= WS_EX_TOOLWINDOW
    if enabled:
        style |= WS_EX_LAYERED | WS_EX_TRANSPARENT
    else:
        style &= ~WS_EX_TRANSPARENT
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)


def apply_rounded_corners(hwnd: int) -> None:
    try:
        preference = ctypes.c_int(DWMWCP_ROUND)
        dwmapi.DwmSetWindowAttribute(
            wintypes.HWND(hwnd),
            ctypes.c_int(DWMWA_WINDOW_CORNER_PREFERENCE),
            ctypes.byref(preference),
            ctypes.sizeof(preference),
        )
    except Exception:
        pass


def metric_color(value: Optional[float], moderate: float = 70, critical: float = 90) -> str:
    if value is None:
        return MUTED
    if value >= critical:
        return CRIT
    if value >= moderate:
        return WARN
    return OK


def load_band_colors(pct: float, active: bool = False) -> tuple[str, str]:
    """Soft load palette: bg, fg for tab chips."""
    p = max(0.0, min(100.0, float(pct)))
    if p < 25:
        bg, fg = "#2a4a6b", "#9ec3e6"
        if active:
            bg, fg = "#355a7d", "#c5ddf2"
    elif p < 50:
        bg, fg = "#3a4a5c", "#b8c4d0"
        if active:
            bg, fg = "#46586c", "#d0dae4"
    elif p < 75:
        bg, fg = "#6b4a2a", "#e0b48a"
        if active:
            bg, fg = "#7d5835", "#efc9a5"
    else:
        bg, fg = "#6b2a2a", "#e09a9a"
        if active:
            bg, fg = "#7d3535", "#efb0b0"
    return bg, fg


COLLAPSED_HEIGHT = 60
SNAP_PX = 12
POS_IDLE_MS = 30000


class OverlayWindow:
    """Tabbed slate monitoring widget."""

    def __init__(
        self,
        root: tk.Tk,
        config: Dict[str, Any],
        on_close: Optional[Callable[[], None]] = None,
        on_drag_start: Optional[Callable[[], None]] = None,
        on_drag_end: Optional[Callable[[], None]] = None,
        history_points: Optional[Callable[[], List]] = None,
    ) -> None:
        self.root = root
        self.config = config
        self.on_close = on_close
        self.on_drag_start = on_drag_start
        self.on_drag_end = on_drag_end
        self._history_points = history_points
        self._visible = True
        self._dragging = False
        self._drag_x = 0
        self._drag_y = 0
        self._active_tab = "CPU"
        self._latest: Optional[MetricsSnapshot] = None
        self._tree_pids: Dict[str, List[str]] = {"CPU": [], "RAM": [], "Disk": []}
        self._rebuild_counter = 0
        self._pulse_on = False
        self._pulse_job: Optional[str] = None
        self._alert_active = False
        self._tab_percents: Dict[str, float] = {t: 0.0 for t in TABS}
        self._kill_row: Optional[str] = None
        self._self_pid = os.getpid()
        self._icon_cache: Optional[IconCache] = None
        self._collapsed = False
        self._full_height = int(config.get("height", 400))
        self._pos_save_job: Optional[str] = None
        self._pending_pos: Optional[tuple[int, int]] = None

        width = int(config.get("width", 560))
        height = int(config.get("height", 400))
        margin = int(config.get("margin", 20))
        self._chart_window_s = int(config.get("chart_window_s", 300))
        self._full_height = height

        self.win = tk.Toplevel(root)
        self.win.title("System Monitor Overlay")
        self.win.configure(bg=BG)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.attributes("-alpha", float(config.get("opacity", 0.85)))

        sw = self.win.winfo_screenwidth()
        sh = self.win.winfo_screenheight()
        pos_x = config.get("pos_x")
        pos_y = config.get("pos_y")
        if pos_x is not None and pos_y is not None:
            try:
                x, y = int(pos_x), int(pos_y)
            except (TypeError, ValueError):
                x, y = sw - width - margin, margin
        else:
            x, y = sw - width - margin, margin
        x, y = self._clamp_pos(x, y, width, height, sw, sh)
        self.win.geometry(f"{width}x{height}+{x}+{y}")
        self.win.protocol("WM_DELETE_WINDOW", self._handle_close)

        self._font = tkfont.Font(family="Segoe UI", size=9)
        self._font_b = tkfont.Font(family="Segoe UI", size=9, weight="bold")
        self._font_mono = tkfont.Font(family="Consolas", size=8)
        self._font_title = tkfont.Font(family="Segoe UI", size=10, weight="bold")
        self._font_tab = tkfont.Font(family="Segoe UI", size=8)

        self._icon_cache = IconCache(self.win, max_items=160, size=16)
        self._setup_style()
        self._build_ui()
        self.win.update_idletasks()
        self._hwnd = _hwnd(self.win)
        apply_toolwindow(self._hwnd)
        apply_rounded_corners(self._hwnd)
        if config.get("click_through"):
            set_click_through_hwnd(self._hwnd, True)
        self._restore_alpha()
        log("overlay window created")
        self.win.after(100, self._reapply_win32)

    def _setup_style(self) -> None:
        style = ttk.Style(self.win)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(
            "Monitor.Treeview",
            background=PANEL,
            foreground=FG,
            fieldbackground=PANEL,
            borderwidth=0,
            relief="flat",
            rowheight=28,
            font=("Consolas", 8),
            focuscolor=PANEL,
        )
        style.configure(
            "Monitor.Treeview.Heading",
            background=BORDER,
            foreground=MUTED,
            relief="flat",
            borderwidth=0,
            font=("Segoe UI", 8, "bold"),
            focuscolor=BORDER,
        )
        style.map(
            "Monitor.Treeview",
            background=[("selected", "#1f6feb")],
            foreground=[("selected", "#ffffff")],
            focuscolor=[("!focus", PANEL), ("focus", PANEL), ("selected", "#1f6feb")],
            bordercolor=[("focus", PANEL), ("!focus", PANEL)],
            lightcolor=[("focus", PANEL), ("!focus", PANEL)],
            darkcolor=[("focus", PANEL), ("!focus", PANEL)],
        )
        style.configure(
            "Monitor.Vertical.TScrollbar",
            background=BORDER,
            troughcolor=BG,
            arrowcolor=MUTED,
            bordercolor=BG,
            darkcolor=BORDER,
            lightcolor=BORDER,
            relief="flat",
            borderwidth=0,
        )

    def _restore_alpha(self) -> None:
        opacity = float(self.config.get("opacity", 0.85))
        try:
            self.win.attributes("-alpha", opacity)
        except Exception as exc:
            log(f"restore alpha failed: {exc}")

    def _reapply_win32(self) -> None:
        try:
            self._hwnd = _hwnd(self.win)
            apply_toolwindow(self._hwnd)
            apply_rounded_corners(self._hwnd)
            set_click_through_hwnd(self._hwnd, bool(self.config.get("click_through")))
            self._restore_alpha()
            log("win32 styles reapplied")
        except Exception as exc:
            log(f"win32 reapply failed: {exc}")

    def _handle_close(self) -> None:
        if self.on_close:
            self.on_close()
        else:
            self.hide()

    def _build_ui(self) -> None:
        self.border = tk.Frame(self.win, bg=BORDER, bd=0, highlightthickness=1, highlightbackground=BORDER)
        self.border.pack(fill=tk.BOTH, expand=True)

        inner = tk.Frame(self.border, bg=BG)
        inner.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)

        # Header
        header = tk.Frame(inner, bg=BG)
        header.pack(fill=tk.X, padx=8, pady=(8, 4))
        title = tk.Label(header, text="SYSTEM MONITOR", bg=BG, fg=FG, font=self._font_title)
        title.pack(side=tk.LEFT)
        for w in (header, title):
            w.bind("<Button-1>", self._start_drag)
            w.bind("<B1-Motion>", self._on_drag)
            w.bind("<ButtonRelease-1>", self._end_drag)

        btn_box = tk.Frame(header, bg=BG)
        btn_box.pack(side=tk.RIGHT)
        self.copy_btn = tk.Label(
            btn_box, text="⎘", bg=BG, fg=ACCENT, font=("Segoe UI", 12), cursor="hand2", padx=6
        )
        self.copy_btn.pack(side=tk.LEFT)
        self.copy_btn.bind("<Button-1>", lambda _e: self.copy_stats())
        self.min_btn = tk.Label(
            btn_box, text="−", bg=BG, fg=MUTED, font=("Segoe UI", 12), cursor="hand2", padx=6
        )
        self.min_btn.pack(side=tk.LEFT)
        self.min_btn.bind("<Button-1>", lambda _e: self.toggle_collapse())
        self.close_btn = tk.Label(
            btn_box, text="×", bg=BG, fg=MUTED, font=("Segoe UI", 12), cursor="hand2", padx=6
        )
        self.close_btn.pack(side=tk.LEFT)
        self.close_btn.bind("<Button-1>", lambda _e: self._handle_close())

        # Tabs
        tab_bar = tk.Frame(inner, bg=BG)
        tab_bar.pack(fill=tk.X, padx=6, pady=(2, 4))
        self._inner = inner
        self._header = header
        self._tab_bar = tab_bar
        self._tab_btns: Dict[str, tk.Label] = {}
        for name in TABS:
            lbl = tk.Label(
                tab_bar,
                text=name,
                bg=PANEL,
                fg=MUTED,
                font=self._font_tab,
                padx=8,
                pady=4,
                cursor="hand2",
            )
            lbl.pack(side=tk.LEFT, padx=2)
            lbl.bind("<Button-1>", lambda _e, n=name: self._on_tab_click(n))
            self._tab_btns[name] = lbl
        self._paint_tabs()

        # Body
        self.body = tk.Frame(inner, bg=BG)
        self.body.pack(fill=tk.BOTH, expand=True, padx=6, pady=(0, 6))

        self.table_frames: Dict[str, tk.Frame] = {}
        self.trees: Dict[str, ttk.Treeview] = {}

        self.table_frames["CPU"] = self._make_tree_tab(
            "CPU", ("pid", "cpu", "type"), ("PID", "CPU%", "Type"), (70, 70, 50), name_width=300
        )
        self.table_frames["RAM"] = self._make_tree_tab(
            "RAM", ("pid", "ram", "type"), ("PID", "MB", "Type"), (70, 70, 50), name_width=300
        )
        self.table_frames["Disk"] = self._make_tree_tab(
            "Disk",
            ("pid", "r", "w", "type"),
            ("PID", "R", "W", "Type"),
            (60, 55, 55, 45),
            name_width=240,
        )

        # Metrics tab
        self.metrics_frame = tk.Frame(self.body, bg=BG)
        self.table_frames["Показатели"] = self.metrics_frame
        self._build_metrics_panel(self.metrics_frame)

        # Soft kill button
        self.kill_btn = tk.Label(
            self.body,
            text="×",
            bg=PANEL,
            fg=MUTED,
            font=("Segoe UI", 10),
            cursor="hand2",
            padx=2,
            pady=0,
        )
        self.kill_btn.bind("<Button-1>", self._on_kill_click)
        self.kill_btn.bind("<Enter>", self._on_kill_enter)
        self.kill_btn.bind("<Leave>", self._on_kill_leave)
        self.kill_btn.place_forget()
        self._hide_kill_job: Optional[str] = None

        self._switch_tab("CPU")

    def _on_kill_enter(self, _e: tk.Event) -> None:
        self._cancel_hide_kill()
        self.kill_btn.configure(fg=CRIT, bg=BORDER)

    def _on_kill_leave(self, _e: tk.Event) -> None:
        self.kill_btn.configure(fg=MUTED, bg=PANEL)
        self._schedule_hide_kill()

    def _make_tree_tab(
        self,
        key: str,
        columns: tuple,
        headings: tuple,
        widths: tuple,
        name_width: int = 280,
    ) -> tk.Frame:
        frame = tk.Frame(self.body, bg=BG, highlightthickness=0, bd=0)
        tree = ttk.Treeview(
            frame,
            columns=columns,
            show="tree headings",
            style="Monitor.Treeview",
            selectmode="browse",
            takefocus=0,
        )
        tree.heading("#0", text="Name")
        tree.column("#0", width=name_width, minwidth=120, stretch=True, anchor="w")
        for col, head, width in zip(columns, headings, widths):
            tree.heading(col, text=head)
            anchor = "e"
            if col == "type":
                anchor = "center"
            tree.column(col, width=width, minwidth=30, stretch=False, anchor=anchor)

        scroll = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=tree.yview, style="Monitor.Vertical.TScrollbar")
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        tree.tag_configure("FG", foreground=ACCENT)
        tree.tag_configure("BG", foreground=MUTED)
        tree.tag_configure("alt", background=ROW_ALT)
        tree.bind("<Motion>", lambda e, t=tree, k=key: self._on_tree_motion(e, t, k))
        tree.bind("<Leave>", lambda _e: self._schedule_hide_kill())
        self.trees[key] = tree
        return frame

    def _build_metrics_panel(self, parent: tk.Frame) -> None:
        # Interval selector
        self._chart_window_s = int(self.config.get("chart_window_s", 300))
        interval_bar = tk.Frame(parent, bg=BG)
        interval_bar.pack(fill=tk.X, padx=2, pady=(0, 4))
        tk.Label(interval_bar, text="Окно:", bg=BG, fg=MUTED, font=self._font_tab).pack(side=tk.LEFT, padx=(0, 4))
        self._interval_btns: Dict[int, tk.Label] = {}
        for label, seconds in (
            ("10с", 10),
            ("30с", 30),
            ("1м", 60),
            ("5м", 300),
            ("30м", 1800),
            ("1ч", 3600),
            ("3ч", 10800),
            ("6ч", 21600),
        ):
            btn = tk.Label(
                interval_bar,
                text=label,
                bg=PANEL,
                fg=MUTED,
                font=self._font_tab,
                padx=5,
                pady=2,
                cursor="hand2",
            )
            btn.pack(side=tk.LEFT, padx=1)
            btn.bind("<Button-1>", lambda _e, s=seconds: self._set_chart_window(s))
            self._interval_btns[seconds] = btn
        self._paint_interval_btns()

        top = tk.Frame(parent, bg=BG)
        top.pack(fill=tk.X, padx=2, pady=2)

        left = tk.Frame(top, bg=BG)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        right = tk.Frame(top, bg=BG)
        right.pack(side=tk.RIGHT, fill=tk.Y, padx=(8, 0))

        self.cpu_summary = tk.Label(left, text="CPU: —", bg=BG, fg=FG, font=self._font_mono, anchor="w")
        self.cpu_summary.pack(fill=tk.X)
        self.core_canvas = tk.Canvas(left, height=16, bg=BG, highlightthickness=0, bd=0)
        self.core_canvas.pack(fill=tk.X, pady=1)
        self.ram_summary = tk.Label(left, text="RAM: —", bg=BG, fg=FG, font=self._font_mono, anchor="w")
        self.ram_summary.pack(fill=tk.X)
        self.gpu_summary = tk.Label(left, text="GPU: N/A", bg=BG, fg=MUTED, font=self._font_mono, anchor="w")
        self.gpu_summary.pack(fill=tk.X)
        self.gpu_vram = tk.Label(left, text="VRAM: N/A", bg=BG, fg=MUTED, font=self._font_mono, anchor="w")
        self.gpu_vram.pack(fill=tk.X)
        self.disk_summary = tk.Label(left, text="Disk IO: —", bg=BG, fg=FG, font=self._font_mono, anchor="w")
        self.disk_summary.pack(fill=tk.X)
        self.disk_usage = tk.Label(left, text="—", bg=BG, fg=MUTED, font=self._font_mono, anchor="w", justify=tk.LEFT)
        self.disk_usage.pack(fill=tk.X)
        self.net_summary = tk.Label(left, text="Net: —", bg=BG, fg=OK, font=self._font_mono, anchor="w")
        self.net_summary.pack(fill=tk.X)

        tk.Label(right, text="Temps °C", bg=BG, fg=MUTED, font=self._font_b, anchor="w").pack(fill=tk.X)
        self.temp_labels: Dict[str, tk.Label] = {}
        for key, title in (("cpu", "CPU"), ("gpu", "GPU"), ("motherboard", "MB"), ("ram", "RAM")):
            lbl = tk.Label(right, text=f"{title}: N/A", bg=PANEL, fg=MUTED, font=self._font_mono, anchor="w", padx=6, pady=1)
            lbl.pack(fill=tk.X, pady=1)
            self.temp_labels[key] = lbl

        self.cpu_temp_lbl = self.temp_labels["cpu"]
        self.gpu_temp_lbl = self.temp_labels["gpu"]

        self.temp_chart = TempChart(parent, height=160)
        self.temp_chart.pack(fill=tk.BOTH, expand=True, padx=2, pady=(4, 2))

    def _paint_interval_btns(self) -> None:
        for seconds, btn in self._interval_btns.items():
            if seconds == self._chart_window_s:
                btn.configure(bg=ACCENT, fg="#ffffff")
            else:
                btn.configure(bg=PANEL, fg=MUTED)

    def _set_chart_window(self, seconds: int) -> None:
        self._chart_window_s = int(seconds)
        self.config["chart_window_s"] = self._chart_window_s
        try:
            from settings import save_config

            save_config(self.config)
        except Exception:
            pass
        self._paint_interval_btns()
        if self._latest is not None:
            self._update_metrics_panel(self._latest)
        log(f"chart_window_s={seconds}")

    def _paint_tabs(self) -> None:
        for name, lbl in self._tab_btns.items():
            pct = float(self._tab_percents.get(name, 0.0))
            short = {"CPU": "CPU", "RAM": "RAM", "Disk": "Disk", "Показатели": "Показатели"}[name]
            active = name == self._active_tab
            bg, fg = load_band_colors(pct, active=active)
            lbl.configure(text=f"{short} {pct:.0f}%", bg=bg, fg=fg)

    def _update_tab_badges(self, snap: MetricsSnapshot) -> None:
        disk_pct = 0.0
        if snap.disks:
            disk_pct = max(float(d.get("percent") or 0.0) for d in snap.disks)
        overall = max(float(snap.cpu_percent), float(snap.ram_percent))
        self._tab_percents = {
            "CPU": float(snap.cpu_percent),
            "RAM": float(snap.ram_percent),
            "Disk": disk_pct,
            "Показатели": overall,
        }
        self._paint_tabs()

    def _on_tab_click(self, name: str) -> None:
        if self._collapsed:
            self.expand()
        self._switch_tab(name)

    def _switch_tab(self, name: str) -> None:
        self._hide_kill()
        self._active_tab = name
        self._paint_tabs()
        if self._collapsed:
            return
        for key, frame in self.table_frames.items():
            if key == name:
                frame.pack(fill=tk.BOTH, expand=True)
            else:
                frame.pack_forget()
        if self._latest is not None:
            self._render_active(force_rebuild=True)
        log(f"tab={name}")

    def toggle_collapse(self) -> None:
        if self._collapsed:
            self.expand()
        else:
            self.collapse()

    def collapse(self) -> None:
        if self._collapsed:
            return
        self._hide_kill()
        self._full_height = max(self._full_height, int(self.config.get("height", 400)))
        try:
            cur_h = int(self.win.winfo_height())
            if cur_h > COLLAPSED_HEIGHT + 20:
                self._full_height = cur_h
        except Exception:
            pass
        self.body.pack_forget()
        self._collapsed = True
        self.min_btn.configure(text="□")
        w = int(self.config.get("width", 560))
        x, y = self.win.winfo_x(), self.win.winfo_y()
        x, y = self._clamp_snap(x, y, w, COLLAPSED_HEIGHT)
        self.win.geometry(f"{w}x{COLLAPSED_HEIGHT}+{x}+{y}")
        self._note_position(x, y)
        log("overlay collapsed")

    def expand(self) -> None:
        if not self._collapsed:
            return
        self._collapsed = False
        self.min_btn.configure(text="−")
        self.body.pack(fill=tk.BOTH, expand=True, padx=6, pady=(0, 6))
        w = int(self.config.get("width", 560))
        h = int(self.config.get("height", self._full_height))
        self._full_height = h
        x, y = self.win.winfo_x(), self.win.winfo_y()
        x, y = self._clamp_snap(x, y, w, h)
        self.win.geometry(f"{w}x{h}+{x}+{y}")
        self._switch_tab(self._active_tab)
        self._note_position(x, y)
        log("overlay expanded")

    # ---- hover kill ----

    def _cancel_hide_kill(self) -> None:
        if self._hide_kill_job:
            try:
                self.win.after_cancel(self._hide_kill_job)
            except Exception:
                pass
            self._hide_kill_job = None

    def _schedule_hide_kill(self) -> None:
        self._cancel_hide_kill()
        self._hide_kill_job = self.win.after(120, self._hide_kill)

    def _hide_kill(self) -> None:
        self._hide_kill_job = None
        self._kill_row = None
        try:
            self.kill_btn.place_forget()
        except Exception:
            pass

    def _on_tree_motion(self, event: tk.Event, tree: ttk.Treeview, key: str) -> None:
        if self._active_tab != key or key == "Показатели":
            self._hide_kill()
            return
        if self._hide_kill_job:
            try:
                self.win.after_cancel(self._hide_kill_job)
            except Exception:
                pass
            self._hide_kill_job = None

        row = tree.identify_row(event.y)
        if not row:
            self._hide_kill()
            return
        try:
            pid = int(row)
        except ValueError:
            self._hide_kill()
            return
        if pid == self._self_pid:
            self._hide_kill()
            return

        bbox = tree.bbox(row)
        if not bbox:
            self._hide_kill()
            return
        x, y, w, h = bbox
        # Place kill button relative to body, mapped from tree coords
        tree.update_idletasks()
        abs_x = tree.winfo_rootx() - self.body.winfo_rootx() + x + w - 22
        abs_y = tree.winfo_rooty() - self.body.winfo_rooty() + y + max(0, (h - 18) // 2)
        self._kill_row = row
        self.kill_btn.configure(fg=MUTED, bg=PANEL)
        self.kill_btn.place(x=abs_x, y=abs_y, width=16, height=16)
        self.kill_btn.lift()

    def _on_kill_click(self, _event: tk.Event) -> None:
        row = self._kill_row
        if not row:
            return
        try:
            pid = int(row)
        except ValueError:
            return
        name = "?"
        if self._latest:
            for lst in (self._latest.processes_cpu, self._latest.processes_ram, self._latest.processes_disk):
                for p in lst:
                    if int(p.get("pid") or 0) == pid:
                        name = str(p.get("name") or "?")
                        break
        if not messagebox.askyesno(
            "Завершить процесс?",
            f"Завершить «{name}» (PID {pid})?\nЭто освободит ресурсы устройства.",
            parent=self.win,
        ):
            self._hide_kill()
            return
        status = terminate_process(pid)
        log(f"terminate pid={pid} name={name} status={status}")
        self._hide_kill()
        if status == "ok" or status == "gone":
            # Remove row immediately for snappy UX
            for tree in self.trees.values():
                if tree.exists(row):
                    tree.delete(row)
        elif status == "denied":
            messagebox.showwarning(
                "Нет доступа",
                f"Не удалось завершить «{name}» (PID {pid}).\nНужны права администратора.",
                parent=self.win,
            )
        else:
            messagebox.showerror("Ошибка", f"Не удалось завершить процесс:\n{status}", parent=self.win)

    # ---- drag / position ----

    def _clamp_pos(
        self,
        x: int,
        y: int,
        win_w: Optional[int] = None,
        win_h: Optional[int] = None,
        sw: Optional[int] = None,
        sh: Optional[int] = None,
    ) -> tuple[int, int]:
        if sw is None:
            sw = self.win.winfo_screenwidth()
        if sh is None:
            sh = self.win.winfo_screenheight()
        if win_w is None:
            win_w = max(1, int(self.win.winfo_width()))
        if win_h is None:
            win_h = max(1, int(self.win.winfo_height()))
        max_x = max(0, int(sw) - int(win_w))
        max_y = max(0, int(sh) - int(win_h))
        return max(0, min(int(x), max_x)), max(0, min(int(y), max_y))

    def _clamp_snap(
        self,
        x: int,
        y: int,
        win_w: Optional[int] = None,
        win_h: Optional[int] = None,
    ) -> tuple[int, int]:
        sw = self.win.winfo_screenwidth()
        sh = self.win.winfo_screenheight()
        if win_w is None:
            win_w = max(1, int(self.win.winfo_width()))
        if win_h is None:
            win_h = max(1, int(self.win.winfo_height()))
        x, y = self._clamp_pos(x, y, win_w, win_h, sw, sh)
        max_x = max(0, sw - int(win_w))
        max_y = max(0, sh - int(win_h))
        if x < SNAP_PX:
            x = 0
        elif max_x - x < SNAP_PX:
            x = max_x
        if y < SNAP_PX:
            y = 0
        elif max_y - y < SNAP_PX:
            y = max_y
        return x, y

    def _note_position(self, x: Optional[int] = None, y: Optional[int] = None) -> None:
        if x is None or y is None:
            x, y = self.win.winfo_x(), self.win.winfo_y()
        self._pending_pos = (int(x), int(y))
        if self._pos_save_job:
            try:
                self.win.after_cancel(self._pos_save_job)
            except Exception:
                pass
        self._pos_save_job = self.win.after(POS_IDLE_MS, self._save_pending_pos)

    def _save_pending_pos(self) -> None:
        self._pos_save_job = None
        if not self._pending_pos:
            return
        x, y = self._pending_pos
        if self.config.get("pos_x") == x and self.config.get("pos_y") == y:
            return
        self.config["pos_x"] = x
        self.config["pos_y"] = y
        try:
            from settings import save_config

            save_config(self.config)
            log(f"position saved x={x} y={y}")
        except Exception as exc:
            log(f"position save failed: {exc}")

    def _start_drag(self, event: tk.Event) -> None:
        if self.config.get("click_through"):
            return
        self._dragging = True
        self._drag_x = event.x_root - self.win.winfo_x()
        self._drag_y = event.y_root - self.win.winfo_y()
        if self.on_drag_start:
            self.on_drag_start()

    def _on_drag(self, event: tk.Event) -> None:
        if self.config.get("click_through") or not self._dragging:
            return
        x = event.x_root - self._drag_x
        y = event.y_root - self._drag_y
        x, y = self._clamp_snap(x, y)
        self.win.geometry(f"+{x}+{y}")

    def _end_drag(self, _event: tk.Event) -> None:
        if not self._dragging:
            return
        self._dragging = False
        x, y = self._clamp_snap(self.win.winfo_x(), self.win.winfo_y())
        self.win.geometry(f"+{x}+{y}")
        self._note_position(x, y)
        if self.on_drag_end:
            self.on_drag_end()

    # ---- public API ----

    def show(self) -> None:
        self.win.deiconify()
        self.win.attributes("-topmost", True)
        self._visible = True
        self._reapply_win32()
        log("overlay shown")

    def hide(self) -> None:
        self.win.withdraw()
        self._visible = False
        log("overlay hidden")

    def toggle_visibility(self) -> None:
        if self._visible:
            self.hide()
        else:
            self.show()

    @property
    def visible(self) -> bool:
        return self._visible

    @property
    def dragging(self) -> bool:
        return self._dragging

    def set_opacity(self, value: float) -> None:
        value = max(0.3, min(1.0, float(value)))
        self.config["opacity"] = value
        self.win.attributes("-alpha", value)

    def set_click_through(self, enabled: bool) -> None:
        self.config["click_through"] = bool(enabled)
        set_click_through_hwnd(self._hwnd, bool(enabled))
        self._restore_alpha()

    def apply_config(self, config: Dict[str, Any]) -> None:
        prev_x = self.config.get("pos_x")
        prev_y = self.config.get("pos_y")
        self.config = config
        # Do not wipe saved/pending position unless explicitly provided
        if config.get("pos_x") is None and prev_x is not None:
            self.config["pos_x"] = prev_x
        if config.get("pos_y") is None and prev_y is not None:
            self.config["pos_y"] = prev_y
        if "height" in config:
            self._full_height = int(config.get("height", self._full_height))
        self.set_opacity(config.get("opacity", 0.85))
        self.set_click_through(bool(config.get("click_through")))
        if not self._collapsed:
            w = int(self.config.get("width", 560))
            h = int(self.config.get("height", self._full_height))
            x, y = self._clamp_snap(self.win.winfo_x(), self.win.winfo_y(), w, h)
            self.win.geometry(f"{w}x{h}+{x}+{y}")
        log("overlay config applied")

    def update_metrics(self, data: MetricsSnapshot | Dict[str, Any]) -> None:
        if self._dragging:
            return
        if isinstance(data, MetricsSnapshot):
            self._latest = data
        else:
            # Minimal path — wrap not needed; store as snapshot-like via collect usage
            snap = MetricsSnapshot(**{k: data[k] for k in MetricsSnapshot.__dataclass_fields__ if k in data})
            self._latest = snap
        self._render_active(force_rebuild=False)
        self._update_tab_badges(self._latest)
        self._update_alerts()

    def _render_active(self, force_rebuild: bool) -> None:
        snap = self._latest
        if snap is None:
            return
        tab = self._active_tab
        if tab == "CPU":
            self._fill_tree("CPU", snap.processes_cpu, "cpu", force_rebuild)
        elif tab == "RAM":
            self._fill_tree("RAM", snap.processes_ram, "ram", force_rebuild)
        elif tab == "Disk":
            self._fill_tree("Disk", snap.processes_disk, "disk", force_rebuild)
        else:
            self._update_metrics_panel(snap)

    def _fill_tree(self, key: str, procs: List[Dict[str, Any]], mode: str, force_rebuild: bool) -> None:
        tree = self.trees[key]
        pids = [str(p.get("pid", 0)) for p in procs]
        prev = self._tree_pids.get(key, [])
        self._rebuild_counter += 1
        do_rebuild = force_rebuild or prev != pids or (self._rebuild_counter % 5 == 0)
        icons = self._icon_cache

        if do_rebuild:
            tree.delete(*tree.get_children())
            for i, p in enumerate(procs):
                values = self._row_values(p, mode)
                tags = (p.get("type") or "BG",)
                if i % 2:
                    tags = tags + ("alt",)
                img = icons.get(p.get("exe")) if icons else None
                name = str(p.get("name") or "?")[:36]
                tree.insert(
                    "",
                    tk.END,
                    iid=str(p.get("pid")),
                    text=f" {name}",
                    image=img,
                    values=values,
                    tags=tags,
                )
            self._tree_pids[key] = pids
        else:
            for p in procs:
                iid = str(p.get("pid"))
                if tree.exists(iid):
                    name = str(p.get("name") or "?")[:36]
                    kw = {"values": self._row_values(p, mode), "tags": (p.get("type") or "BG",), "text": f" {name}"}
                    if icons:
                        kw["image"] = icons.get(p.get("exe"))
                    tree.item(iid, **kw)

    @staticmethod
    def _row_values(p: Dict[str, Any], mode: str) -> tuple:
        pid = p.get("pid", 0)
        typ = p.get("type") or ("BG" if p.get("is_background") else "FG")
        if mode == "cpu":
            return (pid, f"{float(p.get('cpu') or 0):.1f}", typ)
        if mode == "ram":
            return (pid, f"{float(p.get('ram_mb') or 0):.0f}", typ)
        return (
            pid,
            f"{float(p.get('disk_r') or 0):.2f}",
            f"{float(p.get('disk_w') or 0):.2f}",
            typ,
        )

    def _update_metrics_panel(self, snap: MetricsSnapshot) -> None:
        th = self.config.get("thresholds", {})
        moderate = float(th.get("moderate", 70))
        cpu_crit = float(th.get("cpu_load", 90))
        ram_crit = float(th.get("ram_usage", 90))
        cpu_temp_crit = float(th.get("cpu_temp", 85))
        gpu_temp_crit = float(th.get("gpu_temp", 85))

        cpu = snap.cpu_percent
        self.cpu_summary.configure(text=f"CPU: {cpu:5.1f}%", fg=metric_color(cpu, moderate, cpu_crit))
        self._draw_cores(snap.cpu_per_core, moderate, cpu_crit)

        self.ram_summary.configure(
            text=f"RAM: {snap.ram_used_gb:.1f}/{snap.ram_total_gb:.1f} GB  ({snap.ram_percent:.0f}%)",
            fg=metric_color(snap.ram_percent, moderate, ram_crit),
        )

        if snap.gpu_load is None:
            self.gpu_summary.configure(text="GPU: N/A", fg=MUTED)
        else:
            self.gpu_summary.configure(
                text=f"GPU: {snap.gpu_load:5.1f}%",
                fg=metric_color(snap.gpu_load, moderate, 90),
            )
        if snap.gpu_vram_used is None or snap.gpu_vram_total is None:
            self.gpu_vram.configure(text="VRAM: N/A", fg=MUTED)
        else:
            self.gpu_vram.configure(
                text=f"VRAM: {snap.gpu_vram_used:.0f}/{snap.gpu_vram_total:.0f} MB",
                fg=FG,
            )

        temps = snap.temps or {}
        mapping = {
            "cpu": (temps.get("cpu", snap.cpu_temp), "CPU", cpu_temp_crit),
            "gpu": (temps.get("gpu", snap.gpu_temp), "GPU", gpu_temp_crit),
            "motherboard": (temps.get("motherboard"), "MB", 85),
            "ram": (temps.get("ram"), "RAM", 85),
        }
        for key, (val, title, crit) in mapping.items():
            lbl = self.temp_labels[key]
            if val is None:
                lbl.configure(text=f"{title}: N/A", fg=MUTED)
            else:
                lbl.configure(
                    text=f"{title}: {float(val):.0f}°C",
                    fg=metric_color(float(val), 70, crit),
                )

        self.disk_summary.configure(
            text=f"Disk IO: R {snap.disk_read_mbs:.1f}  W {snap.disk_write_mbs:.1f} MB/s",
            fg=FG,
        )
        if snap.disks:
            lines = [f"{d.get('device', '?')}: {float(d.get('percent') or 0):.0f}%" for d in snap.disks[:4]]
            self.disk_usage.configure(text="  ".join(lines), fg=MUTED)
        else:
            self.disk_usage.configure(text="N/A", fg=MUTED)

        self.net_summary.configure(
            text=f"Net: ↑ {snap.net_up_mbs:.2f}  ↓ {snap.net_down_mbs:.2f} MB/s",
            fg=OK,
        )

        # Chart from history callback
        if self._history_points is not None:
            try:
                window = int(getattr(self, "_chart_window_s", self.config.get("chart_window_s", 300)))
                # history.series may accept window_s
                try:
                    pts = self._history_points(window)  # type: ignore[call-arg]
                except TypeError:
                    pts = [p for p in self._history_points() if getattr(p, "t", 0) >= time.time() - window]
                self.temp_chart.set_points(pts)
            except Exception as exc:
                log(f"chart update failed: {exc}")
        else:
            from history import HistoryPoint

            self.temp_chart.set_points(
                [
                    HistoryPoint(
                        t=snap.timestamp or time.time(),
                        cpu=snap.cpu_percent,
                        ram=snap.ram_percent,
                        dsk=0,
                        cpu_t=temps.get("cpu", snap.cpu_temp),
                        gpu_t=temps.get("gpu", snap.gpu_temp),
                        mb_t=temps.get("motherboard"),
                        ram_t=temps.get("ram"),
                    )
                ]
            )

    def _draw_cores(self, cores: List[float], moderate: float, critical: float) -> None:
        c = self.core_canvas
        c.delete("all")
        if not cores:
            return
        w = max(c.winfo_width(), 280)
        h = 30
        n = len(cores)
        gap = 2
        bar_w = max(2, (w - gap * (n + 1)) // n)
        for i, val in enumerate(cores):
            x0 = gap + i * (bar_w + gap)
            bh = max(1, int((min(val, 100) / 100.0) * (h - 4)))
            y0 = h - 2 - bh
            c.create_rectangle(x0, y0, x0 + bar_w, h - 2, fill=metric_color(val, moderate, critical), outline="")

    def _update_alerts(self) -> None:
        snap = self._latest
        if snap is None:
            return
        th = self.config.get("thresholds", {})
        alerts = False
        if snap.cpu_percent > float(th.get("cpu_load", 90)):
            alerts = True
        if snap.ram_percent > float(th.get("ram_usage", 90)):
            alerts = True
        if snap.cpu_temp is not None and snap.cpu_temp > float(th.get("cpu_temp", 85)):
            alerts = True
        if snap.gpu_temp is not None and snap.gpu_temp > float(th.get("gpu_temp", 85)):
            alerts = True
        proc_lim = float(th.get("process_cpu", 50))
        if any(float(p.get("cpu") or 0) > proc_lim for p in snap.processes_cpu[:20]):
            alerts = True

        if alerts:
            self._alert_active = True
            if not self._pulse_job:
                self._pulse()
        else:
            self._alert_active = False
            self.border.configure(highlightbackground=BORDER)
            if self._pulse_job:
                try:
                    self.win.after_cancel(self._pulse_job)
                except Exception:
                    pass
                self._pulse_job = None

    def _pulse(self) -> None:
        if not self._alert_active:
            self.border.configure(highlightbackground=BORDER)
            self._pulse_job = None
            return
        self._pulse_on = not self._pulse_on
        color = CRIT if self._pulse_on else "#5a1d1d"
        self.border.configure(highlightbackground=color, highlightcolor=color)
        self._pulse_job = self.win.after(400, self._pulse)

    def copy_stats(self) -> None:
        snap = self._latest
        if snap is None:
            log("copy skipped: no data")
            return
        temps = snap.temps or {}
        lines = [
            "=== System Monitor ===",
            f"CPU: {snap.cpu_percent:.1f}%  temp={temps.get('cpu', snap.cpu_temp) if temps.get('cpu', snap.cpu_temp) is not None else 'N/A'}",
            f"RAM: {snap.ram_used_gb:.1f}/{snap.ram_total_gb:.1f} GB ({snap.ram_percent:.0f}%)",
            f"Disk IO: R {snap.disk_read_mbs:.1f} W {snap.disk_write_mbs:.1f} MB/s",
            f"Net: up {snap.net_up_mbs:.2f} down {snap.net_down_mbs:.2f} MB/s",
            f"GPU: {snap.gpu_load if snap.gpu_load is not None else 'N/A'}",
            f"Tab: {self._active_tab}",
            "",
        ]
        if self._active_tab == "CPU":
            lines.append("Name\tPID\tCPU%\tType")
            for p in snap.processes_cpu:
                lines.append(f"{p['name']}\t{p['pid']}\t{p['cpu']:.1f}\t{p.get('type', '?')}")
        elif self._active_tab == "RAM":
            lines.append("Name\tPID\tMB\tType")
            for p in snap.processes_ram:
                lines.append(f"{p['name']}\t{p['pid']}\t{p['ram_mb']:.0f}\t{p.get('type', '?')}")
        elif self._active_tab == "Disk":
            lines.append("Name\tPID\tR\tW\tType")
            for p in snap.processes_disk:
                lines.append(
                    f"{p['name']}\t{p['pid']}\t{p['disk_r']:.2f}\t{p['disk_w']:.2f}\t{p.get('type', '?')}"
                )
        else:
            lines.append("=== Temps ===")
            for key, title in (("cpu", "CPU"), ("gpu", "GPU"), ("motherboard", "MB"), ("ram", "RAM")):
                val = temps.get(key)
                if key == "cpu" and val is None:
                    val = snap.cpu_temp
                if key == "gpu" and val is None:
                    val = snap.gpu_temp
                lines.append(f"{title}: {f'{val:.0f}C' if val is not None else 'N/A'}")
            window = int(getattr(self, "_chart_window_s", 300))
            lines.append(f"Chart window: {window}s")
            lines.append("t\tcpu%\tram%\tdsk%\tcpu_t\tgpu_t\tmb_t\tram_t")
            pts = []
            if self._history_points is not None:
                try:
                    pts = self._history_points(window)  # type: ignore[call-arg]
                except TypeError:
                    pts = self._history_points()
            for p in pts[-60:]:
                lines.append(
                    f"{int(p.t)}\t{p.cpu:.1f}\t{p.ram:.1f}\t{p.dsk:.0f}\t"
                    f"{'' if p.cpu_t is None else f'{p.cpu_t:.0f}'}\t"
                    f"{'' if p.gpu_t is None else f'{p.gpu_t:.0f}'}\t"
                    f"{'' if p.mb_t is None else f'{p.mb_t:.0f}'}\t"
                    f"{'' if p.ram_t is None else f'{p.ram_t:.0f}'}"
                )
        text = "\n".join(lines)
        try:
            self.win.clipboard_clear()
            self.win.clipboard_append(text)
            self.win.update_idletasks()
            log(f"stats copied ({len(lines)} lines)")
        except Exception as exc:
            log(f"copy failed: {exc}")

    def destroy(self) -> None:
        if self._pulse_job:
            try:
                self.win.after_cancel(self._pulse_job)
            except Exception:
                pass
        try:
            self.win.destroy()
        except Exception:
            pass
