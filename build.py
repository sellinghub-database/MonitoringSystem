"""Build System Monitor Overlay into a one-file Windows EXE via PyInstaller."""

from __future__ import annotations

import os
import subprocess
import sys

from tray import ensure_icon_file


def main() -> int:
    root = os.path.dirname(os.path.abspath(__file__))
    os.chdir(root)

    icon_path = os.path.join(root, "icon.ico")
    ensure_icon_file(icon_path)

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--noconsole",
        f"--icon={icon_path}",
        "--name",
        "SystemMonitorOverlay",
        "--hidden-import=wmi",
        "--hidden-import=pythoncom",
        "--hidden-import=pywintypes",
        "--hidden-import=pystray._win32",
        "--hidden-import=PIL._tkinter_finder",
        "--collect-all=pystray",
        "main.py",
    ]
    print("Running:", " ".join(cmd))
    result = subprocess.run(cmd, cwd=root)
    if result.returncode == 0:
        exe = os.path.join(root, "dist", "SystemMonitorOverlay.exe")
        print(f"Build OK: {exe}")
    else:
        print("Build failed.", file=sys.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
