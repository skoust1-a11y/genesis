"""Run with unittest, avoiding the application-importing pytest conftest."""

import contextlib
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[3]


def offline_guard(event, args):
    if event.startswith("socket.") or event == "sqlite3.connect":
        raise AssertionError(f"Offline provisioning attempted {event}")
    if event == "import" and args[0].startswith(("app.config", "app.trading", "app.db", "app.main", "telegram")):
        raise AssertionError(f"Runtime import attempted: {args[0]}")
    if event in ("open", "os.listdir", "os.scandir") and not isinstance(args[0], int):
        path = Path(os.fsdecode(args[0])).absolute()
        protected = (REPO / "trades", REPO / "data", REPO / "backend/data",
                     REPO / "backend/trades", Path.home() / ".genesis")
        if any(path == p or p in path.parents for p in protected) or path.suffix == ".db":
            raise AssertionError(f"Protected state access: {path}")


sys.addaudithook(offline_guard)
from app.state_authority import AuthorityError
from app.state_provisioning import DIRECTORIES, main, provision


class ProvisioningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.parent = Path(self.temp.name).resolve()
        self.parent.chmod(0o700)
        self.root = self.parent / "state"
        self.args = dict(state_root=str(self.root), approved_root=str(self.root),
                         deployments=[("research", "research-01", "research-account"),
                                      ("paper", "paper-01", "paper-account")])

    def run_plan(self, **changes):
        return provision(**(self.args | changes))

    def test_dry_run_no_mutations(self):
        before = self.parent.stat()
        with patch("os.mkdir", side_effect=AssertionError("write")), \
             patch("os.chmod", side_effect=AssertionError("write")), \
             patch("os.fchmod", side_effect=AssertionError("write")):
            result = self.run_plan()
        self.assertFalse(result["applied"])
        self.assertEqual(list(self.parent.iterdir()), [])
        self.assertEqual(self.parent.stat().st_mtime_ns, before.st_mtime_ns)

    def test_approval_missing_or_mismatched(self):
        for approval in (None, "", str(self.parent / "other")):
            with self.subTest(approval=approval), self.assertRaises(AuthorityError):
                self.run_plan(approved_root=approval)
        self.assertFalse(self.root.exists())

    def test_bad_environments_and_identity(self):
        for rows in ([('live', 'd', 'a'), ('research', 'r', 'b')],
                     [('unknown', 'd', 'a'), ('paper', 'p', 'b')],
                     [('research', 'r', 'a')],
                     [('research', 'd', 'a'), ('paper', 'd', 'b')],
                     [('research', 'r', 'a'), ('paper', 'p', 'a')],
                     [('research', '../r', 'a'), ('paper', 'p', 'b')]):
            with self.subTest(rows=rows), self.assertRaises(AuthorityError):
                self.run_plan(deployments=rows)
        self.assertFalse(self.root.exists())

    def test_apply_requires_matching_token(self):
        for token in ("", "unreviewed"):
            with self.assertRaises(AuthorityError):
                self.run_plan(apply_plan=token)
        token = self.run_plan()["plan_token"]
        with self.assertRaises(AuthorityError):
            self.run_plan(deployments=[('research', 'other', 'a'), ('paper', 'p', 'b')], apply_plan=token)
        self.assertFalse(self.root.exists())

    def test_apply_empty_isolated_private_scaffolding(self):
        token = self.run_plan()["plan_token"]
        self.assertTrue(self.run_plan(apply_plan=token)["applied"])
        manifest = json.loads((self.root / "provisioning.json").read_text())
        self.assertFalse(manifest["live_trading_enabled"])
        self.assertFalse(manifest["telegram_delivery_enabled"])
        files = [p for p in self.root.rglob("*") if p.is_file()]
        self.assertEqual(files, [self.root / "provisioning.json"])
        for path in [self.root, *self.root.rglob("*")]:
            info = path.lstat()
            self.assertEqual(stat.S_IMODE(info.st_mode), 0o700 if path.is_dir() else 0o600)
            self.assertEqual(info.st_uid, os.getuid())
        for env in ("research", "paper"):
            deployment = self.root / env / f"{env}-01"
            self.assertEqual({p.name for p in deployment.iterdir()}, set(DIRECTORIES))
            self.assertTrue(all(not list(p.iterdir()) for p in deployment.iterdir()))
        self.assertNotEqual((self.root / "research").stat().st_ino, (self.root / "paper").stat().st_ino)
        before = (self.root / "provisioning.json").read_bytes()
        with self.assertRaises(AuthorityError):
            self.run_plan(apply_plan=token)
        self.assertEqual((self.root / "provisioning.json").read_bytes(), before)

    def test_existing_unknown_file_and_hardlink_never_opened(self):
        source = self.parent / "unknown"
        source.write_bytes(b"synthetic evidence")
        os.link(source, self.root)
        self.assertEqual(source.stat().st_nlink, 2)
        with patch("builtins.open", side_effect=AssertionError("contents read")), self.assertRaises(AuthorityError):
            self.run_plan()
        self.assertEqual(source.read_bytes(), b"synthetic evidence")
        self.assertEqual(self.root.read_bytes(), source.read_bytes())

    def test_existing_directory_with_unknown_state_refused(self):
        self.root.mkdir(mode=0o700)
        unknown = self.root / "unknown"
        unknown.write_text("synthetic")
        with self.assertRaises(AuthorityError):
            self.run_plan()
        self.assertEqual(unknown.read_text(), "synthetic")

    def test_symlink_root_and_ancestor_refused(self):
        self.root.symlink_to(self.parent / "absent")
        with self.assertRaises(AuthorityError):
            self.run_plan()
        self.root.unlink()
        alias = self.parent / "alias"
        alias.symlink_to(self.parent, target_is_directory=True)
        with self.assertRaises((OSError, AuthorityError)):
            self.run_plan(state_root=str(alias / "state"), approved_root=str(alias / "state"))
        self.assertFalse(self.root.exists())

    def test_repository_root_refused_before_io(self):
        with patch("os.open", side_effect=AssertionError("repository I/O")):
            for root in (str(REPO / "new-state"), str(REPO), str(REPO.parent)):
                with self.assertRaises(AuthorityError):
                    self.run_plan(state_root=root, approved_root=root)

    def test_private_parent_required(self):
        self.parent.chmod(0o755)
        with self.assertRaises(AuthorityError):
            self.run_plan()
        self.assertFalse(self.root.exists())

    def test_missing_parent_not_created(self):
        root = str(self.parent / "missing" / "state")
        with self.assertRaises(OSError):
            self.run_plan(state_root=root, approved_root=root)
        self.assertEqual(list(self.parent.iterdir()), [])

    def test_relative_and_dot_paths_rejected(self):
        for root in ("state", str(self.parent / "x") + "/../state", str(self.root) + "/"):
            with self.assertRaises(AuthorityError):
                self.run_plan(state_root=root, approved_root=root)

    def test_cli_dry_run_and_missing_approval(self):
        argv = ["--state-root", str(self.root), "--approved-root", str(self.root)]
        for row in self.args["deployments"]:
            argv.extend(["--deployment", *row])
        with contextlib.redirect_stdout(io.StringIO()) as output:
            main(argv)
        self.assertFalse(json.loads(output.getvalue())["applied"])
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            main(["--state-root", str(self.root), "--deployment", "research", "r", "a"])
        self.assertEqual(error.exception.code, 2)
        self.assertFalse(self.root.exists())


if __name__ == "__main__":
    unittest.main()
