"""One canonical version (server/__init__.py): server, API, UI, service-worker cache, launcher, bootstrap,
release name and changelog can never disagree (v0.7.4 had server 0.7.4 / UI 0.7.3 / cache v073 / UA 0.7)."""
import re
import subprocess
import sys
import unittest

from server import __version__
from server.universe.http import HttpClient
from tests.helpers import ROOT

VERSION_RE = re.compile(r"(?<![\d.])v?\d+\.\d+\.\d+(?![\d.])")      # not IP addresses


class VersionTest(unittest.TestCase):
    def test_target_version(self):
        self.assertEqual(__version__, "0.8.0")

    def test_no_hard_coded_versions_in_the_ui_or_launchers(self):
        files = [p for p in (ROOT / "web").rglob("*") if p.is_file()] + \
                [ROOT / "run_windows.bat", ROOT / "run.sh"]
        for p in files:
            text = p.read_text(encoding="utf-8", errors="replace")
            hits = VERSION_RE.findall(text)
            self.assertEqual(hits, [], f"{p.relative_to(ROOT)} hard-codes a version: {hits}")
        self.assertIn("premove-v__VERSION__", (ROOT / "web" / "sw.js").read_text())
        bat = (ROOT / "run_windows.bat").read_text()
        self.assertIn(r"tools\bootstrap.py --version", bat)
        self.assertIn("v%PMS_VERSION%", bat)

    def test_only_one_definition_in_the_server(self):
        defs = [p for p in (ROOT / "server").rglob("*.py")
                if re.search(r'^__version__\s*=', p.read_text(encoding="utf-8"), re.M)]
        self.assertEqual([p.relative_to(ROOT).as_posix() for p in defs], ["server/__init__.py"])
        for p in list((ROOT / "server").rglob("*.py")) + list((ROOT / "tools").rglob("*.py")):
            self.assertNotIn(f'"{__version__}"', p.read_text(encoding="utf-8") if p.name != "__init__.py" else "",
                             p.relative_to(ROOT).as_posix())

    def test_everything_reports_the_canonical_version(self):
        self.assertEqual(HttpClient().headers["user-agent"], f"pre-move-scanner/{__version__}")
        out = subprocess.run([sys.executable, str(ROOT / "tools" / "bootstrap.py"), "--version"], capture_output=True,
                             text=True).stdout.strip()
        self.assertEqual(out, __version__)
        first = next(ln for ln in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8").splitlines() if ln.startswith("## "))
        self.assertIn(f"v{__version__}", first)
        self.assertIn(f"v{__version__}", (ROOT / "docs" / "TEST_REPORT_V080.md").read_text(encoding="utf-8")[:300])


if __name__ == "__main__":
    unittest.main()
