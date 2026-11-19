"""Unit tests (stdlib unittest, no network or root needed): python -m unittest discover -s tests -v"""
import os
import tempfile
import unittest

os.environ["ESIM_TM_HOME"] = tempfile.mkdtemp()

from esim_tool_manager.backends import AptBackend, Backend  # noqa: E402
from esim_tool_manager.config import Config  # noqa: E402
from esim_tool_manager.manager import ToolManager  # noqa: E402
from esim_tool_manager.registry import Tool, load_registry  # noqa: E402
from esim_tool_manager.version_utils import compare, normalize, satisfies  # noqa: E402


class VersionTests(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize("1:8.0.4+dfsg-1"), "8.0.4")
        self.assertEqual(normalize("42+ds-3build1"), "42")
        self.assertEqual(normalize("garbage"), "")

    def test_compare(self):
        self.assertEqual(compare("5.20", "5.20.0"), 0)
        self.assertEqual(compare("8.0.4", "7.9"), 1)
        self.assertEqual(compare("36", "42"), -1)

    def test_satisfies(self):
        self.assertTrue(satisfies("42", ">=36"))
        self.assertFalse(satisfies("35", ">=36"))
        self.assertTrue(satisfies("42.1", "<43"))
        self.assertFalse(satisfies("", ">=1"))


class RegistryTests(unittest.TestCase):
    def test_bundled_registry_is_valid(self):
        reg = load_registry()
        self.assertIn("ngspice", reg)
        self.assertEqual(reg["numpy"].pip_dist, "numpy")
        for t in reg.values():
            for dep in t.requires:
                self.assertIn(dep, reg, f"{t.id} requires unknown tool {dep}")


class AptParsingTests(unittest.TestCase):
    def test_candidate_parse(self):
        import esim_tool_manager.backends as be
        orig = be.capture
        be.capture = lambda *a, **k: (0, "ngspice:\n  Installed: (none)\n  Candidate: 42+ds-3\n")
        try:
            self.assertEqual(AptBackend().candidate_version("ngspice"), "42+ds-3")
        finally:
            be.capture = orig


class FakeBackend(Backend):
    """In-memory package manager so install/update flows run without root."""
    name, supports_pin = "fake", True

    def __init__(self):
        self.installed, self.candidate, self.commands = {}, {"foo": "2.0"}, []

    def available(self): return True
    def installed_version(self, pkg): return self.installed.get(pkg)
    def candidate_version(self, pkg): return self.candidate.get(pkg)
    def list_versions(self, pkg): return ["1.0", "2.0"]
    def install_cmd(self, pkg, version=None): return ["fake-install", pkg, version or "latest"]
    def upgrade_cmd(self, pkg): return ["fake-upgrade", pkg]


class ManagerFlowTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeBackend()
        tool = Tool(id="foo", name="Foo", binary="foo", min_version="1.5", packages={"fake": "foo"})
        self.mgr = ToolManager(config=Config({}, path=None), registry={"foo": tool},
                               backends=[self.fake], assume_yes=True, out=lambda m: None)
        self.mgr.config.save = lambda: None
        self.mgr.config.path = None
        # fake system state: running a "command" mutates the fake package db
        def fake_run(backend, cmd):
            self.fake.commands.append(cmd)
            if cmd[0] == "fake-install":
                self.fake.installed["foo"] = cmd[2] if cmd[2] != "latest" else "2.0"
            if cmd[0] == "fake-upgrade":
                self.fake.installed["foo"] = "2.0"
            return 0
        self.mgr._run = fake_run
        self.mgr.probe = lambda t: (("foo" in self.fake.installed), "/x/foo", self.fake.installed.get("foo"))

    def test_install_then_skip(self):
        self.assertTrue(self.mgr.install("foo"))
        self.assertEqual(self.fake.installed["foo"], "2.0")
        n = len(self.fake.commands)
        self.assertTrue(self.mgr.install("foo"))          # already installed
        self.assertEqual(len(self.fake.commands), n)

    def test_pinned_install(self):
        self.assertTrue(self.mgr.install("foo", version="1.0"))
        self.assertEqual(self.fake.installed["foo"], "1.0")
        self.assertFalse(self.mgr.status(self.mgr.get_tool("foo")).meets_min)  # 1.0 < 1.5

    def test_unavailable_version_fails(self):
        self.assertFalse(self.mgr.install("foo", version="9.9"))

    def test_update_flow(self):
        self.mgr.install("foo", version="1.0")
        st = self.mgr.check_updates(["foo"])[0]
        self.assertTrue(st.update_available)
        self.assertTrue(self.mgr.update("foo"))
        self.assertEqual(self.fake.installed["foo"], "2.0")

    def test_pinned_tool_not_updated(self):
        self.mgr.install("foo", version="1.0")
        self.mgr.config.data["pinned_versions"] = {"foo": "1.0"}
        self.mgr.update("foo")
        self.assertEqual(self.fake.installed["foo"], "1.0")

    def test_unknown_tool(self):
        with self.assertRaises(KeyError):
            self.mgr.get_tool("nope")


if __name__ == "__main__":
    unittest.main()
