"""Graphical interface (tkinter, standard library: no extra dependency).

The GUI is a thin layer over ToolManager. Every operation runs in a background
thread and talks to the window only through a queue, so the window never
freezes during a long install and no widget is touched from a worker thread.
"""
from __future__ import annotations

import queue
import shutil
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Callable, List, Optional, Tuple

from . import platform_utils as pu
from .deps import run_doctor
from .envsetup import apply_environment
from .logger import ActionLog
from .manager import ToolManager, ToolStatus

COLUMNS = ("tool", "kind", "state", "version", "min", "available", "managed")
HEADINGS = ("Tool", "Kind", "State", "Version", "Min", "Available", "Managed by")
WIDTHS = (150, 70, 90, 110, 70, 130, 100)
TAG_COLOURS = {"ok": "#1a7f37", "update": "#9a6700", "old": "#bc4c00", "missing": "#cf222e"}


def status_row(st: ToolStatus) -> Tuple[tuple, str]:
    """Table values and colour tag for one tool (pure function, unit-tested)."""
    if not st.installed:
        state, tag = "MISSING", "missing"
    elif not st.meets_min:
        state, tag = "OLD", "old"
    elif st.update_available:
        state, tag = "UPDATE", "update"
    else:
        state, tag = "OK", "ok"
    t = st.tool
    values = (t.name, t.kind, state, st.version or ("?" if st.installed else "-"),
              t.min_version or "-", st.candidate or "-", st.managed_by or "-")
    return values, tag


