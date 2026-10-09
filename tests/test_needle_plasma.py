"""Tests for the Plasma plasmoid package and its shared JS helpers."""

import json
import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLASMA = os.path.join(ROOT, "plasma", "plasmoid", "com.pinecompute.needle")
LIB = os.path.join(PLASMA, "contents", "ui", "NeedleLib.js")
QML = os.path.join(PLASMA, "contents", "ui", "main.qml")
METADATA = os.path.join(PLASMA, "metadata.json")
SCHEMA = os.path.join(PLASMA, "contents", "config", "main.xml")
INSTALLER = os.path.join(ROOT, "plasma", "install-plasma.sh")
VERSION_FILE = os.path.join(ROOT, "VERSION")

NODE = shutil.which("node")


class NeedleLibTests(unittest.TestCase):
    """NeedleLib.js has no Qt imports, so node can run it directly."""

    @classmethod
    def setUpClass(cls):
        with open(LIB, encoding="utf-8") as f:
            source = f.read()
        cls.source = source.replace(".pragma library", "")

    @unittest.skipUnless(NODE, "node is not installed")
    def call(self, expr):
        program = self.source + "\nconsole.log(JSON.stringify(" + expr + "));"
        out = subprocess.run([NODE, "-e", program], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    @unittest.skipUnless(NODE, "node is not installed")
    def test_level(self):
        self.assertEqual(self.call("level(5)"), "crit")
        self.assertEqual(self.call("level(20)"), "warn")
        self.assertEqual(self.call("level(80)"), "ok")

    @unittest.skipUnless(NODE, "node is not installed")
    def test_compact(self):
        self.assertEqual(self.call("compact(1234)"), "1.2K")
        self.assertEqual(self.call("compact(133000)"), "133K")
        self.assertEqual(self.call("compact(1200000)"), "1.2M")

    @unittest.skipUnless(NODE, "node is not installed")
    def test_duration(self):
        self.assertEqual(self.call("duration(3600)"), "1h 0m")
        self.assertEqual(self.call("duration(59)"), "1m")

    @unittest.skipUnless(NODE, "node is not installed")
    def test_panel_text_tightest(self):
        providers = [{"id": "claude", "name": "Claude", "windows": [
            {"label": "5-hour", "used": 42.0},
            {"label": "Weekly", "used": 29.0},
        ]}]
        r = self.call("panelText(" + json.dumps(providers) + ", 'tightest', true)")
        self.assertEqual(r["text"], "C 58%")
        self.assertEqual(r["worst"], "ok")

    @unittest.skipUnless(NODE, "node is not installed")
    def test_panel_text_both_windows(self):
        providers = [{"id": "claude", "name": "Claude", "windows": [
            {"label": "5-hour", "used": 8.0},
            {"label": "Weekly", "used": 41.0},
        ]}]
        r = self.call("panelText(" + json.dumps(providers) + ", 'both', true)")
        self.assertEqual(r["text"], "C 92/59%")
        r = self.call("panelText(" + json.dumps(providers) + ", 'weekly', true)")
        self.assertEqual(r["text"], "C 59%")

    @unittest.skipUnless(NODE, "node is not installed")
    def test_panel_text_balance(self):
        providers = [{"id": "openrouter", "name": "OpenRouter",
                      "balance": {"remaining": 4.4, "total": 20.0}}]
        r = self.call("panelText(" + json.dumps(providers) + ", 'tightest', true)")
        self.assertEqual(r["text"], "$4")
        self.assertEqual(r["worst"], "warn")

    @unittest.skipUnless(NODE, "node is not installed")
    def test_binding_left_ignores_non_pace_windows(self):
        p = {"windows": [{"label": "Tool calls", "used": 99.0}, {"label": "Weekly", "used": 30.0}]}
        self.assertEqual(self.call("bindingLeft(" + json.dumps(p) + ")"), 70)

    @unittest.skipUnless(NODE, "node is not installed")
    def test_shell_quote(self):
        self.assertEqual(self.call("shellQuote(\"it's\")"), "'it'\\''s'")


class PlasmaPackageTests(unittest.TestCase):
    def test_metadata_matches_version_file(self):
        version = open(VERSION_FILE, encoding="utf-8").read().strip()
        meta = json.load(open(METADATA, encoding="utf-8"))
        self.assertEqual(meta["KPlugin"]["Version"], version)
        self.assertEqual(meta["KPlugin"]["Id"], "com.pinecompute.needle")
        self.assertEqual(meta["KPackageStructure"], "Plasma/Applet")
        self.assertEqual(meta["X-Plasma-API-Minimum-Version"], "6.0")

    def test_qml_references_versioned_bits(self):
        qml = open(QML, encoding="utf-8").read()
        self.assertIn("Plasma5Support.DataSource", qml)
        self.assertIn("engine: \"executable\"", qml)
        self.assertIn("NeedleLib.js", qml)
        # no leftover corruption markers from writing
        for marker in ("-> No.", "QML    }", "read } catch"):
            self.assertNotIn(marker, qml)

    def test_config_schema_lists_every_configuration_key(self):
        schema = open(SCHEMA, encoding="utf-8").read()
        qml = open(QML, encoding="utf-8").read()
        for key in ("refreshMinutes", "panelStyle", "panelWindow", "showRemaining",
                    "themeMode", "notify", "milestoneAnnounced"):
            self.assertIn(key, schema)
            self.assertIn("configuration." + key, qml)

    def test_installer_copies_package(self):
        script = open(INSTALLER, encoding="utf-8").read()
        self.assertIn("com.pinecompute.needle", script)
        self.assertIn("fetcher/needle.py", script)


if __name__ == "__main__":
    unittest.main()
