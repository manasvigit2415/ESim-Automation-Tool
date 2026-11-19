"""Package-manager backends.

Every backend implements the same small interface, so the ToolManager never
contains OS-specific logic. To support another package manager, subclass
`Backend` and add it to `detect_backends`.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import urllib.request
from typing import List, Optional

from . import platform_utils as pu
from .version_utils import compare, parse


def capture(cmd: List[str], timeout: int = 60, merge: bool = False) -> tuple[int, str]:
    """Run a command and return (returncode, output). Never raises."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return 127, ""
    return p.returncode, (p.stdout or "") + ((p.stderr or "") if merge else "")


class Backend:
    name = ""
    exe = ""
    needs_root = False
    supports_pin = False

    def available(self) -> bool:
        return shutil.which(self.exe) is not None

    def installed_version(self, pkg: str) -> Optional[str]:
        raise NotImplementedError

    def candidate_version(self, pkg: str) -> Optional[str]:
        raise NotImplementedError

    def list_versions(self, pkg: str) -> List[str]:
        return []

    def resolve_version(self, pkg: str, wanted: str) -> Optional[str]:
        """Map a user version ('42') to the backend's exact string ('42+ds-1')."""
        for v in self.list_versions(pkg):
            if compare(v, wanted) == 0:
                return v
        return None

    def install_cmd(self, pkg: str, version: Optional[str] = None) -> List[str]:
        raise NotImplementedError

    def upgrade_cmd(self, pkg: str) -> List[str]:
        raise NotImplementedError

    def refresh_cmd(self) -> Optional[List[str]]:
        return None


class AptBackend(Backend):
    name, exe, needs_root, supports_pin = "apt", "apt-get", True, True

    def installed_version(self, pkg):
        rc, out = capture(["dpkg-query", "-W", "-f=${Status}|${Version}", pkg])
        if rc != 0 or "install ok installed" not in out:
            return None
        return out.split("|", 1)[1].strip()

    def candidate_version(self, pkg):
        _, out = capture(["apt-cache", "policy", pkg])
        m = re.search(r"Candidate:\s*(\S+)", out)
        return m.group(1) if m and m.group(1) != "(none)" else None

    def list_versions(self, pkg):
        _, out = capture(["apt-cache", "madison", pkg])
        return [f.split("|")[1].strip() for f in out.splitlines() if f.count("|") >= 2]

    def install_cmd(self, pkg, version=None):
        return ["apt-get", "install", "-y", f"{pkg}={version}" if version else pkg]

    def upgrade_cmd(self, pkg):
        return ["apt-get", "install", "-y", "--only-upgrade", pkg]

    def refresh_cmd(self):
        return ["apt-get", "update"]


class DnfBackend(Backend):
    name, exe, needs_root = "dnf", "dnf", True

    def installed_version(self, pkg):
        rc, out = capture(["rpm", "-q", "--qf", "%{VERSION}", pkg])
        return out.strip() if rc == 0 else None

    def candidate_version(self, pkg):
        _, out = capture(["dnf", "-q", "info", "--available", pkg])
        m = re.search(r"^Version\s*:\s*(\S+)", out, re.M)
        return m.group(1) if m else None

    def install_cmd(self, pkg, version=None):
        return ["dnf", "install", "-y", pkg]

    def upgrade_cmd(self, pkg):
        return ["dnf", "upgrade", "-y", pkg]


class PacmanBackend(Backend):
    name, exe, needs_root = "pacman", "pacman", True

    def installed_version(self, pkg):
        rc, out = capture(["pacman", "-Q", pkg])
        return out.split()[1] if rc == 0 and len(out.split()) > 1 else None

    def candidate_version(self, pkg):
        _, out = capture(["pacman", "-Si", pkg])
        m = re.search(r"^Version\s*:\s*(\S+)", out, re.M)
        return m.group(1) if m else None

    def install_cmd(self, pkg, version=None):
        return ["pacman", "-S", "--noconfirm", "--needed", pkg]

    def upgrade_cmd(self, pkg):
        return ["pacman", "-S", "--noconfirm", pkg]

    def refresh_cmd(self):
        return ["pacman", "-Sy"]


class BrewBackend(Backend):
    """Homebrew. A package spec 'cask:kicad' selects a cask."""
    name, exe = "brew", "brew"

    @staticmethod
    def _split(pkg):
        return (True, pkg[5:]) if pkg.startswith("cask:") else (False, pkg)

    def installed_version(self, pkg):
        cask, p = self._split(pkg)
        rc, out = capture(["brew", "list", *(["--cask"] if cask else []), "--versions", p])
        parts = out.split()
        return parts[1] if rc == 0 and len(parts) > 1 else None

    def candidate_version(self, pkg):
        cask, p = self._split(pkg)
        rc, out = capture(["brew", "info", "--json=v2", *(["--cask"] if cask else []), p])
        try:
            data = json.loads(out)
            if cask:
                return data["casks"][0]["version"]
            return data["formulae"][0]["versions"]["stable"]
        except (ValueError, KeyError, IndexError):
            return None

    def install_cmd(self, pkg, version=None):
        cask, p = self._split(pkg)
        return ["brew", "install", *(["--cask"] if cask else []), p]

    def upgrade_cmd(self, pkg):
        cask, p = self._split(pkg)
        return ["brew", "upgrade", *(["--cask"] if cask else []), p]

    def refresh_cmd(self):
        return ["brew", "update"]


