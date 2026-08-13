"""Initialize remote (if needed) and push the project to GitHub."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

REPO_URL = "https://github.com/sellinghub-database/MonitoringSystem.git"
COMMIT_MSG = "feat: system monitor overlay v1.0"


def run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    print(">", " ".join(cmd))
    return subprocess.run(cmd, check=check)


def find_git() -> str:
    git = shutil.which("git")
    if git:
        return git
    candidates = [
        r"C:\Program Files\Git\cmd\git.exe",
        r"C:\Program Files (x86)\Git\cmd\git.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Git\cmd\git.exe"),
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    raise FileNotFoundError(
        "Git was not found in PATH. Install Git for Windows and re-open the terminal:\n"
        "https://git-scm.com/download/win"
    )


def main() -> int:
    root = os.path.dirname(os.path.abspath(__file__))
    os.chdir(root)

    try:
        git = find_git()
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 1

    def g(*args: str, check: bool = True) -> subprocess.CompletedProcess:
        return run([git, *args], check=check)

    if not os.path.isdir(os.path.join(root, ".git")):
        g("init")
        g("branch", "-M", "main")

    remotes = subprocess.run([git, "remote"], capture_output=True, text=True, check=False)
    if "origin" not in (remotes.stdout or "").split():
        g("remote", "add", "origin", REPO_URL)
    else:
        g("remote", "set-url", "origin", REPO_URL)

    g("add", ".")
    # Commit may fail if nothing changed — that's fine
    commit = g("commit", "-m", COMMIT_MSG, check=False)
    if commit.returncode not in (0, 1):
        return commit.returncode

    push = g("push", "-u", "origin", "main", check=False)
    if push.returncode != 0:
        print(
            "Push failed. Check credentials / remote access, then retry:\n"
            f"  {git} push -u origin main",
            file=sys.stderr,
        )
        return push.returncode

    print("Pushed to", REPO_URL)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
