"""Generate the .desktop entry for treg console, directly on this machine.

Writes (idempotently, user-level, no root needed):
  $XDG_DATA_HOME/applications/treg-console.desktop   (default ~/.local/share/applications)
  $XDG_DATA_HOME/icons/hicolor/{256,512}x{256,512}/apps/treg-console.png

The .desktop file is what makes the app appear in the system application menu
and lets the desktop environment (GNOME/KDE) associate windows with it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

APP_ID = "treg.console"  # must match the GApplication id in app.py
CMD_NAME = "treg-console"  # installed launcher command
ICON_NAME = "treg-console"  # icon name in the hicolor theme
APP_NAME = "treg console"
APP_COMMENT = "Desktop console for the treg tool catalog — search, call, audit 4,000+ endpoints, and summarize web results with an LLM"
ICON_SRC = Path(__file__).parent / "icon.png"


def _data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))


def _exec_command() -> str:
    """How to launch the app from the menu: the installed command if on PATH,
    else the current interpreter running the module."""
    installed = shutil.which(CMD_NAME)
    if installed:
        return installed
    return f"{sys.executable} -m treg_console"


def desktop_file_content(exec_cmd: str) -> str:
    return f"""[Desktop Entry]
Version=1.0
Type=Application
Name={APP_NAME}
Comment={APP_COMMENT}
Exec={exec_cmd}
Icon={ICON_NAME}
Terminal=false
Categories=Development;
Keywords=treg;api;tools;catalog;endpoints;summarize;
StartupNotify=true
"""


def install_desktop_entry() -> Path | None:
    """Write the .desktop file and icon to the user's XDG dirs. Returns the
    .desktop path, or None if it could not be written."""
    try:
        # icon into the hicolor theme (both common sizes)
        if ICON_SRC.is_file():
            for size in (256, 512):
                d = _data_home() / "icons" / "hicolor" / f"{size}x{size}" / "apps"
                d.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ICON_SRC, d / f"{ICON_NAME}.png")

        apps = _data_home() / "applications"
        apps.mkdir(parents=True, exist_ok=True)
        target = apps / f"{APP_ID}.desktop"
        target.write_text(desktop_file_content(_exec_command()))
        target.chmod(0o755)

        # best-effort validation
        validator = shutil.which("desktop-file-validate")
        if validator:
            r = subprocess.run([validator, str(target)], capture_output=True, text=True)
            if r.returncode != 0:
                print(f"warning: desktop-file-validate: {r.stdout.strip()}", file=sys.stderr)
        return target
    except OSError as e:
        print(f"warning: could not install .desktop entry: {e}", file=sys.stderr)
        return None


def remove_desktop_entry() -> None:
    """Undo install_desktop_entry (the icon is left in place)."""
    target = _data_home() / "applications" / f"{APP_ID}.desktop"
    target.unlink(missing_ok=True)