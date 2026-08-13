# System Monitor Overlay

Compact always-on-top system monitoring widget for **Windows 11**.

## Features

- Real-time CPU, GPU, RAM, disk, network metrics
- Per-core CPU bars and top processes
- Dark glassmorphism overlay (frameless, no taskbar icon)
- Critical alerts with pulsing red border
- System tray: Show/Hide, Settings, Exit
- Autostart via `HKCU\...\Run`
- Optional click-through mode
- One-file EXE build with PyInstaller

## Requirements

- Windows 10/11
- Python 3.10+
- Git (only for `push_to_github.py`)

## Setup

```powershell
cd MonitoringSystem
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
py -3 main.py
```

## Usage

- Overlay appears in the **top-right** corner.
- Drag the title to move (when click-through is off).
- Right-click the **tray icon** for Show/Hide, Settings, Exit.
- Closing the overlay hides it to the tray; use **Exit** to quit.

### Settings

- Opacity
- Click-through
- Autostart on logon
- Custom alert thresholds (CPU/GPU temp, CPU/RAM load, process CPU)

### Alerts

Border and metric labels pulse bright red (`#ff2222`) when:

| Metric | Default threshold |
|--------|-------------------|
| CPU temp | > 85°C |
| GPU temp | > 85°C |
| CPU load | > 90% |
| RAM usage | > 90% |
| Process CPU | > 50% |

Normal value colors: green (`#00ff88`), yellow (`#ffcc00` above moderate), red (`#ff2222` critical).

## Build EXE

```powershell
py -3 build.py
```

Output: `dist\SystemMonitorOverlay.exe` (one-file, no console, with `icon.ico`).

Copy `config.json` next to the EXE if you want to ship defaults; the app also creates one on first run.

## Push to GitHub

Repo: https://github.com/sellinghub-database/MonitoringSystem.git

```powershell
py -3 push_to_github.py
```

Requires Git in PATH (or a standard Git for Windows install).

## Notes / limitations

- **GPU metrics** via `GPUtil` work best with **NVIDIA**. AMD/Intel iGPU often shows `N/A`.
- **Temperatures** on stock Windows are frequently unavailable without LibreHardwareMonitor (WMI). The app shows `N/A` instead of crashing.
- Values are smoothed with a rolling average of the last 3 samples to reduce jitter.
- Config is stored as `config.json` in the app directory (next to the EXE when frozen).

## Project layout

```
MonitoringSystem/
├── main.py              # Entry point
├── monitor.py           # Data collection
├── ui.py                # Overlay widget
├── tray.py              # System tray
├── settings.py          # Settings + config + autostart
├── config.json          # User preferences
├── build.py             # PyInstaller build
├── push_to_github.py    # Git commit & push helper
├── requirements.txt
└── README.md
```

## License

MIT
