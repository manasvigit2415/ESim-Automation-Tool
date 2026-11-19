"""Host-platform detection helpers."""
from __future__ import annotations

import os
import platform
import sys
import sysconfig
from pathlib import Path
from typing import Dict


def os_name() -> str:
    """'linux', 'darwin' or 'windows'."""
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


def distro_info() -> Dict[str, str]:
    """Parse /etc/os-release on Linux (empty dict elsewhere)."""
    info: Dict[str, str] = {}
    try:
        with open("/etc/os-release", encoding="utf-8") as fh:
            for line in fh:
                if "=" in line:
                    k, v = line.rstrip().split("=", 1)
                    info[k] = v.strip('"')
    except OSError:
        pass
    return info


def is_admin() -> bool:
    try:
        if os_name() == "windows":
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        return os.geteuid() == 0
    except Exception:
        return False


def summary() -> Dict[str, str]:
    d = distro_info()
    return {
        "os": os_name(),
        "distro": d.get("PRETTY_NAME", platform.platform()),
        "arch": platform.machine(),
        "python": platform.python_version(),
        "admin": str(is_admin()),
    }


def in_virtualenv() -> bool:
    return sys.prefix != getattr(sys, "base_prefix", sys.prefix)


def externally_managed() -> bool:
    """True when pip must not modify this interpreter (PEP 668), i.e. a
    Debian/Ubuntu/Homebrew system Python outside a virtualenv."""
    if in_virtualenv():
        return False
    return (Path(sysconfig.get_path("stdlib")) / "EXTERNALLY-MANAGED").exists()


def prog() -> str:
    """Command name to show in hints: `esim-tm` when installed via pip,
    otherwise the `python -m` form that actually works for this user."""
    name = Path(sys.argv[0]).name if sys.argv and sys.argv[0] else ""
    if name in ("esim-tm", "esim-tm.exe"):
        return "esim-tm"
    return "python -m esim_tool_manager" if os_name() == "windows" else "python3 -m esim_tool_manager"
