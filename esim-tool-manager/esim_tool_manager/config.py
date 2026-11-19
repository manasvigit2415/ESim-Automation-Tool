"""User settings, persisted as JSON in the state directory."""
from __future__ import annotations

import json
import os
from typing import Any, Dict

from .logger import home_dir

DEFAULTS: Dict[str, Any] = {
    "preferred_backend": None,   # apt | dnf | pacman | brew | choco | None (auto)
    "pinned_versions": {},       # {"ngspice": "42"}: installs use it, updates skip it
    "extra_paths": [],           # extra directories searched for tool binaries
    "extra_env": {},             # extra variables exported by `config apply`
}
SETTABLE = {"preferred_backend", "extra_paths"}


class Config:
    def __init__(self, data: Dict[str, Any] | None = None, path=None) -> None:
        self.path = path or home_dir() / "config.json"
        self.data = {**DEFAULTS, **(data or {})}

    @classmethod
    def load(cls) -> "Config":
        path = home_dir() / "config.json"
        data = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                pass  # corrupt file: fall back to defaults
        return cls(data, path)

    def save(self) -> None:
        self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")

    def get(self, key: str) -> Any:
        return self.data.get(key, DEFAULTS.get(key))

    def set(self, key: str, value: str) -> None:
        if key not in SETTABLE:
            raise KeyError(f"unknown setting '{key}' (allowed: {', '.join(sorted(SETTABLE))})")
        if key == "extra_paths":
            self.data[key] = [p for p in value.split(os.pathsep) if p]
        else:
            self.data[key] = None if value.lower() in ("", "auto", "none") else value
        self.save()

    def pin(self, tool: str, version: str | None) -> None:
        pins = dict(self.data.get("pinned_versions") or {})
        if version:
            pins[tool] = version
        else:
            pins.pop(tool, None)
        self.data["pinned_versions"] = pins
        self.save()
