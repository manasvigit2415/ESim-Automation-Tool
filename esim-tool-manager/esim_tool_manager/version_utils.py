"""Version parsing and comparison.

Handles the messy version strings produced by package managers and tools, e.g.
Debian "1:8.0.4+dfsg-1", Debian pre-release "9.0.0~rc1", Homebrew "42",
Chocolatey "8.0.4" or Verilator "5.020".

Pre-releases (`~`, rc, alpha, beta, dev, pre) sort *before* the matching final
release, so "9.0.0~rc1" is correctly older than "9.0.0" and does not satisfy
a ">=9.0" requirement.
"""
from __future__ import annotations

import operator
import re
from typing import Tuple

_EPOCH = re.compile(r"^\d+:")
_LEADING = re.compile(r"\d+(?:\.\d+)*")
_PRERELEASE = re.compile(r"^(?:~|[-_.]?(?:rc|alpha|beta|dev|pre|preview|snapshot)\d*)", re.I)
_SPEC = re.compile(r"^\s*(>=|<=|==|=|>|<)?\s*(.+?)\s*$")
_OPS = {">=": operator.ge, "<=": operator.le, ">": operator.gt,
        "<": operator.lt, "==": operator.eq, "=": operator.eq}


def normalize(version: str) -> str:
    """Strip epoch and distro suffixes: '1:8.0.4+dfsg-1' -> '8.0.4'."""
    if not version:
        return ""
    version = _EPOCH.sub("", version.strip())
    m = _LEADING.match(version)
    return m.group(0) if m else ""


def parse(version: str) -> Tuple[int, ...]:
    n = normalize(version)
    return tuple(int(p) for p in n.split(".")) if n else ()


def is_prerelease(version: str) -> bool:
    """True for '9.0.0~rc1', '9.0.0-beta2', '9.0.0rc1' (but not '42+ds-1')."""
    version = _EPOCH.sub("", (version or "").strip())
    m = _LEADING.match(version)
    return bool(m and _PRERELEASE.match(version[m.end():]))


def compare(a: str, b: str) -> int:
    """Return -1, 0 or 1. Trailing zeros are ignored ('5.20' == '5.20.0') and a
    pre-release is older than the same numbers without the suffix."""
    pa, pb = list(parse(a)), list(parse(b))
    size = max(len(pa), len(pb))
    pa += [0] * (size - len(pa))
    pb += [0] * (size - len(pb))
    if pa != pb:
        return (pa > pb) - (pa < pb)
    pre_a, pre_b = is_prerelease(a), is_prerelease(b)
    return (pre_b > pre_a) - (pre_a > pre_b)   # final > pre-release


def satisfies(version: str, spec: str) -> bool:
    """True if `version` meets a spec such as '>=36', '<9', '==42' or just '42'."""
    m = _SPEC.match(spec or "")
    if not m or not parse(version):
        return False
    op = _OPS[m.group(1) or ">="]
    return op(compare(version, m.group(2)), 0)
