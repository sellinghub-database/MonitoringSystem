"""System metrics collector with rolling averages and full process lists."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

import psutil

try:
    import wmi as wmi_mod
except Exception:  # pragma: no cover
    wmi_mod = None

try:
    import GPUtil
except Exception:  # pragma: no cover
    GPUtil = None

_SERVICE_USERS = {"SYSTEM", "LOCAL SERVICE", "NETWORK SERVICE", "SERVICE"}
_BG_NAME_HINTS = (
    "svchost",
    "services",
    "csrss",
    "smss",
    "wininit",
    "winlogon",
    "lsass",
    "fontdrvhost",
    "dwm",
    "conhost",
    "runtimebroker",
    "searchindexer",
    "searchhost",
    "startmenuexperiencehost",
    "shellexperiencehost",
    "sihost",
    "taskhostw",
    "dllhost",
    "wmi",
    "spoolsv",
    "securityhealth",
    "msmpeng",
    "nissrv",
)


def _avg(values: Deque[float]) -> Optional[float]:
    if not values:
        return None
    return sum(values) / len(values)


@dataclass
class MetricsSnapshot:
    cpu_percent: float = 0.0
    cpu_per_core: List[float] = field(default_factory=list)
    cpu_temp: Optional[float] = None
    gpu_load: Optional[float] = None
    gpu_vram_used: Optional[float] = None
    gpu_vram_total: Optional[float] = None
    gpu_temp: Optional[float] = None
    temps: Dict[str, Optional[float]] = field(default_factory=dict)
    ram_used_gb: float = 0.0
    ram_total_gb: float = 0.0
    ram_percent: float = 0.0
    disk_read_mbs: float = 0.0
    disk_write_mbs: float = 0.0
    disks: List[Dict[str, Any]] = field(default_factory=list)
    net_up_mbs: float = 0.0
    net_down_mbs: float = 0.0
    processes_cpu: List[Dict[str, Any]] = field(default_factory=list)
    processes_ram: List[Dict[str, Any]] = field(default_factory=list)
    processes_disk: List[Dict[str, Any]] = field(default_factory=list)
    top_cpu: List[Dict[str, Any]] = field(default_factory=list)
    top_ram: List[Dict[str, Any]] = field(default_factory=list)
    timestamp: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cpu_percent": self.cpu_percent,
            "cpu_per_core": list(self.cpu_per_core),
            "cpu_temp": self.cpu_temp,
            "gpu_load": self.gpu_load,
            "gpu_vram_used": self.gpu_vram_used,
            "gpu_vram_total": self.gpu_vram_total,
            "gpu_temp": self.gpu_temp,
            "temps": dict(self.temps),
            "ram_used_gb": self.ram_used_gb,
            "ram_total_gb": self.ram_total_gb,
            "ram_percent": self.ram_percent,
            "disk_read_mbs": self.disk_read_mbs,
            "disk_write_mbs": self.disk_write_mbs,
            "disks": list(self.disks),
            "net_up_mbs": self.net_up_mbs,
            "net_down_mbs": self.net_down_mbs,
            "processes_cpu": list(self.processes_cpu),
            "processes_ram": list(self.processes_ram),
            "processes_disk": list(self.processes_disk),
            "top_cpu": list(self.top_cpu),
            "top_ram": list(self.top_ram),
            "timestamp": self.timestamp,
        }


class SystemMonitor:
    """Collects system metrics on a background thread."""

    def __init__(
        self,
        refresh_ms: int = 2000,
        on_update: Optional[Callable[[MetricsSnapshot], None]] = None,
        perf_mode: bool = True,
    ) -> None:
        self.refresh_ms = max(500, int(refresh_ms))
        self.on_update = on_update
        self.perf_mode = bool(perf_mode)
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._latest: Optional[MetricsSnapshot] = None

        self._cpu_hist: Deque[float] = deque(maxlen=3)
        self._ram_hist: Deque[float] = deque(maxlen=3)
        self._gpu_hist: Deque[float] = deque(maxlen=3)
        self._disk_r_hist: Deque[float] = deque(maxlen=3)
        self._disk_w_hist: Deque[float] = deque(maxlen=3)
        self._net_up_hist: Deque[float] = deque(maxlen=3)
        self._net_down_hist: Deque[float] = deque(maxlen=3)
        self._core_hists: List[Deque[float]] = []

        self._prev_disk = None
        self._prev_net = None
        self._prev_time: Optional[float] = None
        self._prev_io: Dict[int, Tuple[int, int]] = {}
        self._bg_cache: Dict[int, Tuple[bool, float]] = {}
        self._exe_cache: Dict[int, Tuple[str, float]] = {}
        self._bg_ttl = 8.0
        self._exe_ttl = 60.0
        self._cycle = 0
        self._cached_cpu_temp: Optional[float] = None
        self._cached_temps: Dict[str, Optional[float]] = {
            "cpu": None,
            "gpu": None,
            "motherboard": None,
            "ram": None,
        }
        self._cached_gpu: Dict[str, Optional[float]] = {
            "load": None,
            "vram_used": None,
            "vram_total": None,
            "temp": None,
        }
        self._wmi = None
        self._wmi_ohm = None
        self._ohm_source: Optional[str] = None
        self._lhm_hint_logged = False
        self._nvidia_smi: Optional[str] = None
        self._init_wmi()
        self._nvidia_smi = self._find_nvidia_smi()

        psutil.cpu_percent(interval=None, percpu=True)
        for p in psutil.process_iter(["cpu_percent"]):
            try:
                _ = p.info
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
            except Exception:
                continue

    def _init_wmi(self) -> None:
        from logutil import log

        if wmi_mod is None:
            log("wmi=missing ohm=None (install WMI / run as admin if needed)")
            return
        try:
            self._wmi = wmi_mod.WMI(namespace="root\\wmi")
            wmi_ok = True
        except Exception as exc:
            self._wmi = None
            wmi_ok = False
            log(f"wmi root\\wmi failed: {exc}")
        self._ohm_source = None
        try:
            self._wmi_ohm = wmi_mod.WMI(namespace="root\\LibreHardwareMonitor")
            self._ohm_source = "LibreHardwareMonitor"
        except Exception:
            try:
                self._wmi_ohm = wmi_mod.WMI(namespace="root\\OpenHardwareMonitor")
                self._ohm_source = "OpenHardwareMonitor"
            except Exception:
                self._wmi_ohm = None
                self._ohm_source = None
        log(f"wmi={'ok' if wmi_ok else 'None'} ohm={self._ohm_source or 'None'}")
        if self._ohm_source is None and not self._lhm_hint_logged:
            self._lhm_hint_logged = True
            log(
                "temps: LibreHardwareMonitor/OHM WMI unavailable — "
                "start LibreHardwareMonitor with WMI enabled for CPU/MB/RAM temps"
            )

    @staticmethod
    def _find_nvidia_smi() -> Optional[str]:
        found = shutil.which("nvidia-smi")
        if found:
            return found
        for path in (
            r"C:\Windows\System32\nvidia-smi.exe",
            r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe",
        ):
            if os.path.isfile(path):
                return path
        return None

    @staticmethod
    def _classify_temp_sensor(name: str, parent: str, identifier: str) -> Optional[str]:
        """Map LHM/OHM sensor to cpu|gpu|motherboard|ram using Name + Parent + Identifier."""
        blob = f"{name} {parent} {identifier}".lower()
        # Path-based (most reliable)
        if any(
            p in blob
            for p in (
                "/nvidiagpu",
                "/amdgpu",
                "/intelgpu",
                "nvidiagpu",
                "amdgpu",
                "gpu-nvidia",
                "gpu-amd",
            )
        ):
            return "gpu"
        if "gpu" in name or "gpu" in parent:
            return "gpu"
        if any(p in blob for p in ("/ram", "/memory", "dimm", "/ddr")):
            if "vram" in blob or "gpu" in blob:
                return "gpu"
            return "ram"
        if any(
            t in name
            for t in ("dimm", "memory", "ddr", "sodimm")
        ) or ("ram" in name and "vram" not in name and "gram" not in name):
            return "ram"
        if any(p in blob for p in ("/amdcpu", "/intelcpu", "/cpu", "amdcpu", "intelcpu")):
            return "cpu"
        if any(t in name for t in ("cpu", "package", "tdie", "tctl", "ccd", "core #", "core temperature")):
            return "cpu"
        if "core" in name and "gpu" not in blob:
            return "cpu"
        # Motherboard / chipset / VRM — avoid bare "system" (matches too much)
        if any(
            t in name
            for t in (
                "motherboard",
                "mainboard",
                "pch",
                "chipset",
                "vrm",
                "mos",
                "systin",
                "auxtin",
                "cputin",
            )
        ) or any(p in blob for p in ("/lpc/", "/motherboard", "mainboard", "nct", "it87", "superio")):
            # cputin is often MB sensor near CPU socket — keep as motherboard
            if "cputin" in name:
                return "motherboard"
            if "cpu" in name and "cputin" not in name:
                return "cpu"
            return "motherboard"
        return None

    @property
    def latest(self) -> Optional[MetricsSnapshot]:
        with self._lock:
            return self._latest

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    def pause(self) -> None:
        from logutil import log

        if not self._paused.is_set():
            self._paused.set()
            log("monitor paused")

    def resume(self, collect_now: bool = True) -> None:
        from logutil import log

        was = self._paused.is_set()
        self._paused.clear()
        if was:
            log("monitor resumed")
        if collect_now and was:
            try:
                snap = self.collect()
                with self._lock:
                    self._latest = snap
                if self.on_update:
                    self.on_update(snap)
            except Exception as exc:
                log(f"monitor collect error: {exc}")

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._paused.clear()
        self._thread = threading.Thread(target=self._loop, name="SystemMonitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._paused.clear()
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)

    def _loop(self) -> None:
        from logutil import log

        while not self._stop.is_set():
            if self._paused.is_set():
                self._stop.wait(0.2)
                continue
            try:
                snap = self.collect()
                with self._lock:
                    self._latest = snap
                if self.on_update and not self._paused.is_set():
                    self.on_update(snap)
            except Exception as exc:
                log(f"monitor collect error: {exc}")
            wait = self.refresh_ms / 1000.0
            if self.perf_mode:
                wait = max(wait, 2.0)
            self._stop.wait(wait)

    def collect(self) -> MetricsSnapshot:
        now = time.time()
        dt = (now - self._prev_time) if self._prev_time else None
        self._cycle += 1

        per_core = psutil.cpu_percent(interval=None, percpu=True)
        overall = psutil.cpu_percent(interval=None)
        if not self._core_hists or len(self._core_hists) != len(per_core):
            self._core_hists = [deque(maxlen=3) for _ in per_core]
        for i, v in enumerate(per_core):
            self._core_hists[i].append(float(v))
        self._cpu_hist.append(float(overall))
        smooth_cores = [_avg(h) or 0.0 for h in self._core_hists]
        smooth_cpu = _avg(self._cpu_hist) or 0.0

        vm = psutil.virtual_memory()
        ram_pct = float(vm.percent)
        self._ram_hist.append(ram_pct)
        smooth_ram = _avg(self._ram_hist) or ram_pct
        ram_used = vm.used / (1024 ** 3)
        ram_total = vm.total / (1024 ** 3)

        disk_io = psutil.disk_io_counters()
        disk_r = disk_w = 0.0
        if disk_io and self._prev_disk and dt and dt > 0:
            disk_r = max(0.0, (disk_io.read_bytes - self._prev_disk.read_bytes) / dt / (1024 ** 2))
            disk_w = max(0.0, (disk_io.write_bytes - self._prev_disk.write_bytes) / dt / (1024 ** 2))
        if disk_io:
            self._prev_disk = disk_io
        self._disk_r_hist.append(disk_r)
        self._disk_w_hist.append(disk_w)

        disks: List[Dict[str, Any]] = []
        for part in psutil.disk_partitions(all=False):
            if "cdrom" in (part.opts or "").lower() or not part.fstype:
                continue
            try:
                usage = psutil.disk_usage(part.mountpoint)
                disks.append(
                    {
                        "device": part.device.rstrip("\\") or part.mountpoint,
                        "mount": part.mountpoint,
                        "percent": float(usage.percent),
                        "used_gb": usage.used / (1024 ** 3),
                        "total_gb": usage.total / (1024 ** 3),
                    }
                )
            except Exception:
                continue

        net = psutil.net_io_counters()
        net_up = net_down = 0.0
        if net and self._prev_net and dt and dt > 0:
            net_up = max(0.0, (net.bytes_sent - self._prev_net.bytes_sent) / dt / (1024 ** 2))
            net_down = max(0.0, (net.bytes_recv - self._prev_net.bytes_recv) / dt / (1024 ** 2))
        if net:
            self._prev_net = net
        self._net_up_hist.append(net_up)
        self._net_down_hist.append(net_down)

        heavy_every = 3 if self.perf_mode else 2
        if self._cycle == 1 or self._cycle % heavy_every == 0:
            self._cached_temps = self._read_all_temps()
            self._cached_cpu_temp = self._cached_temps.get("cpu")
            self._cached_gpu = self._read_gpu()
            if self._cached_gpu.get("temp") is not None:
                self._cached_temps["gpu"] = self._cached_gpu.get("temp")

        gpu = self._cached_gpu
        if gpu.get("load") is not None:
            self._gpu_hist.append(float(gpu["load"]))
            gpu_load = _avg(self._gpu_hist)
        else:
            gpu_load = None

        procs_cpu, procs_ram, procs_disk = self._collect_processes(dt)
        self._prev_time = now

        return MetricsSnapshot(
            cpu_percent=smooth_cpu,
            cpu_per_core=smooth_cores,
            cpu_temp=self._cached_cpu_temp,
            gpu_load=gpu_load,
            gpu_vram_used=gpu.get("vram_used"),
            gpu_vram_total=gpu.get("vram_total"),
            gpu_temp=self._cached_temps.get("gpu") or gpu.get("temp"),
            temps=dict(self._cached_temps),
            ram_used_gb=ram_used,
            ram_total_gb=ram_total,
            ram_percent=smooth_ram,
            disk_read_mbs=_avg(self._disk_r_hist) or 0.0,
            disk_write_mbs=_avg(self._disk_w_hist) or 0.0,
            disks=disks,
            net_up_mbs=_avg(self._net_up_hist) or 0.0,
            net_down_mbs=_avg(self._net_down_hist) or 0.0,
            processes_cpu=procs_cpu,
            processes_ram=procs_ram,
            processes_disk=procs_disk,
            top_cpu=procs_cpu[:5],
            top_ram=procs_ram[:5],
            timestamp=now,
        )

    def _is_background(self, pid: int, name: str, username: Optional[str]) -> bool:
        now = time.time()
        cached = self._bg_cache.get(pid)
        if cached and (now - cached[1]) < self._bg_ttl:
            return cached[0]

        is_bg = False
        lname = (name or "").lower()
        if any(h in lname for h in _BG_NAME_HINTS):
            is_bg = True
        elif username:
            user_part = username.split("\\")[-1].upper()
            if user_part in _SERVICE_USERS:
                is_bg = True

        if not is_bg:
            try:
                # No visible window titles → likely background (cheap: num_threads + status)
                p = psutil.Process(pid)
                if p.status() in (psutil.STATUS_STOPPED,):
                    is_bg = True
            except Exception:
                is_bg = True

        self._bg_cache[pid] = (is_bg, now)
        # Opportunistic prune
        if len(self._bg_cache) > 800:
            cutoff = now - self._bg_ttl * 2
            self._bg_cache = {k: v for k, v in self._bg_cache.items() if v[1] >= cutoff}
        return is_bg

    def _collect_processes(
        self, dt: Optional[float]
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
        procs: List[Dict[str, Any]] = []
        seen_pids = set()
        new_io: Dict[int, Tuple[int, int]] = {}

        for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_info", "username", "exe"]):
            try:
                info = p.info
                pid = int(info.get("pid") or 0)
                if not pid:
                    continue
                seen_pids.add(pid)
                mem = info.get("memory_info")
                ram_mb = (mem.rss / (1024 ** 2)) if mem else 0.0
                name = info.get("name") or "?"
                username = info.get("username")
                exe = self._get_exe(pid, info.get("exe"))
                disk_r = disk_w = 0.0
                try:
                    io = p.io_counters()
                    rb, wb = int(io.read_bytes), int(io.write_bytes)
                    new_io[pid] = (rb, wb)
                    prev = self._prev_io.get(pid)
                    if prev and dt and dt > 0:
                        disk_r = max(0.0, (rb - prev[0]) / dt / (1024 ** 2))
                        disk_w = max(0.0, (wb - prev[1]) / dt / (1024 ** 2))
                except (psutil.AccessDenied, psutil.NoSuchProcess, AttributeError):
                    pass

                is_bg = self._is_background(pid, name, username)
                procs.append(
                    {
                        "name": name,
                        "pid": pid,
                        "exe": exe,
                        "cpu": float(info.get("cpu_percent") or 0.0),
                        "ram_mb": float(ram_mb),
                        "disk_r": disk_r,
                        "disk_w": disk_w,
                        "is_background": is_bg,
                        "type": "BG" if is_bg else "FG",
                    }
                )
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
            except Exception:
                continue

        self._prev_io = new_io
        # Drop bg cache for dead PIDs occasionally
        if self._cycle % 10 == 0:
            self._bg_cache = {k: v for k, v in self._bg_cache.items() if k in seen_pids}
            self._exe_cache = {k: v for k, v in self._exe_cache.items() if k in seen_pids}

        by_cpu = sorted(procs, key=lambda x: x["cpu"], reverse=True)
        by_ram = sorted(procs, key=lambda x: x["ram_mb"], reverse=True)
        by_disk = sorted(procs, key=lambda x: (x["disk_r"] + x["disk_w"]), reverse=True)
        return by_cpu, by_ram, by_disk

    def _get_exe(self, pid: int, exe_from_info: Optional[str]) -> str:
        now = time.time()
        cached = self._exe_cache.get(pid)
        if cached and (now - cached[1]) < self._exe_ttl:
            return cached[0]
        path = ""
        if exe_from_info and isinstance(exe_from_info, str):
            path = exe_from_info
        else:
            try:
                path = psutil.Process(pid).exe() or ""
            except Exception:
                path = ""
        self._exe_cache[pid] = (path, now)
        return path

    def _read_all_temps(self) -> Dict[str, Optional[float]]:
        result: Dict[str, Optional[float]] = {
            "cpu": None,
            "gpu": None,
            "motherboard": None,
            "ram": None,
        }
        buckets: Dict[str, List[float]] = {k: [] for k in result}

        if self._wmi_ohm is not None:
            try:
                for sensor in self._wmi_ohm.Sensor():
                    stype = (getattr(sensor, "SensorType", "") or "").lower()
                    if stype != "temperature":
                        continue
                    name = (getattr(sensor, "Name", "") or "").lower()
                    parent = (getattr(sensor, "Parent", "") or "").lower()
                    identifier = (getattr(sensor, "Identifier", "") or "").lower()
                    try:
                        val = float(sensor.Value)
                    except Exception:
                        continue
                    if not (0 < val < 150):
                        continue
                    key = self._classify_temp_sensor(name, parent, identifier)
                    if key:
                        buckets[key].append(val)
            except Exception:
                pass

        for key, vals in buckets.items():
            if vals:
                result[key] = max(vals)

        if result["cpu"] is None:
            result["cpu"] = self._read_cpu_temp_fallback()

        if result["gpu"] is None:
            nv = self._read_nvidia_smi()
            if nv.get("temp") is not None:
                result["gpu"] = nv["temp"]

        return result

    def _read_cpu_temp_fallback(self) -> Optional[float]:
        """ACPI / psutil only — LHM path already handled in _read_all_temps."""
        if self._wmi is not None:
            try:
                for zone in self._wmi.MSAcpi_ThermalZoneTemperature():
                    raw = float(zone.CurrentTemperature)
                    celsius = raw / 10.0 - 273.15
                    if 0 < celsius < 150:
                        return celsius
            except Exception:
                pass

        try:
            sensors = psutil.sensors_temperatures()
            if sensors:
                temps = []
                for entries in sensors.values():
                    for entry in entries:
                        if entry.current is not None and 0 < entry.current < 150:
                            temps.append(float(entry.current))
                if temps:
                    return max(temps)
        except Exception:
            pass
        return None

    def _read_nvidia_smi(self) -> Dict[str, Optional[float]]:
        result: Dict[str, Optional[float]] = {
            "load": None,
            "vram_used": None,
            "vram_total": None,
            "temp": None,
        }
        smi = self._nvidia_smi
        if not smi:
            return result
        try:
            proc = subprocess.run(
                [
                    smi,
                    "--query-gpu=temperature.gpu,utilization.gpu,memory.used,memory.total",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=1.5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                check=False,
            )
            if proc.returncode != 0 or not (proc.stdout or "").strip():
                return result
            line = proc.stdout.strip().splitlines()[0]
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 4:
                return result

            def _f(i: int) -> Optional[float]:
                try:
                    return float(parts[i])
                except (IndexError, ValueError):
                    return None

            result["temp"] = _f(0)
            result["load"] = _f(1)
            result["vram_used"] = _f(2)
            result["vram_total"] = _f(3)
        except Exception:
            pass
        return result

    def _read_gpu(self) -> Dict[str, Optional[float]]:
        result: Dict[str, Optional[float]] = {
            "load": None,
            "vram_used": None,
            "vram_total": None,
            "temp": None,
        }

        if GPUtil is not None:
            try:
                gpus = GPUtil.getGPUs()
                if gpus:
                    g = gpus[0]
                    result["load"] = float(g.load) * 100.0
                    result["vram_used"] = float(g.memoryUsed)
                    result["vram_total"] = float(g.memoryTotal)
                    if g.temperature is not None:
                        result["temp"] = float(g.temperature)
            except Exception:
                pass

        if result["temp"] is None or result["load"] is None:
            nv = self._read_nvidia_smi()
            for key in result:
                if result[key] is None and nv.get(key) is not None:
                    result[key] = nv[key]

        if self._wmi_ohm is not None and (
            result["temp"] is None or result["load"] is None or result["vram_used"] is None
        ):
            try:
                loads, temps, mem_used, mem_total = [], [], [], []
                for sensor in self._wmi_ohm.Sensor():
                    name = (getattr(sensor, "Name", "") or "").lower()
                    parent = (getattr(sensor, "Parent", "") or "").lower()
                    identifier = (getattr(sensor, "Identifier", "") or "").lower()
                    stype = (getattr(sensor, "SensorType", "") or "").lower()
                    blob = f"{name} {parent} {identifier}"
                    if "gpu" not in blob and "nvidia" not in blob and "amd" not in parent:
                        continue
                    try:
                        val = float(sensor.Value)
                    except Exception:
                        continue
                    if stype == "load" and ("core" in name or "gpu" in name or "d3d" in name):
                        loads.append(val)
                    elif stype == "temperature":
                        temps.append(val)
                    elif stype in ("smalldata", "data"):
                        if "used" in name:
                            mem_used.append(val)
                        if "total" in name:
                            mem_total.append(val)
                if result["load"] is None and loads:
                    result["load"] = max(loads)
                if result["temp"] is None and temps:
                    result["temp"] = max(temps)
                if result["vram_used"] is None and mem_used:
                    result["vram_used"] = max(mem_used)
                if result["vram_total"] is None and mem_total:
                    result["vram_total"] = max(mem_total)
            except Exception:
                pass

        return result


def terminate_process(pid: int) -> str:
    """Terminate a process by PID. Returns status: ok | denied | gone | error:..."""
    if pid <= 0:
        return "error:invalid_pid"
    if pid == os.getpid():
        return "error:self"
    try:
        proc = psutil.Process(pid)
        proc.terminate()
        try:
            proc.wait(timeout=1.0)
        except psutil.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=1.0)
        return "ok"
    except psutil.NoSuchProcess:
        return "gone"
    except psutil.AccessDenied:
        return "denied"
    except Exception as exc:
        return f"error:{exc}"
