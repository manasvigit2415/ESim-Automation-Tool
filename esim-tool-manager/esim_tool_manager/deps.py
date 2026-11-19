"""Dependency checker: verifies the system and each tool's prerequisites."""
from __future__ import annotations

import shutil
import socket
import sys
from dataclasses import dataclass
from typing import List

from . import platform_utils as pu
from .manager import ToolManager
from .registry import Tool
from .version_utils import compare

MIN_PYTHON = (3, 9)
MIN_FREE_GB = 2.0


@dataclass
class Check:
    name: str
    level: str      # "ok" | "warn" | "error"
    message: str
    hint: str = ""


def system_checks(mgr: ToolManager) -> List[Check]:
    out: List[Check] = []
    py = sys.version_info[:2]
    out.append(Check("python", "ok" if py >= MIN_PYTHON else "error",
                     f"Python {py[0]}.{py[1]}",
                     "" if py >= MIN_PYTHON else f"Python >= {MIN_PYTHON[0]}.{MIN_PYTHON[1]} required"))

    if pu.externally_managed() and not mgr.pip.allow_system:
        out.append(Check("python env", "warn", "system Python is externally managed (PEP 668)",
                         "pip installs need a virtualenv: python3 -m venv .venv && source .venv/bin/activate"))
    else:
        out.append(Check("python env", "ok", "virtualenv" if pu.in_virtualenv() else "pip can install here"))

    b = mgr.system_backend()
    if b:
        out.append(Check("package manager", "ok", b.name))
    else:
        hint = {"windows": "Install Chocolatey: https://chocolatey.org/install",
                "darwin": "Install Homebrew: https://brew.sh"}.get(pu.os_name(), "No apt/dnf/pacman found")
        out.append(Check("package manager", "error", "none detected", hint))

    if b and b.needs_root and not pu.is_admin():
        has_sudo = shutil.which("sudo") is not None
        out.append(Check("privileges", "ok" if has_sudo else "error",
                         "will use sudo" if has_sudo else "not root and no sudo",
                         "" if has_sudo else "Run as root or install sudo"))
    elif pu.os_name() == "windows" and not pu.is_admin():
        out.append(Check("privileges", "warn", "not elevated",
                         "Run the terminal as Administrator for Chocolatey installs"))
    else:
        out.append(Check("privileges", "ok", "sufficient"))

    free = shutil.disk_usage(mgr.config.path.parent).free / 1e9
    out.append(Check("disk space", "ok" if free >= MIN_FREE_GB else "warn",
                     f"{free:.1f} GB free", "" if free >= MIN_FREE_GB else "KiCad needs ~2 GB"))
    try:
        socket.create_connection(("pypi.org", 443), timeout=3).close()
        out.append(Check("network", "ok", "reachable"))
    except OSError:
        out.append(Check("network", "warn", "pypi.org unreachable",
                         "Installs and update checks need internet access"))
    return out


def tool_checks(mgr: ToolManager, tool: Tool) -> List[Check]:
    out: List[Check] = []
    backend = mgr.backend_for(tool)
    st = mgr.status(tool)
    if st.installed:
        if st.meets_min and tool.recommended_version and st.version \
                and compare(st.version, tool.recommended_version) < 0:
            out.append(Check(tool.id, "warn", f"{st.version} works (min {tool.min_version}) but "
                             f"{tool.recommended_version} is recommended",
                             f"A newer build is available from {tool.homepage}"))
        elif st.meets_min:
            out.append(Check(tool.id, "ok", f"{st.version or 'installed'} (min {tool.min_version or 'n/a'})"))
        else:
            out.append(Check(tool.id, "error", f"{st.version} is older than required {tool.min_version}",
                             f"Run: {pu.prog()} update {tool.id}"))
    else:
        level = "error" if tool.required else "warn"
        if backend and tool.packages.get(backend.name):
            out.append(Check(tool.id, level, "missing", f"Run: {pu.prog()} install {tool.id}"))
        else:
            out.append(Check(tool.id, level, "missing, no package for this platform",
                             f"Install manually: {tool.homepage}"))
    for dep in tool.requires:
        d = mgr.status(mgr.get_tool(dep))
        if not d.installed:
            out.append(Check(f"{tool.id} -> {dep}", "error", "required dependency missing",
                             f"Run: {pu.prog()} install {dep}"))
        elif not d.meets_min:
            out.append(Check(f"{tool.id} -> {dep}", "error",
                             f"{dep} {d.version} too old (needs >= {d.tool.min_version})"))
    return out


MARKS = {"ok": "[ OK ]", "warn": "[WARN]", "error": "[FAIL]"}


def run_doctor(mgr: ToolManager, tool_ids: List[str]) -> tuple[List[str], int, int]:
    """Run every check; return (report lines, error count, warning count).
    Shared by the CLI `doctor` command and the GUI."""
    checks = system_checks(mgr)
    for tid in tool_ids:
        checks += tool_checks(mgr, mgr.get_tool(tid))
    lines: List[str] = []
    for c in checks:
        lines.append(f"{MARKS[c.level]} {c.name}: {c.message}")
        if c.hint and c.level != "ok":
            lines.append(f"         -> {c.hint}")
    errors = sum(c.level == "error" for c in checks)
    warnings = sum(c.level == "warn" for c in checks)
    lines += ["", f"{errors} error(s), {warnings} warning(s)."]
    return lines, errors, warnings
