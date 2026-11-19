"""Tests for detection accuracy, version logic, Winget, verification, logging and the GUI helpers."""
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

os.environ["ESIM_TM_HOME"] = tempfile.mkdtemp()

import esim_tool_manager.backends as be  # noqa: E402
import esim_tool_manager.manager as mm  # noqa: E402
from esim_tool_manager import cli  # noqa: E402
from esim_tool_manager.config import Config  # noqa: E402
from esim_tool_manager.logger import ActionLog  # noqa: E402
from esim_tool_manager.manager import ToolManager, ToolStatus  # noqa: E402
from esim_tool_manager.registry import Tool, load_registry  # noqa: E402
from esim_tool_manager.version_utils import compare, is_prerelease, satisfies  # noqa: E402

REG = load_registry()


class VersionOutputTests(unittest.TestCase):
    """The version regexes against output formats seen in the wild."""

    def extract(self, tool_id, text):
        m = re.search(REG[tool_id].version_regex, text)
        return m.group(1) if m else None

    def test_ngspice_formats(self):
        self.assertEqual(self.extract("ngspice", "ngspice-42 : Circuit level simulation program\nThe U. C. Berkeley"), "42")
        self.assertEqual(self.extract("ngspice", "******\n** ngspice-36 : Circuit level simulation program\n"), "36")
        self.assertEqual(self.extract("ngspice", "ngspice 43.1 compiled"), "43.1")
        self.assertIsNone(self.extract("ngspice", "command not found"))

    def test_kicad_formats(self):
        self.assertEqual(self.extract("kicad", "7.0.11\n"), "7.0.11")
        self.assertEqual(self.extract("kicad", "8.0.4-1\n"), "8.0.4")
        self.assertEqual(self.extract("kicad", "9.0.0-rc2"), "9.0.0")

    def test_other_tools(self):
        self.assertEqual(self.extract("ghdl", "GHDL 4.1.0 (Ubuntu 4.1.0+dfsg) [Dunoon edition]"), "4.1.0")
        self.assertEqual(self.extract("verilator", "Verilator 5.020 2024-01-01 rev v5.020"), "5.020")

    def test_registry_has_detection_fallbacks(self):
        ng = REG["ngspice"]
        self.assertGreaterEqual(len(ng.probes()), 3)
        self.assertTrue(ng.search_paths["windows"] and ng.search_paths["darwin"])
        self.assertTrue(REG["kicad"].search_paths["windows"])


class VersionLogicTests(unittest.TestCase):
    def test_prerelease_sorts_before_final(self):
        self.assertEqual(compare("9.0.0~rc1", "9.0.0"), -1)
        self.assertEqual(compare("9.0.0", "9.0.0~rc1"), 1)
        self.assertEqual(compare("9.0.0-beta2", "9.0.0"), -1)
        self.assertTrue(is_prerelease("9.0.0~rc1"))
        self.assertFalse(is_prerelease("42+ds-3build1"))
        self.assertFalse(is_prerelease("8.0.4-1"))

    def test_prerelease_does_not_meet_requirement(self):
        self.assertFalse(satisfies("9.0.0~rc1", ">=9.0"))
        self.assertTrue(satisfies("9.0.1~rc1", ">=9.0"))

    def test_distro_suffixes_still_equal(self):
        self.assertEqual(compare("1:8.0.4+dfsg-1", "8.0.4"), 0)