class App:
    def __init__(self, root: tk.Tk, manager: Optional[ToolManager] = None) -> None:
        self.root = root
        self.q: "queue.Queue[tuple]" = queue.Queue()
        self.busy = False
        self.with_candidates = False   # once 'Check updates' is used, keep showing Available
        self.mgr = manager or ToolManager(assume_yes=True)
        self.mgr._out = lambda msg: self.q.put(("log", msg))
        self.mgr.assume_yes = True          # the GUI asks with dialogs instead
        if pu.os_name() == "linux" and not pu.is_admin() and shutil.which("pkexec"):
            self.mgr.elevate = ["pkexec"]   # graphical password prompt instead of sudo
        self._build()
        self.refresh()
        self.root.after(100, self._poll)

    # ------------------------------------------------------------ layout
    def _build(self) -> None:
        r = self.root
        r.title("eSim Tool Manager")
        r.geometry("1080x680")
        r.minsize(900, 520)
        ttk.Style().theme_use("clam" if "clam" in ttk.Style().theme_names() else "default")

        bar = ttk.Frame(r, padding=(8, 8, 8, 4))
        bar.pack(fill="x")
        self.buttons: List[ttk.Button] = []
        for label, fn in [("Refresh", self.refresh), ("Check updates", lambda: self.refresh(True)),
                          ("Install selected", self.install_selected),
                          ("Update selected", self.update_selected),
                          ("Install required", self.install_required),
                          ("Doctor", self.doctor), ("Verify", self.verify),
                          ("Write env", self.apply_env)]:
            b = ttk.Button(bar, text=label, command=fn)
            b.pack(side="left", padx=2)
            self.buttons.append(b)

        mid = ttk.Frame(r, padding=(8, 0))
        mid.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(mid, columns=COLUMNS, show="headings", height=10, selectmode="extended")
        for col, head, w in zip(COLUMNS, HEADINGS, WIDTHS):
            self.tree.heading(col, text=head)
            self.tree.column(col, width=w, anchor="w")
        for tag, colour in TAG_COLOURS.items():
            self.tree.tag_configure(tag, foreground=colour)
        scroll = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        tabs = ttk.Notebook(r)
        tabs.pack(fill="both", expand=True, padx=8, pady=6)
        out = ttk.Frame(tabs)
        tabs.add(out, text="Output")
        self.output = tk.Text(out, height=10, wrap="word", state="disabled", font=("TkFixedFont", 9))
        osb = ttk.Scrollbar(out, orient="vertical", command=self.output.yview)
        self.output.configure(yscrollcommand=osb.set)
        self.output.pack(side="left", fill="both", expand=True)
        osb.pack(side="right", fill="y")

        hist = ttk.Frame(tabs)
        tabs.add(hist, text="History")
        cols = ("time", "action", "tool", "status", "detail")
        self.history = ttk.Treeview(hist, columns=cols, show="headings", height=8)
        for col, w in zip(cols, (140, 110, 120, 80, 460)):
            self.history.heading(col, text=col.title())
            self.history.column(col, width=w, anchor="w")
        for tag, colour in (("error", "#cf222e"), ("warning", "#9a6700"), ("success", "#1a7f37")):
            self.history.tag_configure(tag, foreground=colour)
        self.history.pack(fill="both", expand=True)

        info = pu.summary()
        backends = ", ".join(b.name for b in self.mgr.backends) or "none"
        self.status = tk.StringVar(value=f"{info['distro']}  |  package managers: {backends}  |  "
                                         f"administrator: {info['admin']}")
        ttk.Label(r, textvariable=self.status, anchor="w", padding=(8, 2)).pack(fill="x")

    # ------------------------------------------------------------ plumbing
    def log(self, text: str) -> None:
        self.output.configure(state="normal")
        self.output.insert("end", text + "\n")
        self.output.see("end")
        self.output.configure(state="disabled")

    def _set_busy(self, busy: bool, label: str = "") -> None:
        self.busy = busy
        for b in self.buttons:
            b.state(["disabled"] if busy else ["!disabled"])
        self.root.config(cursor="watch" if busy else "")
        if label:
            self.status.set(label)

    def run_async(self, label: str, work: Callable[[], None], then_refresh: bool = False) -> None:
        if self.busy:
            return
        self._set_busy(True, f"{label} ...")
        self.log(f"--- {label} ---")

        def worker() -> None:
            try:
                work()
                if then_refresh:
                    self._collect(self.with_candidates)
            except Exception as exc:                      # surface, never swallow
                self.q.put(("log", f"ERROR: {exc}"))
            finally:
                self.q.put(("done", label))
        threading.Thread(target=worker, daemon=True).start()

    def _collect(self, with_candidates: bool) -> None:
        rows = []
        for tool in self.mgr.registry.values():
            values, tag = status_row(self.mgr.status(tool, with_candidate=with_candidates))
            rows.append((tool.id, values, tag))
        self.q.put(("rows", rows))

    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self.log(payload)
                elif kind == "rows":
                    self._fill_table(payload)
                elif kind == "done":
                    self._set_busy(False)
                    self.status.set(f"{payload}: finished")
                    self._fill_history()
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _fill_table(self, rows) -> None:
        selected = set(self.tree.selection())
        self.tree.delete(*self.tree.get_children())
        for tool_id, values, tag in rows:
            self.tree.insert("", "end", iid=tool_id, values=values, tags=(tag,))
        self.tree.selection_set([i for i in selected if self.tree.exists(i)])

    def _fill_history(self) -> None:
        self.history.delete(*self.history.get_children())
        for e in reversed(ActionLog().tail(200)):
            self.history.insert("", "end", tags=(e["status"],),
                                values=(e["time"], e["action"], e["tool"], e["status"], e["detail"]))

    def selected_ids(self) -> List[str]:
        ids = list(self.tree.selection())
        if not ids:
            messagebox.showinfo("eSim Tool Manager", "Select one or more tools in the table first.")
        return ids

    def _needs_elevation_warning(self) -> bool:
        """System package installs need root (Linux) or an elevated terminal (Windows)."""
        if pu.is_admin():
            return False
        if pu.os_name() == "linux" and self.mgr.elevate[0] == "pkexec":
            return False
        if pu.os_name() == "windows":
            return not messagebox.askokcancel(
                "Administrator recommended",
                "Chocolatey and some Winget installs need an elevated terminal.\n"
                "Continue anyway?")
        return not messagebox.askokcancel(
            "Administrator needed",
            "System installs need root. Without a graphical password prompt (pkexec) this may fail.\n"
            "Tip: run  sudo python3 -m esim_tool_manager gui\nContinue anyway?")

    # ------------------------------------------------------------ actions
    def refresh(self, with_candidates: bool = False) -> None:
        self.with_candidates = self.with_candidates or with_candidates
        self.run_async("Checking updates" if with_candidates else "Refreshing",
                       lambda: self._collect(self.with_candidates))

    def install_selected(self) -> None:
        ids = self.selected_ids()
        if not ids or not messagebox.askyesno("Install", f"Install: {', '.join(ids)}?"):
            return
        if self._needs_elevation_warning():
            return
        self.run_async("Installing", lambda: [self.mgr.install(i) for i in ids], then_refresh=True)

    def update_selected(self) -> None:
        ids = self.selected_ids()
        if not ids or not messagebox.askyesno("Update", f"Update: {', '.join(ids)}?"):
            return
        if self._needs_elevation_warning():
            return
        self.run_async("Updating", lambda: [self.mgr.update(i) for i in ids], then_refresh=True)

    def install_required(self) -> None:
        ids = [t.id for t in self.mgr.registry.values() if t.required]
        if not messagebox.askyesno("Install", f"Install all required tools?\n{', '.join(ids)}"):
            return
        if self._needs_elevation_warning():
            return
        self.run_async("Installing required tools", lambda: [self.mgr.install(i) for i in ids],
                       then_refresh=True)

    def doctor(self) -> None:
        def work() -> None:
            lines, _, _ = run_doctor(self.mgr, list(self.mgr.registry))
            for line in lines:
                self.q.put(("log", line))
        self.run_async("Running doctor", work)

    def verify(self) -> None:
        def work() -> None:
            for tool in self.mgr.registry.values():
                st = self.mgr.status(tool)
                verdict = ("MISSING" if not st.installed else "TOO OLD" if not st.meets_min else "PASS")
                self.q.put(("log", f"{verdict:8} {tool.id} {st.version or ''}"))
        self.run_async("Verifying", work)

    def apply_env(self) -> None:
        self.run_async("Writing environment files", lambda: apply_environment(self.mgr))


def launch() -> int:
    root = tk.Tk()
    App(root)
    root.mainloop()
    return 0


def main() -> int:
    return launch()
