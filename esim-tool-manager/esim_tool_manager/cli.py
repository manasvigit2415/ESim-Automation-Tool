"""Command-line interface: `esim-tm <command>` or `python -m esim_tool_manager`.

This module only parses arguments and formats output; all real work is done by
ToolManager, the dependency checker and the environment writer.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional, Sequence

from . import __version__, platform_utils as pu
from .config import Config
from .deps import run_doctor, system_checks
from .envsetup import apply_environment
from .logger import ActionLog, get_logger, home_dir
from .manager import ToolManager

STATE_LABELS = {True: "OK", False: "OLD"}


def table(headers: Sequence[str], rows: List[Sequence[str]], max_width: int = 40) -> str:
    """Render a plain-text table; over-long cells are shortened."""
    def clip(text: object) -> str:
        text = str(text)
        return text if len(text) <= max_width else text[: max_width - 3] + "..."

    cells = [[clip(c) for c in row] for row in rows]
    widths = [max(len(h), *(len(r[i]) for r in cells)) if cells else len(h)
              for i, h in enumerate(headers)]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    lines = [fmt.format(*headers), fmt.format(*("-" * w for w in widths))]
    lines += [fmt.format(*row) for row in cells]
    return "\n".join(lines)


def _targets(mgr: ToolManager, names: List[str], include_optional: bool) -> List[str]:
    """Explicit names win; otherwise required tools (or all with --all)."""
    if names:
        return [mgr.get_tool(n).id for n in names]
    if include_optional:
        return list(mgr.registry)
    return [t.id for t in mgr.registry.values() if t.required]


# ---------------------------------------------------------------- commands
def cmd_list(mgr: ToolManager, args) -> int:
    rows, data = [], {}
    for tool in mgr.registry.values():
        st = mgr.status(tool)
        state = STATE_LABELS[st.meets_min] if st.installed else "MISSING"
        rows.append([tool.id, tool.kind, "yes" if tool.required else "opt", state,
                     st.version or ("?" if st.installed else "-"),
                     tool.min_version or "-", st.managed_by or "-"])
        data[tool.id] = {"installed": st.installed, "version": st.version, "path": st.path,
                         "state": state, "managed_by": st.managed_by}
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        print(table(["TOOL", "KIND", "REQ", "STATE", "VERSION", "MIN", "MANAGED BY"], rows))
    return 0


def cmd_install(mgr: ToolManager, args) -> int:
    version = args.version if len(args.tools) == 1 else None
    if args.version and version is None:
        print("error: --version needs exactly one tool name", file=sys.stderr)
        return 2
    results = [mgr.install(tid, version=version, force=args.force)
               for tid in _targets(mgr, args.tools, args.all)]
    return 0 if all(results) else 1


def cmd_check_updates(mgr: ToolManager, args) -> int:
    results = mgr.check_updates(args.tools or None)
    rows = []
    for st in results:
        if st.update_available:
            status = "UPDATE AVAILABLE"
        else:
            status = "up to date" if st.installed else "not installed"
        rows.append([st.tool.id, st.version or "-", st.candidate or "-", status])
    print(table(["TOOL", "INSTALLED", "AVAILABLE", "STATUS"], rows))
    count = sum(st.update_available for st in results)
    hint = f" Run `{pu.prog()} update`." if count else ""
    print(f"\n{count} update(s) available.{hint}")
    return 0


def cmd_update(mgr: ToolManager, args) -> int:
    if args.tools:
        ids = [mgr.get_tool(t).id for t in args.tools]
    else:
        ids = [st.tool.id for st in mgr.check_updates() if st.update_available]
        if not ids:
            print("Everything is up to date.")
            return 0
    results = [mgr.update(tid) for tid in ids]
    return 0 if all(results) else 1


def cmd_doctor(mgr: ToolManager, args) -> int:
    lines, errors, _ = run_doctor(mgr, _targets(mgr, args.tools, True))
    print("\n".join(lines))
    return 1 if errors else 0


def cmd_config(mgr: ToolManager, args) -> int:
    cfg: Config = mgr.config
    needs = {"set": 2, "pin": 2, "unpin": 1}
    if len(args.args) < needs.get(args.action, 0):
        print(f"error: 'config {args.action}' needs {needs[args.action]} argument(s)", file=sys.stderr)
        return 2
    if args.action == "show":
        print(json.dumps(cfg.data, indent=2))
    elif args.action == "set":
        cfg.set(args.args[0], args.args[1])
        print("saved")
    elif args.action == "pin":
        cfg.pin(mgr.get_tool(args.args[0]).id, args.args[1])
        print(f"pinned {args.args[0]} to {args.args[1]}")
    elif args.action == "unpin":
        cfg.pin(mgr.get_tool(args.args[0]).id, None)
        print(f"unpinned {args.args[0]}")
    elif args.action == "apply":
        apply_environment(mgr, persist=args.persist)
    return 0


def cmd_verify(mgr: ToolManager, args) -> int:
    """Re-probe the real system and confirm every tool works and is new enough."""
    rows, failed = [], 0
    for tid in _targets(mgr, args.tools, args.all):
        tool = mgr.get_tool(tid)
        st = mgr.status(tool)
        if not st.installed:
            result = "FAIL (missing)"
        elif not st.meets_min:
            result = f"FAIL (needs >= {tool.min_version})"
        else:
            result = "PASS" if st.version else "PASS (version unknown)"
        failed += result.startswith("FAIL") and tool.required
        rows.append([tid, result, st.version or "-", st.path or "-"])
    print(table(["TOOL", "RESULT", "VERSION", "PATH"], rows, max_width=60))
    print(f"\n{'All required tools verified.' if not failed else f'{failed} required tool(s) failed verification.'}")
    return 1 if failed else 0


def cmd_setup(mgr: ToolManager, args) -> int:
    """One-shot: check the system, install what is missing, write the environment, verify."""
    print("== 1/4 System check ==")
    blocking = [c for c in system_checks(mgr) if c.level == "error"]
    for c in blocking:
        print(f"[FAIL] {c.name}: {c.message}\n         -> {c.hint}")
    if blocking:
        return 1
    print("ok")
    print("\n== 2/4 Installing tools ==")
    installed_ok = cmd_install(mgr, argparse.Namespace(tools=[], all=args.all, version=None, force=False)) == 0
    print("\n== 3/4 Environment ==")
    apply_environment(mgr)
    print("\n== 4/4 Verification ==")
    verified = cmd_verify(mgr, argparse.Namespace(tools=[], all=args.all)) == 0
    return 0 if (installed_ok and verified) else 1


def cmd_gui(mgr: ToolManager, args) -> int:
    try:
        from .gui import launch
    except ImportError as exc:
        print(f"error: the GUI needs tkinter ({exc}). On Ubuntu/Debian: sudo apt install python3-tk",
              file=sys.stderr)
        return 1
    return launch()


def cmd_log(mgr: ToolManager, args) -> int:
    entries = ActionLog().tail(args.n, problems_only=args.errors)
    if args.json:
        print(json.dumps(entries, indent=2))
        return 0
    if not entries:
        print("No problems recorded." if args.errors else "No actions recorded yet.")
        return 0
    rows = [[e["time"], e["action"], e["tool"], e["status"], e["detail"]] for e in entries]
    print(table(["TIME", "ACTION", "TOOL", "STATUS", "DETAIL"], rows, max_width=80))
    print(f"\nFull debug log: {home_dir() / 'tool_manager.log'}")
    return 0


def cmd_info(mgr: ToolManager, args) -> int:
    for key, value in pu.summary().items():
        print(f"{key:>8}: {value}")
    print(f"{'backends':>8}: {', '.join(b.name for b in mgr.backends) or 'none'}")
    print(f"{'state':>8}: {home_dir()}")
    return 0


# ----------------------------------------------------------------- parser
def _add_global_flags(parser: argparse.ArgumentParser, suppress: bool) -> None:
    """Global flags work before or after the subcommand. On sub-parsers the
    default is suppressed so they never overwrite a value given earlier."""
    default = argparse.SUPPRESS if suppress else False
    parser.add_argument("--dry-run", action="store_true", default=default,
                        help="show the commands without running them")
    parser.add_argument("-y", "--yes", action="store_true", default=default,
                        help="do not ask for confirmation")
    parser.add_argument("-v", "--verbose", action="store_true", default=default,
                        help="print the debug log to the console")
    parser.add_argument("--break-system-packages", action="store_true", default=default,
                        help="allow pip to modify an externally-managed system Python (PEP 668)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="esim-tm", description="Automated Tool Manager for eSim")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    _add_global_flags(parser, suppress=False)
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, fn, help_text: str, aliases: Sequence[str] = ()) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, aliases=list(aliases), help=help_text)
        _add_global_flags(sp, suppress=True)
        sp.set_defaults(fn=fn)
        return sp

    sp = add("list", cmd_list, "show installed tools and versions", aliases=["status"])
    sp.add_argument("--json", action="store_true", help="machine-readable output")

    sp = add("install", cmd_install, "install tools (default: all required tools)")
    sp.add_argument("tools", nargs="*")
    sp.add_argument("--all", action="store_true", help="include optional tools")
    sp.add_argument("--version", help="exact version (single tool only)")
    sp.add_argument("--force", action="store_true", help="reinstall even if present")

    sp = add("check-updates", cmd_check_updates, "compare installed and available versions")
    sp.add_argument("tools", nargs="*")

    sp = add("update", cmd_update, "upgrade tools that have updates (default: all of them)")
    sp.add_argument("tools", nargs="*")

    sp = add("doctor", cmd_doctor, "check system and tool dependencies")
    sp.add_argument("tools", nargs="*")

    sp = add("config", cmd_config, "settings, version pins and PATH/env setup")
    sp.add_argument("action", choices=["show", "set", "pin", "unpin", "apply"])
    sp.add_argument("args", nargs="*")
    sp.add_argument("--persist", action="store_true",
                    help="with apply: source the env file from your shell rc")

    sp = add("verify", cmd_verify, "re-check that tools are installed and new enough")
    sp.add_argument("tools", nargs="*")
    sp.add_argument("--all", action="store_true", help="include optional tools")

    sp = add("setup", cmd_setup, "check, install required tools, write environment, verify")
    sp.add_argument("--all", action="store_true", help="include optional tools")

    add("gui", cmd_gui, "open the graphical interface")

    sp = add("log", cmd_log, "show the history of actions")
    sp.add_argument("-n", type=int, default=20, help="number of entries")
    sp.add_argument("--errors", action="store_true", help="only errors and warnings")
    sp.add_argument("--json", action="store_true", help="machine-readable output")

    add("info", cmd_info, "show platform information")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    get_logger(args.verbose)
    mgr = ToolManager(dry_run=args.dry_run, assume_yes=args.yes,
                      allow_system_pip=args.break_system_packages)
    try:
        return args.fn(mgr, args)
    except KeyError as exc:
        print(f"error: {exc.args[0] if exc.args else exc}", file=sys.stderr)
        return 2
    except BrokenPipeError:      # e.g. `esim-tm list | head`
        return 0
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