class WingetTests(unittest.TestCase):
    LIST = ("Name   Id           Version Available Source\n"
            "-------------------------------------------\n"
            "KiCad  KiCad.KiCad  8.0.4   9.0.0     winget\n")
    VERSIONS = "Found KiCad [KiCad.KiCad]\nVersion\n-------\n9.0.0\n8.0.4\n7.0.11\n"

    def test_parsing(self):
        w = be.WingetBackend
        self.assertEqual(w.parse_installed(self.LIST, "KiCad.KiCad"), "8.0.4")
        self.assertIsNone(w.parse_installed("No installed package found", "KiCad.KiCad"))
        self.assertEqual(w.parse_versions(self.VERSIONS), ["9.0.0", "8.0.4", "7.0.11"])

    def test_backend_methods(self):
        w = be.WingetBackend()
        with mock.patch.object(be, "capture", lambda *a, **k: (0, self.LIST)):
            self.assertEqual(w.installed_version("KiCad.KiCad"), "8.0.4")
        with mock.patch.object(be, "capture", lambda *a, **k: (0, "Found KiCad\nVersion: 9.0.0\nPublisher: x")):
            self.assertEqual(w.candidate_version("KiCad.KiCad"), "9.0.0")
        with mock.patch.object(be, "capture", lambda *a, **k: (0, self.VERSIONS)):
            self.assertEqual(w.resolve_version("KiCad.KiCad", "8.0.4"), "8.0.4")
            self.assertIsNone(w.resolve_version("KiCad.KiCad", "1.2"))
        cmd = w.install_cmd("KiCad.KiCad", "8.0.4")
        self.assertEqual(cmd[:3], ["winget", "install", "--id"])
        self.assertEqual(cmd[-2:], ["--version", "8.0.4"])
        self.assertIn("--accept-package-agreements", w.upgrade_cmd("KiCad.KiCad"))

    def test_windows_detects_both_managers(self):
        with mock.patch.object(be.pu, "os_name", return_value="windows"), \
             mock.patch.object(be.shutil, "which", lambda exe: "C:/x/" + exe):
            self.assertEqual([b.name for b in be.detect_backends()], ["winget", "choco"])


class BackendFallbackTests(unittest.TestCase):
    def test_tool_without_package_uses_next_backend(self):
        winget, choco = be.WingetBackend(), be.ChocoBackend()
        mgr = ToolManager(config=Config({}, path=None), backends=[winget, choco], out=lambda m: None)
        self.assertIs(mgr.backend_for(REG["kicad"]), winget)     # both have kicad: first wins
        self.assertIs(mgr.backend_for(REG["ghdl"]), choco)       # winget has no ghdl package
        mgr.config.data["preferred_backend"] = "choco"
        self.assertIs(mgr.backend_for(REG["kicad"]), choco)


