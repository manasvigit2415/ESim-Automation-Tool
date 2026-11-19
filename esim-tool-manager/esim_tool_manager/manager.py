"""ToolManager: orchestrates probing, installing and updating tools."""
from __future__ import annotations

import glob
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections import deque
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from . import platform_utils as pu
from .backends import Backend, PipBackend, capture, detect_backends
from .config import Config
from .logger import ActionLog, get_logger
from .registry import Probe, Tool, load_registry
from .version_utils import compare, parse


@dataclass
class ToolStatus:
    tool: Tool
    installed: bool
    version: Optional[str] = None       # None if installed but version unparsable
    path: Optional[str] = None
    managed_by: Optional[str] = None    # backend name, "manual" or None
    candidate: Optional[str] = None     # newest version the backend offers

    @property
    def meets_min(self) -> bool:
        if not self.installed:
            return False
        if not self.tool.min_version or not parse(self.version or ""):
            return True                  # nothing to compare: assume OK
        return compare(self.version, self.tool.min_version) >= 0

    @property
    def update_available(self) -> bool:
        return bool(self.installed and self.version and self.candidate
                    and compare(self.candidate, self.version) > 0)


class ToolManager:
    def __init__(self, config: Optional[Config] = None,
                 registry: Optional[Dict[str, Tool]] = None,
                 backends: Optional[List[Backend]] = None,
                 dry_run: bool = False, assume_yes: bool = False,
                 allow_system_pip: bool = False,
                 out: Callable[[str], None] = print) -> None:
        self.config = config or Config.load()
        self.registry = registry if registry is not None else load_registry()
        self.backends = backends if backends is not None else detect_backends()
        self.pip = PipBackend(allow_system=allow_system_pip)
        self.dry_run, self.assume_yes, self._out = dry_run, assume_yes, out
        self.log = get_logger()
        self.actions = ActionLog()
        self._refreshed: set = set()
        self.elevate: List[str] = ["sudo"]          # GUI swaps in pkexec
        self.last_output: List[str] = []             # tail of the last command

    # ---------- helpers ----------
    def say(self, msg: str) -> None:
        self._out(msg)
        self.log.info(msg)

    def get_tool(self, tool_id: str) -> Tool:
        try:
            return self.registry[tool_id.lower()]
        except KeyError:
            raise KeyError(f"unknown tool '{tool_id}'. Known: {', '.join(self.registry)}") from None

    def _ordered_backends(self) -> List[Backend]:
        """Configured backend first (sort is stable), the rest in detection order."""
        pref = self.config.get("preferred_backend")
        return sorted(self.backends, key=lambda b: b.name != pref)

    def system_backend(self) -> Optional[Backend]:
        ordered = self._ordered_backends()
        return ordered[0] if ordered else None

    def backend_for(self, tool: Tool) -> Optional[Backend]:
        """pip for Python packages; for system tools the first available backend
        that has a package id for this tool (e.g. winget lacks GHDL, choco has it)."""
        if tool.kind == "pip":
            return self.pip
        ordered = self._ordered_backends()
        for b in ordered:
            if tool.packages.get(b.name):
                return b
        return ordered[0] if ordered else None

    def package_for(self, tool: Tool) -> Optional[str]:
        b = self.backend_for(tool)
        return tool.packages.get(b.name) if b else None

    def confirm(self, msg: str) -> bool:
        if self.assume_yes or self.dry_run:
            return True
        if not sys.stdin.isatty():
            self.say(f"{msg} -> non-interactive session, re-run with -y to proceed")
            return False
        return input(f"{msg} [y/N] ").strip().lower() in ("y", "yes")

    def _run(self, backend: Backend, cmd: List[str]) -> int:
        """Run a command, streaming its output to the user while keeping the tail
        so a failure can be explained in the history and the debug log."""
        if backend.needs_root and not pu.is_admin() and shutil.which(self.elevate[0]):
            cmd = self.elevate + cmd
        text = shlex.join(cmd)
        self.log.info("RUN %s", text)
        self.last_output = []
        if self.dry_run:
            self._out(f"  [dry-run] {text}")
            return 0
        tail: deque = deque(maxlen=40)
        try:
            with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  text=True, errors="replace", bufsize=1) as proc:
                for line in proc.stdout:
                    line = line.rstrip()
                    if line:
                        tail.append(line)
                        self.log.debug("| %s", line)
                        self._out(line)
                rc = proc.wait()
        except OSError as exc:
            self.say(f"  could not run {cmd[0]}: {exc}")
            self.last_output = [str(exc)]
            return 127
        self.last_output = list(tail)
        self.log.info("rc=%s", rc)
        if rc != 0:
            self.log.error("command failed (rc=%s): %s\n%s", rc, text, "\n".join(tail))
        return rc

    _ERROR_LINE = re.compile(r"(^E:|error|fail|unable|cannot|can't|not found|denied|no such)", re.I)

    def _failure(self, rc: int) -> str:
        """One-line reason for the last failed command: the last line that looks
        like an error, else simply the last line of output."""
        lines = self.last_output
        pick = next((l for l in reversed(lines) if self._ERROR_LINE.search(l)), lines[-1] if lines else "")
        return f"exit code {rc}" + (f": {pick[:240]}" if pick else "")

    def _refresh_index(self, backend: Backend) -> None:
        cmd = backend.refresh_cmd()
        if cmd and backend.name not in self._refreshed:
            self._refreshed.add(backend.name)
            self._run(backend, cmd)

    # ---------- probing ----------
    def find_binary(self, probe: Probe) -> Optional[str]:
        """Locate an executable: extra_paths from config, then PATH, then the
        per-OS well-known install locations."""
        dirs = self.config.get("extra_paths") or []
        if dirs:
            found = shutil.which(probe.binary, path=os.pathsep.join(dirs))
            if found:
                return found
        found = shutil.which(probe.binary)
        if found:
            return found
        for pattern in probe.search_paths.get(pu.os_name(), []):
            matches = sorted(glob.glob(os.path.expandvars(os.path.expanduser(pattern))), reverse=True)
            for m in matches:
                if os.path.isfile(m):
                    return m
        return None

    def probe(self, tool: Tool) -> Tuple[bool, Optional[str], Optional[str]]:
        """Return (installed, path, version) by inspecting the real system.

        Probes are tried in order (e.g. `kicad-cli`, then `kicad` for KiCad 6).
        A binary that is found but prints no recognisable version still counts
        as installed, with version None, rather than being reported missing.
        """
        if tool.kind == "pip":
            v = self.pip.installed_version(tool.pip_dist)
            return v is not None, None, v
        first_path = None
        for pr in tool.probes():
            path = self.find_binary(pr)
            if not path:
                continue
            first_path = first_path or path
            _, out = capture([path] + pr.version_cmd[1:], timeout=20, merge=True)
            m = re.search(tool.version_regex, out)
            if m:
                return True, path, m.group(1)
        return (first_path is not None), first_path, None

    def status(self, tool: Tool, with_candidate: bool = False) -> ToolStatus:
        installed, path, version = self.probe(tool)
        backend, pkg = self.backend_for(tool), self.package_for(tool)
        managed = None
        if installed:
            managed = "manual"
            if backend and pkg and backend.installed_version(pkg):
                managed = backend.name
        st = ToolStatus(tool, installed, version, path, managed)
        if with_candidate and backend and pkg:
            st.candidate = backend.candidate_version(pkg)
        return st

    # ---------- install ----------
    def install(self, tool_id: str, version: Optional[str] = None, force: bool = False) -> bool:
        tool = self.get_tool(tool_id)
        backend, pkg = self.backend_for(tool), self.package_for(tool)

        if backend is None:
            self.say(f"[{tool.id}] no supported package manager found on this system")
            self.actions.record("install", tool.id, "error", "no package manager")
            return False
        if not pkg:
            self.say(f"[{tool.id}] no '{backend.name}' package defined. Install manually: {tool.homepage}")
            self.actions.record("install", tool.id, "error", f"no {backend.name} package")
            return False
        blocked = backend.blocked_reason() if hasattr(backend, "blocked_reason") else None
        if blocked:
            self.say(f"[{tool.id}] cannot install: {blocked}")
            self.actions.record("install", tool.id, "error", "externally-managed Python")
            return False

        for dep in tool.requires:                      # dependencies first
            if not self.status(self.get_tool(dep)).installed:
                self.say(f"[{tool.id}] installing dependency '{dep}' first")
                if not self.install(dep):
                    self.actions.record("install", tool.id, "error", f"dependency {dep} failed")
                    return False

        wanted = version or (self.config.get("pinned_versions") or {}).get(tool.id)
        st = self.status(tool)
        if st.installed and not force and (not wanted or (st.version and compare(st.version, wanted) == 0)):
            self.say(f"[{tool.id}] already installed ({st.version or 'unknown version'}), skipping")
            self.actions.record("install", tool.id, "skipped", f"already installed {st.version}")
            return True

        exact = None
        if wanted:
            if not backend.supports_pin:
                self.say(f"[{tool.id}] {backend.name} cannot pin versions; installing the latest instead")
            else:
                exact = backend.resolve_version(pkg, wanted)
                if exact is None:
                    avail = ", ".join(backend.list_versions(pkg)[-5:]) or "none found"
                    self.say(f"[{tool.id}] version {wanted} not offered by {backend.name} (available: {avail})")
                    self.actions.record("install", tool.id, "error", f"version {wanted} unavailable")
                    return False

        label = f"{tool.name} {exact or 'latest'} via {backend.name}"
        if not self.confirm(f"[{tool.id}] install {label}?"):
            self.actions.record("install", tool.id, "cancelled", label)
            return False

        self.say(f"[{tool.id}] installing {label}")
        self._refresh_index(backend)
        rc = self._run(backend, backend.install_cmd(pkg, exact))
        if rc != 0:
            self.say(f"[{tool.id}] install FAILED (exit code {rc}); see `{pu.prog()} log`")
            if backend.name == "pip":
                self.say(f"[{tool.id}] hint: if pip reported a build error, this package may not support "
                         f"Python {sys.version_info.major}.{sys.version_info.minor} yet")
            self.actions.record("install", tool.id, "error", self._failure(rc))
            return False
        if self.dry_run:
            self.actions.record("install", tool.id, "dry-run", label)
            return True

        new = self.status(tool)
        if exact and new.installed and new.version and compare(new.version, wanted) != 0:
            self.say(f"[{tool.id}] installed version {new.version} does not match the requested {wanted}")
            self.actions.record("install", tool.id, "error", f"version {new.version} != requested {wanted}")
            return False
        if not new.installed:
            self.say(f"[{tool.id}] package installed but binary '{tool.binary}' not found on PATH")
            self.actions.record("install", tool.id, "error", "binary not found after install")
            return False
        if not new.meets_min:
            self.say(f"[{tool.id}] WARNING: installed {new.version}, eSim needs >= {tool.min_version}. "
                     f"Try a newer repository or install manually: {tool.homepage}")
            self.actions.record("install", tool.id, "warning", f"version {new.version} < {tool.min_version}")
            return True
        self.say(f"[{tool.id}] OK, version {new.version}")
        self.actions.record("install", tool.id, "success", f"version {new.version}")
        return True

    # ---------- update ----------
    def check_updates(self, tool_ids: Optional[List[str]] = None) -> List[ToolStatus]:
        tools = [self.get_tool(t) for t in tool_ids] if tool_ids else list(self.registry.values())
        res = [self.status(t, with_candidate=True) for t in tools]
        self.actions.record("check-updates", ",".join(t.id for t in tools), "success",
                            f"{sum(s.update_available for s in res)} update(s) available")
        return res

    def update(self, tool_id: str) -> bool:
        tool = self.get_tool(tool_id)
        backend, pkg = self.backend_for(tool), self.package_for(tool)
        st = self.status(tool, with_candidate=True)
        if not st.installed:
            self.say(f"[{tool.id}] not installed (use `{pu.prog()} install {tool.id}`)")
            return False
        pin = (self.config.get("pinned_versions") or {}).get(tool.id)
        if pin:
            self.say(f"[{tool.id}] pinned to {pin}, skipping (use `{pu.prog()} config unpin {tool.id}`)")
            self.actions.record("update", tool.id, "skipped", f"pinned {pin}")
            return True
        if not (backend and pkg) or st.managed_by != backend.name:
            self.say(f"[{tool.id}] not managed by {backend.name if backend else 'a package manager'} "
                     f"(installed manually), update it yourself: {tool.homepage}")
            self.actions.record("update", tool.id, "skipped", "not package-managed")
            return True
        if not st.update_available:
            self.say(f"[{tool.id}] up to date ({st.version})")
            return True
        blocked = backend.blocked_reason() if hasattr(backend, "blocked_reason") else None
        if blocked:
            self.say(f"[{tool.id}] cannot update: {blocked}")
            self.actions.record("update", tool.id, "error", "externally-managed Python")
            return False
        if not self.confirm(f"[{tool.id}] update {st.version} -> {st.candidate}?"):
            self.actions.record("update", tool.id, "cancelled", "")
            return False
        self._refresh_index(backend)
        rc = self._run(backend, backend.upgrade_cmd(pkg))
        if rc != 0:
            self.say(f"[{tool.id}] update FAILED (exit code {rc})")
            self.actions.record("update", tool.id, "error", self._failure(rc))
            return False
        if self.dry_run:
            self.actions.record("update", tool.id, "dry-run", f"{st.version} -> {st.candidate}")
            return True
        new = self.status(tool)
        if not new.installed or not new.version:
            self.say(f"[{tool.id}] update ran but the new version could not be read; check manually")
            self.actions.record("update", tool.id, "warning", "could not verify new version")
            return True
        if compare(new.version, st.candidate) < 0:
            self.say(f"[{tool.id}] update ran but the version is still {new.version} "
                     f"(expected {st.candidate}); open a new terminal or check your PATH")
            self.actions.record("update", tool.id, "error", f"still {new.version}, expected {st.candidate}")
            return False
        self.say(f"[{tool.id}] updated {st.version} -> {new.version}")
        self.actions.record("update", tool.id, "success", f"{st.version} -> {new.version}")
        return True
