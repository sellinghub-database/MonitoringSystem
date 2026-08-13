"""Temperature / load history chart on tkinter Canvas."""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional, Sequence, Tuple

import tkinter as tk

from history import HistoryPoint

BG = "#0f1419"
FG = "#c9d1d9"
MUTED = "#8b949e"
GRID = "#21262d"

# Prefer temps; always also plot load% as fallback so chart is never empty.
TEMP_SERIES = [
    ("cpu_t", "CPU°", "#3d8bfd"),
    ("gpu_t", "GPU°", "#3fb950"),
    ("mb_t", "MB°", "#d29922"),
    ("ram_t", "RAM°", "#a371f7"),
]
LOAD_SERIES = [
    ("cpu", "CPU%", "#58a6ff"),
    ("ram", "RAM%", "#f778ba"),
]


class TempChart(tk.Frame):
    def __init__(self, parent: tk.Misc, height: int = 160, **kwargs) -> None:
        super().__init__(parent, bg=BG, **kwargs)
        self.canvas = tk.Canvas(self, height=height, bg=BG, highlightthickness=0, bd=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.tooltip = tk.Label(self, text="", bg="#161b22", fg=FG, font=("Consolas", 8), justify=tk.LEFT)
        self.tooltip.place_forget()
        self._points: List[HistoryPoint] = []
        self._hover_idx: Optional[int] = None
        self._mode = "auto"  # auto: temps if any else load%
        self.canvas.bind("<Configure>", lambda _e: self.redraw())
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Leave>", self._on_leave)

    def set_points(self, points: Sequence[HistoryPoint]) -> None:
        self._points = list(points)
        self.redraw()

    def _active_series(self) -> List[Tuple[str, str, str]]:
        pts = self._points
        has_temp = any(
            getattr(p, k, None) is not None for p in pts for k, _, _ in TEMP_SERIES
        )
        if has_temp:
            return list(TEMP_SERIES) + list(LOAD_SERIES)
        return list(LOAD_SERIES)

    def redraw(self) -> None:
        c = self.canvas
        c.delete("all")
        w = max(c.winfo_width(), 120)
        h = max(c.winfo_height(), 100)
        pad_l, pad_r, pad_t, pad_b = 36, 10, 16, 22
        plot_w = w - pad_l - pad_r
        plot_h = h - pad_t - pad_b
        if plot_w < 20 or plot_h < 20:
            c.create_text(w // 2, h // 2, text="График…", fill=MUTED, font=("Segoe UI", 9))
            return

        pts = self._points
        series = self._active_series()
        unit_temp = any(k.endswith("_t") or k in ("cpu_t", "gpu_t", "mb_t", "ram_t") for k, _, _ in series if k.endswith("_t"))
        # Split: if both temp and load, use two scales visually by normalizing load to 0-100 same axis with dual meaning in tooltip
        vals: List[float] = []
        for p in pts:
            for key, _, _ in series:
                v = getattr(p, key, None)
                if v is not None:
                    vals.append(float(v))

        if not vals:
            ymin, ymax = (0.0, 100.0)
        else:
            ymin = max(0.0, min(vals) - 5)
            ymax = max(vals) + 5
            if ymax <= ymin:
                ymax = ymin + 10

        for i in range(5):
            y = pad_t + plot_h * i / 4
            temp = ymax - (ymax - ymin) * i / 4
            c.create_line(pad_l, y, w - pad_r, y, fill=GRID)
            c.create_text(pad_l - 4, y, text=f"{temp:.0f}", fill=MUTED, anchor="e", font=("Consolas", 7))

        if len(pts) < 1:
            c.create_text(w // 2, h // 2, text="Ожидание данных…", fill=MUTED, font=("Segoe UI", 9))
            return

        if len(pts) == 1:
            # Duplicate point so a stub line can show
            pts = [pts[0], HistoryPoint(
                t=pts[0].t + 1,
                cpu=pts[0].cpu,
                ram=pts[0].ram,
                dsk=pts[0].dsk,
                cpu_t=pts[0].cpu_t,
                gpu_t=pts[0].gpu_t,
                mb_t=pts[0].mb_t,
                ram_t=pts[0].ram_t,
            )]

        t0, t1 = pts[0].t, pts[-1].t
        span = max(t1 - t0, 1.0)

        def x_at(t: float) -> float:
            return pad_l + plot_w * ((t - t0) / span)

        def y_at(v: float) -> float:
            return pad_t + plot_h * (1.0 - (v - ymin) / (ymax - ymin))

        drawn = False
        for key, _label, color in series:
            coords: List[float] = []
            for p in pts:
                v = getattr(p, key, None)
                if v is None:
                    if len(coords) >= 4:
                        c.create_line(*coords, fill=color, width=1.5, smooth=False)
                        drawn = True
                    coords = []
                    continue
                coords.extend([x_at(p.t), y_at(float(v))])
            if len(coords) >= 4:
                c.create_line(*coords, fill=color, width=1.5, smooth=False)
                drawn = True
            elif len(coords) == 2:
                c.create_oval(coords[0] - 2, coords[1] - 2, coords[0] + 2, coords[1] + 2, fill=color, outline="")
                drawn = True

        if not drawn:
            c.create_text(w // 2, h // 2, text="Нет серий для отображения", fill=MUTED, font=("Segoe UI", 9))

        lx = pad_l
        for key, label, color in series:
            c.create_rectangle(lx, 2, lx + 8, 10, fill=color, outline="")
            c.create_text(lx + 12, 6, text=label, fill=MUTED, anchor="w", font=("Segoe UI", 7))
            lx += 48

        c.create_text(pad_l, h - 8, text=datetime.fromtimestamp(t0).strftime("%H:%M:%S"), fill=MUTED, anchor="w", font=("Consolas", 7))
        c.create_text(w - pad_r, h - 8, text=datetime.fromtimestamp(t1).strftime("%H:%M:%S"), fill=MUTED, anchor="e", font=("Consolas", 7))

        if self._hover_idx is not None and 0 <= self._hover_idx < len(self._points):
            p = self._points[self._hover_idx]
            x = x_at(p.t)
            c.create_line(x, pad_t, x, pad_t + plot_h, fill="#8b949e", dash=(3, 2))
            for key, _label, color in series:
                v = getattr(p, key, None)
                if v is not None:
                    yy = y_at(float(v))
                    c.create_oval(x - 3, yy - 3, x + 3, yy + 3, fill=color, outline="")

    def _on_motion(self, event: tk.Event) -> None:
        if not self._points:
            return
        pts = self._points
        w = max(self.canvas.winfo_width(), 100)
        pad_l = 36
        pad_r = 10
        plot_w = w - pad_l - pad_r
        t0, t1 = pts[0].t, pts[-1].t
        span = max(t1 - t0, 1.0)
        ratio = min(1.0, max(0.0, (event.x - pad_l) / max(plot_w, 1)))
        t = t0 + span * ratio
        idx = min(range(len(pts)), key=lambda i: abs(pts[i].t - t))
        self._hover_idx = idx
        p = pts[idx]
        lines = [datetime.fromtimestamp(p.t).strftime("%H:%M:%S")]
        for key, label, _ in self._active_series():
            v = getattr(p, key, None)
            if key.endswith("_t") or key in ("cpu_t", "gpu_t", "mb_t", "ram_t"):
                lines.append(f"{label}: {f'{v:.0f}°C' if v is not None else 'N/A'}")
            else:
                lines.append(f"{label}: {f'{v:.1f}%' if v is not None else 'N/A'}")
        self.tooltip.configure(text="\n".join(lines))
        self.tooltip.place(x=min(event.x + 12, w - 140), y=max(4, event.y - 50))
        self.redraw()

    def _on_leave(self, _event: tk.Event) -> None:
        self._hover_idx = None
        self.tooltip.place_forget()
        self.redraw()
