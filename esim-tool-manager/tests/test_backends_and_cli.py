"""Backend output parsing (mocked), PEP 668 handling, KiCad fallback probe, CLI fixes.

The dnf/pacman/brew/choco backends cannot be exercised for real on one machine,
so these tests feed each one realistic command output and check the parsing and
the commands it builds.
"""
import json
import os
import tempfile
import unittest
from unittest import mock

os.environ["ESIM_TM_HOME"] = tempfile.mkdtemp()

import esim_tool_manager.backends as be  # noqa: E402
from esim_tool_manager import cli  # noqa: E402
from esim_tool_manager.config import Config  # noqa: E402
from esim_tool_manager.manager import ToolManager  # noqa: E402
from esim_tool_manager.registry import load_registry  # noqa: E402


def fake_capture(output, rc=0):
    return mock.patch.object(be, "capture", lambda *a, **k: (rc, output))


class PackageManagerParsingTests(unittest.TestCase):
    def test_dnf(self):
        with fake_capture("Name         : ngspice\nVersion      : 42\nRelease      : 1.fc40\n"):
            self.assertEqual(be.DnfBackend().candidate_version("ngspice"), "42")
        with fake_capture("42"):
            self.assertEqual(be.DnfBackend().installed_version("ngspice"), "42")
        with fake_capture("", rc=1):
            self.assertIsNone(be.DnfBackend().installed_version("ngspice"))

    def test_pacman(self):
        with fake_capture("ngspice 42-1\n"):
            self.assertEqual(be.PacmanBackend().installed_version("ngspice"), "42-1")
        with fake_capture("Name : ngspice\nVersion : 43-1\n"):
            self.assertEqual(be.PacmanBackend().candidate_version("ngspice"), "43-1")

    def test_brew_formula_and_cask(self):
        brew = be.BrewBackend()
        formula = json.dumps({"formulae": [{"versions": {"stable": "42"}}]})
        cask = json.dumps({"casks": [{"version": "8.0.4"}]})
        with fake_capture(formula):
            self.assertEqual(brew.candidate_version("ngspice"), "42")
        with fake_capture(cask):
            self.assertEqual(brew.candidate_version("cask:kicad"), "8.0.4")
        with fake_capture("not json"):
            self.assertIsNone(brew.candidate_version("ngspice"))
        self.assertEqual(brew.install_cmd("cask:kicad"), ["brew", "install", "--cask", "kicad"])
        self.assertEqual(brew.upgrade_cmd("ngspice"), ["brew", "upgrade", "ngspice"])
        with fake_capture("kicad 8.0.4\n"):
            self.assertEqual(brew.installed_version("cask:kicad"), "8.0.4")

    def test_choco(self):
        choco = be.ChocoBackend()
        with fake_capture("kicad|8.0.4\n"):
            self.assertEqual(choco.installed_version("kicad"), "8.0.4")
            self.assertEqual(choco.candidate_version("kicad"), "8.0.4")
        with fake_capture("kicad|8.0.4\nkicad|7.0.11\n"):
            self.assertEqual(choco.resolve_version("kicad", "7.0.11"), "7.0.11")
            self.assertIsNone(choco.resolve_version("kicad", "9.9"))
        self.assertEqual(choco.install_cmd("kicad", "7.0.11"),
                         ["choco", "install", "kicad", "-y", "--version=7.0.11"])

    def test_apt_pin_resolves_distro_suffix(self):
        madison = " ngspice | 42+ds-3 | http://archive.ubuntu.com noble/universe amd64 Packages\n"
        with fake_capture(madison):
            self.assertEqual(be.AptBackend().resolve_version("ngspice", "42"), "42+ds-3")
            self.assertIsNone(be.AptBackend().resolve_version("ngspice", "99"))
        self.assertEqual(be.AptBackend().install_cmd("ngspice", "42+ds-3"),
                         ["apt-get", "install", "-y", "ngspice=42+ds-3"])


