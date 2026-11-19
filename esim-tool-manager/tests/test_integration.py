"""Real-system tests. They install real software, so they only run when asked:

    ESIM_TM_INTEGRATION=1 python -m unittest tests.test_integration -v

CI runs them on Linux, Windows and macOS (see .github/workflows/ci.yml).
Each test uses its own state directory and skips what the host cannot do.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENABLED = os.environ.get("ESIM_TM_INTEGRATION") == "1"


def tm(*args, python=None, check=False):
    env = dict(os.environ, ESIM_TM_HOME=tempfile.mkdtemp(), PYTHONPATH=str(ROOT))
    p = subprocess.run([python or sys.executable, "-m", "esim_tool_manager", *args],
                       capture_output=True, text=True, env=env, cwd=ROOT)
    if check and p.returncode:
        raise AssertionError(f"{args} failed ({p.returncode}):\n{p.stdout}\n{p.stderr}")
    return p


@unittest.skipUnless(ENABLED, "set ESIM_TM_INTEGRATION=1 to run real-system tests")
class RealSystemTests(unittest.TestCase):
    def test_read_only_commands_work_on_this_os(self):
        for cmd in (["info"], ["list"], ["doctor"], ["verify"], ["log"]):
            self.assertIn(tm(*cmd).returncode, (0, 1), cmd)      # 1 = findings, never a crash
        data = json.loads(tm("list", "--json", check=True).stdout)
        self.assertIn("ngspice", data)

    def test_real_pip_install_in_a_virtualenv(self):
        venv = Path(tempfile.mkdtemp()) / "venv"
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
        py = str(venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
        tm("install", "watchdog", "-y", python=py, check=True)
        listing = json.loads(tm("list", "--json", python=py, check=True).stdout)
        self.assertTrue(listing["watchdog"]["installed"])
        self.assertEqual(tm("verify", "watchdog", python=py).returncode, 0)

    def test_real_pip_pin_then_update_is_verified(self):
        """Install an old watchdog, then update it: a genuine update with verification."""
        venv = Path(tempfile.mkdtemp()) / "venv"
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
        py = str(venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
        tm("install", "watchdog", "--version", "5.0.0", "-y", python=py, check=True)
        before = json.loads(tm("list", "--json", python=py, check=True).stdout)["watchdog"]["version"]
        self.assertEqual(before, "5.0.0")
        out = tm("update", "watchdog", "-y", python=py, check=True).stdout
        self.assertIn("updated 5.0.0 ->", out)
        after = json.loads(tm("list", "--json", python=py, check=True).stdout)["watchdog"]["version"]
        self.assertNotEqual(after, "5.0.0")

    @unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("apt-get"), "needs apt")
    def test_real_apt_install_and_verification(self):
        if os.geteuid() != 0 and not shutil.which("sudo"):
            self.skipTest("needs root or sudo")
        tm("install", "ngspice", "-y", check=True)
        listing = json.loads(tm("list", "--json", check=True).stdout)
        self.assertTrue(listing["ngspice"]["installed"])
        self.assertEqual(listing["ngspice"]["managed_by"], "apt")
        self.assertEqual(tm("verify", "ngspice").returncode, 0)
        bad = tm("install", "ngspice", "--version", "99", "-y")
        self.assertEqual(bad.returncode, 1)
        self.assertIn("not offered", bad.stdout)

    @unittest.skipUnless(sys.platform == "win32", "Windows only")
    def test_windows_package_managers_report_kicad_version(self):
        from esim_tool_manager.backends import ChocoBackend, WingetBackend
        found = False
        for backend, pkg in ((WingetBackend(), "KiCad.KiCad"), (ChocoBackend(), "kicad")):
            if backend.available():
                found = True
                self.assertRegex(backend.candidate_version(pkg) or "", r"^\d+\.\d+")
        if not found:
            self.skipTest("neither winget nor choco present")

    @unittest.skipUnless(sys.platform == "darwin" and shutil.which("brew"), "macOS with Homebrew only")
    def test_homebrew_reports_ngspice_version(self):
        from esim_tool_manager.backends import BrewBackend
        self.assertRegex(BrewBackend().candidate_version("ngspice") or "", r"^\d+")


if __name__ == "__main__":
    unittest.main()