class CommandRunnerTests(unittest.TestCase):
    def setUp(self):
        self.lines = []
        self.mgr = ToolManager(config=Config({}, path=None), backends=[], out=self.lines.append)
        self.backend = be.PipBackend()

    def test_streams_output_and_returns_rc(self):
        rc = self.mgr._run(self.backend, [sys.executable, "-c", "print('hello'); print('world')"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.lines, ["hello", "world"])

    def test_failure_detail_picks_the_error_line(self):
        code = "import sys; print('Reading state...'); print('E: Unable to locate package foo'); print('done'); sys.exit(100)"
        rc = self.mgr._run(self.backend, [sys.executable, "-c", code])
        self.assertEqual(rc, 100)
        self.assertEqual(self.mgr._failure(rc), "exit code 100: E: Unable to locate package foo")

    def test_missing_executable(self):
        self.assertEqual(self.mgr._run(self.backend, ["definitely-not-a-real-binary-xyz"]), 127)


class Fake(be.Backend):
    name, supports_pin = "fake", True

    def __init__(self):
        self.version, self.cand = None, "2.0"

    def available(self): return True
    def installed_version(self, pkg): return self.version
    def candidate_version(self, pkg): return self.cand
    def list_versions(self, pkg): return ["1.0", "2.0"]
    def install_cmd(self, pkg, version=None): return ["i", version or "latest"]
    def upgrade_cmd(self, pkg): return ["u"]


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.fake = Fake()
        tool = Tool(id="foo", name="Foo", binary="foo", min_version="1.0", packages={"fake": "foo"})
        self.mgr = ToolManager(config=Config({}, path=None), registry={"foo": tool},
                               backends=[self.fake], assume_yes=True, out=lambda m: None)
        self.reported = None          # what probe() will "see" after commands run
        self.mgr.probe = lambda t: (self.reported is not None, "/x/foo", self.reported)
        self.mgr._run = lambda b, cmd: 0

    def test_pinned_install_with_wrong_result_is_rejected(self):
        self.reported = "2.0"          # package manager "installed" 2.0 although 1.0 was requested
        self.assertFalse(self.mgr.install("foo", version="1.0"))

    def test_install_matching_pin_passes(self):
        self.reported = "1.0"
        self.assertTrue(self.mgr.install("foo", version="1.0"))

    def test_update_that_changes_nothing_fails_verification(self):
        self.reported, self.fake.version = "1.0", "1.0"
        self.assertFalse(self.mgr.update("foo"))                      # still 1.0 after "upgrade"

    def test_update_that_works_passes(self):
        self.reported, self.fake.version = "1.0", "1.0"
        self.mgr._run = lambda b, cmd: setattr(self, "reported", "2.0") or 0
        self.assertTrue(self.mgr.update("foo"))
        statuses = [e["status"] for e in ActionLog().tail(5)]
        self.assertEqual(statuses[-1], "success")

    def test_update_failure_is_recorded_with_detail(self):
        self.reported, self.fake.version = "1.0", "1.0"
        self.mgr._run = lambda b, cmd: 1
        self.assertFalse(self.mgr.update("foo"))
        self.assertEqual(ActionLog().tail(1)[0]["status"], "error")


class HistoryTests(unittest.TestCase):
    def test_problem_filter_and_corrupt_line_tolerance(self):
        log = ActionLog()
        log.record("install", "a", "success")
        log.record("install", "b", "error", "boom")
        log.record("update", "c", "warning", "meh")
        with open(log.path, "a", encoding="utf-8") as fh:
            fh.write("{not json\n")
        problems = [e["tool"] for e in log.tail(50, problems_only=True)]
        self.assertEqual(problems[-2:], ["b", "c"])
        self.assertTrue(all(e["status"] in ("error", "warning") for e in log.tail(50, True)))


class CliCommandTests(unittest.TestCase):
    def test_new_commands_parse(self):
        p = cli.build_parser()
        self.assertTrue(p.parse_args(["log", "--errors", "--json"]).errors)
        self.assertEqual(p.parse_args(["setup", "--all", "-y"]).command, "setup")
        self.assertEqual(p.parse_args(["verify"]).command, "verify")
        self.assertEqual(p.parse_args(["gui"]).command, "gui")

    def test_verify_exit_codes(self):
        mgr = ToolManager(config=Config({}, path=None), backends=[], out=lambda m: None)
        tool = Tool(id="foo", name="Foo", binary="foo", min_version="2.0")
        mgr.registry = {"foo": tool}
        args = mock.Mock(tools=[], all=False)
        for installed, version, expected in [(False, None, 1), (True, "1.0", 1), (True, "2.1", 0)]:
            mgr.probe = lambda t, i=installed, v=version: (i, "/x/foo" if i else None, v)
            with mock.patch("builtins.print"):
                self.assertEqual(cli.cmd_verify(mgr, args), expected)


class GuiTests(unittest.TestCase):
    def test_status_row_states(self):
        try:
            from esim_tool_manager.gui import status_row
        except ImportError:
            self.skipTest("tkinter not installed")
        t = Tool(id="x", name="X", binary="x", min_version="2.0")
        self.assertEqual(status_row(ToolStatus(t, False))[1], "missing")
        self.assertEqual(status_row(ToolStatus(t, True, "1.0"))[1], "old")
        self.assertEqual(status_row(ToolStatus(t, True, "2.0", candidate="3.0"))[1], "update")
        self.assertEqual(status_row(ToolStatus(t, True, "2.0", candidate="2.0"))[1], "ok")

    def test_window_builds_and_fills_table(self):
        try:
            import tkinter as tk
            from esim_tool_manager.gui import App
        except ImportError:
            self.skipTest("tkinter not installed")
        try:
            root = tk.Tk()
        except tk.TclError:
            self.skipTest("no display available (run under xvfb-run)")
        try:
            mgr = ToolManager(config=Config({}, path=None), backends=[], out=lambda m: None)
            mgr.probe = lambda t: (False, None, None)
            app = App(root, mgr)
            deadline = 50
            while app.busy and deadline:
                root.update(); root.after(20); deadline -= 1
            for _ in range(30):
                root.update()
            self.assertEqual(len(app.tree.get_children()), len(mgr.registry))
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