class PipPep668Tests(unittest.TestCase):
    def test_blocked_without_override(self):
        with mock.patch.object(be.pu, "externally_managed", return_value=True):
            self.assertIn("externally managed", be.PipBackend().blocked_reason())
            self.assertIsNone(be.PipBackend(allow_system=True).blocked_reason())

    def test_not_blocked_in_venv_or_normal_python(self):
        with mock.patch.object(be.pu, "externally_managed", return_value=False):
            self.assertIsNone(be.PipBackend().blocked_reason())

    def test_override_adds_flag_only_when_needed(self):
        with mock.patch.object(be.pu, "in_virtualenv", return_value=False), \
             mock.patch.object(be.pu, "externally_managed", return_value=True):
            self.assertIn("--break-system-packages", be.PipBackend(True).install_cmd("numpy"))
            self.assertNotIn("--break-system-packages", be.PipBackend(False).install_cmd("numpy"))
        with mock.patch.object(be.pu, "in_virtualenv", return_value=True):
            cmd = be.PipBackend(True).install_cmd("numpy")
            self.assertNotIn("--user", cmd)
            self.assertNotIn("--break-system-packages", cmd)

    def test_manager_refuses_cleanly(self):
        msgs = []
        mgr = ToolManager(config=Config({}, path=None), backends=[], assume_yes=True, out=msgs.append)
        with mock.patch.object(be.pu, "externally_managed", return_value=True):
            self.assertFalse(mgr.install("numpy"))
        self.assertTrue(any("externally managed" in m for m in msgs))


class KicadProbeTests(unittest.TestCase):
    def setUp(self):
        self.mgr = ToolManager(config=Config({}, path=None), backends=[], out=lambda m: None)
        self.kicad = load_registry()["kicad"]

    def _probe(self, present, outputs):
        """present: binaries that exist; outputs: binary -> text printed by the version command."""
        self.mgr.find_binary = lambda pr: f"/x/{pr.binary}" if pr.binary in present else None
        import esim_tool_manager.manager as mm
        with mock.patch.object(mm, "capture", lambda cmd, **k: (0, outputs.get(os.path.basename(cmd[0]), ""))):
            return self.mgr.probe(self.kicad)

    def test_kicad7_uses_cli(self):
        self.assertEqual(self._probe({"kicad-cli", "kicad"}, {"kicad-cli": "7.0.11\n"}),
                         (True, "/x/kicad-cli", "7.0.11"))

    def test_kicad6_is_not_reported_missing(self):
        installed, path, version = self._probe({"kicad"}, {"kicad": "6.0.11"})
        self.assertTrue(installed)
        self.assertEqual((path, version), ("/x/kicad", "6.0.11"))

    def test_kicad6_headless_unknown_version_still_installed(self):
        installed, _, version = self._probe({"kicad"}, {"kicad": "Error: Unable to initialize GTK+"})
        self.assertTrue(installed)
        self.assertIsNone(version)

    def test_really_missing(self):
        self.assertEqual(self._probe(set(), {}), (False, None, None))


class CliTests(unittest.TestCase):
    def test_update_hint_is_a_valid_command(self):
        """check-updates used to suggest `update --all`, which did not exist."""
        args = cli.build_parser().parse_args(["update"])
        self.assertEqual(args.command, "update")
        with self.assertRaises(SystemExit):
            with mock.patch("sys.stderr"):
                cli.build_parser().parse_args(["update", "--all"])

    def test_global_flags_before_or_after_subcommand(self):
        p = cli.build_parser()
        self.assertTrue(p.parse_args(["-y", "install", "ngspice"]).yes)
        self.assertTrue(p.parse_args(["install", "ngspice", "-y"]).yes)
        self.assertTrue(p.parse_args(["install", "numpy", "--break-system-packages"]).break_system_packages)
        self.assertFalse(p.parse_args(["list"]).dry_run)

    def test_version_requires_single_tool(self):
        self.assertEqual(cli.main(["install", "ngspice", "kicad", "--version", "1"]), 2)

    def test_table_clips_long_cells(self):
        out = cli.table(["A", "B"], [["x" * 200, "y"]], max_width=20)
        self.assertTrue(all(len(line) < 40 for line in out.splitlines()))
        self.assertIn("...", out)


if __name__ == "__main__":
    unittest.main()
