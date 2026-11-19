"""Tool registry: a declarative description of every managed tool.

Tools live in data/tools.json. Users can add or override entries by creating
~/.esim_tool_manager/tools.json (same schema), so no code change is needed to
support a new tool.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .logger import home_dir

BUNDLED = Path(__file__).parent / "data" / "tools.json"


@dataclass
class Probe:
    """One way of detecting a tool: an executable plus how to ask its version."""
    binary: str
    version_cmd: List[str]
    search_paths: Dict[str, List[str]] = field(default_factory=dict)


@dataclass
class Tool:
    id: str
    name: str
    kind: str = "system"                 # "system" (binary on PATH) or "pip"
    description: str = ""
    homepage: str = ""
    required: bool = True
    binary: str = ""                     # executable name, for system tools
    version_cmd: List[str] = field(default_factory=list)
    version_regex: str = r"(\d+(?:\.\d+)+)"
    min_version: Optional[str] = None    # minimum version eSim works with
    recommended_version: Optional[str] = None
    packages: Dict[str, str] = field(default_factory=dict)  # backend -> package id
    requires: List[str] = field(default_factory=list)       # other tool ids
    search_paths: Dict[str, List[str]] = field(default_factory=dict)  # os -> globs
    # Alternative executables tried when the primary one is absent, each a dict
    # {"binary", "version_cmd", "search_paths"}. Needed because e.g. `kicad-cli`
    # only exists from KiCad 7, while older KiCad ships only `kicad`.
    fallback_probes: List[dict] = field(default_factory=list)
    pip_dist: str = ""

    def probes(self) -> List[Probe]:
        """Primary probe first, then fallbacks."""
        out = [Probe(self.binary, self.version_cmd, self.search_paths)]
        for fb in self.fallback_probes:
            out.append(Probe(fb["binary"], fb.get("version_cmd") or [fb["binary"], "--version"],
                             fb.get("search_paths", {})))
        return out

    @classmethod
    def from_dict(cls, tool_id: str, d: dict) -> "Tool":
        t = cls(id=tool_id, name=d.get("name", tool_id), **{
            k: v for k, v in d.items() if k in cls.__dataclass_fields__ and k not in ("id", "name")})
        if t.kind == "pip" and not t.pip_dist:
            t.pip_dist = t.packages.get("pip", tool_id)
        if t.kind == "system" and not t.version_cmd:
            t.version_cmd = [t.binary, "--version"]
        return t


def load_registry(extra: Optional[Path] = None) -> Dict[str, Tool]:
    reg: Dict[str, Tool] = {}
    sources = [BUNDLED, extra or home_dir() / "tools.json"]
    for src in sources:
        if not src.exists():
            continue
        for tool_id, d in json.loads(src.read_text(encoding="utf-8")).items():
            reg[tool_id] = Tool.from_dict(tool_id, d)
    return reg