class ChocoBackend(Backend):
    name, exe, supports_pin = "choco", "choco", True

    def installed_version(self, pkg):
        rc, out = capture(["choco", "list", "--local-only", "--exact", pkg, "--limit-output"])
        for line in out.splitlines():
            if line.lower().startswith(pkg.lower() + "|"):
                return line.split("|")[1].strip()
        return None

    def candidate_version(self, pkg):
        _, out = capture(["choco", "search", pkg, "--exact", "--limit-output"])
        for line in out.splitlines():
            if line.lower().startswith(pkg.lower() + "|"):
                return line.split("|")[1].strip()
        return None

    def list_versions(self, pkg):
        _, out = capture(["choco", "search", pkg, "--exact", "--all-versions", "--limit-output"])
        return [l.split("|")[1].strip() for l in out.splitlines() if "|" in l]

    def install_cmd(self, pkg, version=None):
        cmd = ["choco", "install", pkg, "-y"]
        return cmd + [f"--version={version}"] if version else cmd

    def upgrade_cmd(self, pkg):
        return ["choco", "upgrade", pkg, "-y"]


class WingetBackend(Backend):
    """Windows Package Manager. Output is a human-readable table, so parsing is
    token based: the version is the token after the exact package id."""
    name, exe, supports_pin = "winget", "winget", True
    _FLAGS = ["--accept-source-agreements", "--disable-interactivity"]

    @staticmethod
    def parse_installed(output: str, pkg: str) -> Optional[str]:
        for line in output.splitlines():
            tokens = line.split()
            if pkg in tokens and tokens.index(pkg) + 1 < len(tokens):
                return tokens[tokens.index(pkg) + 1]
        return None

    @staticmethod
    def parse_versions(output: str) -> List[str]:
        """Lines after the '----' rule of `winget show --versions`, newest first."""
        versions, seen_rule = [], False
        for line in output.splitlines():
            line = line.strip()
            if set(line) == {"-"}:
                seen_rule = True
            elif seen_rule and re.fullmatch(r"\d[\w.+~-]*", line):
                versions.append(line)
        return versions

    def installed_version(self, pkg):
        rc, out = capture(["winget", "list", "--id", pkg, "--exact", *self._FLAGS])
        return self.parse_installed(out, pkg) if rc == 0 else None

    def candidate_version(self, pkg):
        _, out = capture(["winget", "show", "--id", pkg, "--exact", *self._FLAGS])
        m = re.search(r"^Version:\s*(\S+)", out, re.M)
        return m.group(1) if m else None

    def list_versions(self, pkg):
        _, out = capture(["winget", "show", "--id", pkg, "--exact", "--versions", *self._FLAGS])
        return self.parse_versions(out)

    def install_cmd(self, pkg, version=None):
        cmd = ["winget", "install", "--id", pkg, "--exact", "--silent",
               "--accept-package-agreements", "--accept-source-agreements"]
        return cmd + ["--version", version] if version else cmd

    def upgrade_cmd(self, pkg):
        return ["winget", "upgrade", "--id", pkg, "--exact", "--silent",
                "--accept-package-agreements", "--accept-source-agreements"]


class PipBackend(Backend):
    """Python packages, installed into the interpreter running the manager.

    On a PEP 668 "externally managed" system Python (Ubuntu 23.04+, Debian 12+,
    Homebrew) pip refuses to install outside a virtualenv. We detect that up
    front (`blocked_reason`) instead of failing with a raw pip error, and only
    pass --break-system-packages if the user explicitly opted in.
    """
    name, exe, supports_pin = "pip", sys.executable, True

    def __init__(self, allow_system: bool = False) -> None:
        self.allow_system = allow_system

    def available(self) -> bool:
        return True

    def blocked_reason(self) -> Optional[str]:
        if pu.externally_managed() and not self.allow_system:
            return ("this Python is externally managed (PEP 668), so pip cannot install into it. "
                    "Create a virtualenv first:  python3 -m venv .venv && source .venv/bin/activate  "
                    "(then run this tool again), or pass --break-system-packages to override.")
        return None

    def installed_version(self, pkg):
        from importlib import metadata
        try:
            return metadata.version(pkg)
        except metadata.PackageNotFoundError:
            return None

    def _pypi(self, pkg) -> Optional[dict]:
        try:
            with urllib.request.urlopen(f"https://pypi.org/pypi/{pkg}/json", timeout=8) as r:
                return json.load(r)
        except Exception:
            return None

    def candidate_version(self, pkg):
        data = self._pypi(pkg)
        return data["info"]["version"] if data else None

    def list_versions(self, pkg):
        data = self._pypi(pkg)
        return sorted(data["releases"], key=parse) if data else []

    def resolve_version(self, pkg, wanted):
        return wanted  # pip validates the pin itself

    def _base(self):
        cmd = [sys.executable, "-m", "pip", "install"]
        if not pu.in_virtualenv():
            cmd.append("--user")
            if pu.externally_managed() and self.allow_system:
                cmd.append("--break-system-packages")
        return cmd

    def install_cmd(self, pkg, version=None):
        return self._base() + [f"{pkg}=={version}" if version else pkg]

    def upgrade_cmd(self, pkg):
        return self._base() + ["--upgrade", pkg]


def detect_backends() -> List[Backend]:
    """System package managers available on this host, best first."""
    order = {"linux": [AptBackend, DnfBackend, PacmanBackend, BrewBackend],
             "darwin": [BrewBackend],
             "windows": [WingetBackend, ChocoBackend]}[pu.os_name()]
    return [b() for b in order if b().available()]
