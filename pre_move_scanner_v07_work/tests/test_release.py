"""Release-package regression tests: a release ZIP can never carry runtime or user state.

A previous release package included data/ and risked overwriting data/scanner_v07.db (and with it
the accumulated baselines). These tests fail if a release contains data/, config.json, .venv/,
any SQLite file (by name or by content), WAL / SHM files, caches, or an API key."""
import shutil
import unittest
import zipfile

from tests.helpers import ROOT, scratch_dir
from tools import make_release


class ReleaseBuildTest(unittest.TestCase):
    def setUp(self):
        self.d = scratch_dir("release")
        shutil.rmtree(self.d, ignore_errors=True)
        self.d.mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def copy_tree(self):
        """The real source tree (allow-listed paths) plus the runtime state a user's folder has."""
        tree = self.d / "tree"
        ign = shutil.ignore_patterns("__pycache__", "*.pyc")
        for d in make_release.INCLUDE_DIRS:
            if (ROOT / d).is_dir():
                shutil.copytree(ROOT / d, tree / d, ignore=ign)
        for f in make_release.INCLUDE_FILES:
            if (ROOT / f).is_file():
                shutil.copy2(ROOT / f, tree / f)
        for rel in ("data/scanner_v07.db", "data/scanner_v07.db-wal", "data/scanner_v07.db-shm", "config.json",
                    ".venv/Scripts/python.exe", "server/data/scanner_v07.db", "tests/config.json", "web/x.db-wal",
                    "tools/__pycache__/junk.pyc", "data/backups/scanner_v07.before-v0.8.0.db"):
            p = tree / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(make_release.SQLITE_MAGIC + b"planted runtime state")
        return tree

    def test_real_release_is_clean(self):
        tree = self.copy_tree()
        zpath, skipped = make_release.build(self.d / "dist", root=tree, env={})
        self.assertEqual(make_release.verify(zpath, env={}), [])
        with zipfile.ZipFile(zpath) as z:
            names = z.namelist()
        for n in names:
            parts = n.split("/")
            self.assertNotIn("data", parts[:-1], n)
            self.assertNotIn(".venv", parts[:-1], n)
            self.assertNotIn("__pycache__", parts[:-1], n)
            self.assertNotEqual(parts[-1], "config.json", n)
            self.assertFalse(n.endswith((".db", ".db-wal", ".db-shm")), n)
        for required in ("server/__init__.py", "server/intel/providers/evm.py", "web/js/universe.js",
                         "config.example.json", "requirements.txt", "run_windows.bat", "tools/bootstrap.py",
                         "RELEASE_MANIFEST.txt", "CHANGELOG.md"):
            self.assertIn(required, names)
        self.assertTrue(any("scanner_v07.db" in s for s, _ in skipped))       # planted inside server/: left out
        self.assertTrue(zpath.name.startswith("pre_move_scanner_v"))

    def zip_with(self, entries):
        p = self.d / "bad.zip"
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("server/__init__.py", '__version__ = "x"\n')
            z.writestr("config.example.json", "{}")
            z.writestr("requirements.txt", "")
            z.writestr("run_windows.bat", "")
            z.writestr("tools/bootstrap.py", "")
            for name, data in entries:
                z.writestr(name, data)
        return p

    def test_verifier_rejects_every_prohibited_file(self):
        bad = [("data/scanner_v07.db", b"x"), ("data/scanner_v07.db-wal", b"x"), ("scanner.db-shm", b"x"),
               ("config.json", b"{}"), (".venv/Scripts/python.exe", b"x"), ("venv/bin/python", b"x"),
               ("server/__pycache__/app.cpython-312.pyc", b"x"), ("docs/backup.sqlite", b"x"), (".env", b"A=1"),
               ("data/backups/scanner_v07.before.db", b"x"), ("tools/secrets.json", b"{}"),
               ("labels/notes.txt", make_release.SQLITE_MAGIC + b"hidden database"),
               ("docs/keys.md", b"my key " + b"CG-" + b"abcdefghijklmnopqrstuvwxyz0123")]
        problems = make_release.verify(self.zip_with(bad), env={})
        for name, _ in bad:
            self.assertTrue(any(p.startswith(name + ":") for p in problems), name)
        self.assertEqual(make_release.verify(self.zip_with([("docs/ok.md", b"fine")]), env={}), [])

    def test_api_key_values_never_ship(self):
        env = {"ETHERSCAN_API_KEY": "ABCDEFGH12345678TESTKEY"}
        p = self.zip_with([("docs/notes.md", b"left over: ABCDEFGH12345678TESTKEY")])
        self.assertTrue(any("ETHERSCAN_API_KEY" in x for x in make_release.verify(p, env=env)))
        tree = self.copy_tree()
        (tree / "docs" / "_leak.md").write_text("ABCDEFGH12345678TESTKEY")
        with self.assertRaises(RuntimeError):
            make_release.build(self.d / "dist", root=tree, env=env)            # the builder refuses outright
        self.assertFalse(list((self.d / "dist").glob("*.zip")) if (self.d / "dist").exists() else [])

    def test_gitignore_keeps_runtime_state_out_of_the_repo(self):
        if not (ROOT.parent / ".gitignore").exists():
            self.skipTest("not a git checkout (unpacked release)")
        text = (ROOT.parent / ".gitignore").read_text()
        for pattern in ("*.db", "*.db-wal", "*.db-shm", ".venv/", "config.json", "data/", "dist/"):
            self.assertIn(pattern.rstrip("/").split("/")[-1].replace("*", ""), text, pattern)


if __name__ == "__main__":
    unittest.main()
