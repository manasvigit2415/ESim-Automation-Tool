"""Logging: a rotating debug log plus a structured, user-facing action history."""
from __future__ import annotations

import json
import logging
import os
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Dict, List


def home_dir() -> Path:
    """State directory (override with the ESIM_TM_HOME environment variable)."""
    d = Path(os.environ.get("ESIM_TM_HOME") or Path.home() / ".esim_tool_manager")
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_logger(verbose: bool = False) -> logging.Logger:
    log = logging.getLogger("esim_tm")
    if log.handlers:
        return log
    log.setLevel(logging.DEBUG)
    fh = RotatingFileHandler(home_dir() / "tool_manager.log", maxBytes=512_000,
                             backupCount=3, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    log.addHandler(fh)
    if verbose:
        sh = logging.StreamHandler()
        sh.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
        log.addHandler(sh)
    return log


class ActionLog:
    """Append-only JSON-lines history of installs, updates and errors."""

    def __init__(self) -> None:
        self.path = home_dir() / "actions.jsonl"

    def record(self, action: str, tool: str, status: str, detail: str = "") -> None:
        entry = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "action": action,
                 "tool": tool, "status": status, "detail": detail}
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")

    def tail(self, n: int = 20, problems_only: bool = False) -> List[Dict[str, str]]:
        """Newest n entries; with problems_only, just errors and warnings."""
        if not self.path.exists():
            return []
        with open(self.path, encoding="utf-8") as fh:
            lines = fh.readlines()
        out = []
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                continue                      # skip a corrupt line, keep the rest
            if not problems_only or entry.get("status") in ("error", "warning"):
                out.append(entry)
        return out[-n:]
