"""Compact CSV metrics history with downsample and 10 MB cap."""

from __future__ import annotations

import csv
import os
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, Iterable, List, Optional

from logutil import log

HEADER = ["t", "cpu", "ram", "dsk", "cpu_t", "gpu_t", "mb_t", "ram_t"]
MAX_BYTES = 10 * 1024 * 1024
MAX_AGE_S = 6 * 3600
DEFAULT_INTERVAL_S = 10


def _fmt(v: Optional[float], nd: int = 1) -> str:
    if v is None:
        return ""
    return f"{float(v):.{nd}f}"


@dataclass
class HistoryPoint:
    t: float
    cpu: float
    ram: float
    dsk: float
    cpu_t: Optional[float]
    gpu_t: Optional[float]
    mb_t: Optional[float]
    ram_t: Optional[float]


class StatsHistory:
    def __init__(
        self,
        path: str,
        interval_s: float = DEFAULT_INTERVAL_S,
        enabled: bool = True,
    ) -> None:
        self.path = path
        self.interval_s = 10.0  # always collect every 10s
        self.enabled = bool(enabled)
        self._last_write = 0.0
        self._writes = 0
        self.points: Deque[HistoryPoint] = deque(maxlen=2500)
        self._ensure_file()
        self._load_recent()

    def ensure_file(self) -> None:
        self._ensure_file()

    def _ensure_file(self) -> None:
        folder = os.path.dirname(self.path)
        if folder and not os.path.isdir(folder):
            os.makedirs(folder, exist_ok=True)
        if not os.path.isfile(self.path):
            with open(self.path, "w", encoding="utf-8", newline="") as f:
                csv.writer(f).writerow(HEADER)

    def _load_recent(self) -> None:
        if not os.path.isfile(self.path):
            return
        cutoff = time.time() - MAX_AGE_S
        try:
            with open(self.path, "r", encoding="utf-8", newline="") as f:
                reader = csv.DictReader(f)
                rows = list(reader)
            for row in rows[-2500:]:
                try:
                    t = float(row.get("t") or 0)
                    if t < cutoff:
                        continue
                    self.points.append(
                        HistoryPoint(
                            t=t,
                            cpu=float(row["cpu"] or 0),
                            ram=float(row["ram"] or 0),
                            dsk=float(row["dsk"] or 0),
                            cpu_t=float(row["cpu_t"]) if row.get("cpu_t") else None,
                            gpu_t=float(row["gpu_t"]) if row.get("gpu_t") else None,
                            mb_t=float(row["mb_t"]) if row.get("mb_t") else None,
                            ram_t=float(row["ram_t"]) if row.get("ram_t") else None,
                        )
                    )
                except Exception:
                    continue
        except Exception as exc:
            log(f"history load failed: {exc}")

    def maybe_append(self, snap) -> None:
        if not self.enabled:
            return
        now = time.time()
        if now - self._last_write < self.interval_s:
            return
        self._last_write = now
        temps = getattr(snap, "temps", None) or {}
        disk_pct = 0.0
        disks = getattr(snap, "disks", None) or []
        if disks:
            disk_pct = max(float(d.get("percent") or 0) for d in disks)

        pt = HistoryPoint(
            t=now,
            cpu=float(getattr(snap, "cpu_percent", 0) or 0),
            ram=float(getattr(snap, "ram_percent", 0) or 0),
            dsk=disk_pct,
            cpu_t=temps.get("cpu", getattr(snap, "cpu_temp", None)),
            gpu_t=temps.get("gpu", getattr(snap, "gpu_temp", None)),
            mb_t=temps.get("motherboard"),
            ram_t=temps.get("ram"),
        )
        self.points.append(pt)
        try:
            with open(self.path, "a", encoding="utf-8", newline="") as f:
                csv.writer(f).writerow(
                    [
                        int(pt.t),
                        _fmt(pt.cpu),
                        _fmt(pt.ram),
                        _fmt(pt.dsk, 0),
                        _fmt(pt.cpu_t, 0),
                        _fmt(pt.gpu_t, 0),
                        _fmt(pt.mb_t, 0),
                        _fmt(pt.ram_t, 0),
                    ]
                )
            self._writes += 1
            if self._writes % 30 == 0 or self._file_size() > 8 * 1024 * 1024:
                self.compact()
        except Exception as exc:
            log(f"history write failed: {exc}")

    def _file_size(self) -> int:
        try:
            return os.path.getsize(self.path)
        except OSError:
            return 0

    def compact(self) -> None:
        """Downsample older points and enforce size/age limits."""
        try:
            with open(self.path, "r", encoding="utf-8", newline="") as f:
                rows = list(csv.DictReader(f))
        except Exception as exc:
            log(f"history compact read failed: {exc}")
            return

        now = time.time()
        cutoff = now - MAX_AGE_S
        buckets: Dict[int, dict] = {}
        for row in rows:
            try:
                t = float(row.get("t") or 0)
            except Exception:
                continue
            if t < cutoff:
                continue
            age = now - t
            if age <= 30 * 60:
                step = 10
            elif age <= 2 * 3600:
                step = 60
            else:
                step = 300
            key = int(t // step) * step
            # keep latest in bucket
            buckets[key] = row

        ordered = [buckets[k] for k in sorted(buckets.keys())]
        # Trim if still too large (~80 bytes/row rough)
        max_rows = max(200, MAX_BYTES // 80)
        if len(ordered) > max_rows:
            ordered = ordered[-max_rows:]

        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8", newline="") as f:
                w = csv.DictWriter(f, fieldnames=HEADER, extrasaction="ignore")
                w.writeheader()
                for row in ordered:
                    w.writerow({h: row.get(h, "") for h in HEADER})
            os.replace(tmp, self.path)
            # refresh memory from compacted
            self.points.clear()
            self._load_recent()
            log(f"history compacted rows={len(ordered)} size={self._file_size()}")
        except Exception as exc:
            log(f"history compact failed: {exc}")
            try:
                if os.path.isfile(tmp):
                    os.remove(tmp)
            except OSError:
                pass

    def series(self, window_s: Optional[float] = None) -> List[HistoryPoint]:
        pts = list(self.points)
        if window_s is None or window_s <= 0:
            return pts
        cutoff = time.time() - float(window_s)
        return [p for p in pts if p.t >= cutoff]

    def force_append(self, snap) -> None:
        """Bypass interval once (startup seed)."""
        prev = self._last_write
        self._last_write = 0.0
        self.maybe_append(snap)
        if self._last_write == 0.0:
            self._last_write = prev
