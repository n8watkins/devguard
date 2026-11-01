"""CLI output for ps, watch, port --list and config, with a fake /proc."""

from __future__ import annotations

import contextlib
import io
import json
import unittest
from unittest import mock

from helpers import GIB, FakeProc, Repo, TempCase

from devguard import cli
from devguard.procs import LinuxProcSource

NEXT = "/w/node_modules/.bin/next"


class CliTest(TempCase):
    def setUp(self):
        super().setUp()
        self.proc = FakeProc(self.tmp / "proc")
        self.repo = Repo(self.tmp / "r")
        self.config = self.tmp / "c.toml"
        self.config.write_text(f'state_dir = "{self.tmp / "state"}"\n[watch]\nfetch_interval_minutes = 0\n')
        patcher = mock.patch.object(cli, "default_source", lambda: LinuxProcSource(self.proc.root))
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--config", str(self.config), *args])
        return code, out.getvalue(), err.getvalue()

    def test_ps_table_and_json(self):
        done = self.repo.worktree("feature-done")
        self.repo.merge("feature-done")
        self.proc.add(10, ["node", NEXT, "dev", "-p", "4010"], cwd=str(done), rss=2 * GIB)
        self.proc.add(11, ["node", NEXT, "start", "-p", "4011"], cwd=str(self.repo.main))
        code, out, _ = self.run_cli("ps")
        self.assertEqual(code, 0)
        self.assertIn("Memory: 8.0G used of 16.0G", out)
        lines = out.splitlines()
        header = next(line for line in lines if line.startswith("PID"))
        self.assertEqual(header.split(), ["PID", "SERVER", "PORT", "MEM", "UPTIME", "PROJECT", "BRANCH", "STATE", "WORKTREE"])
        row = next(line for line in lines if line.lstrip().startswith("10 "))
        for part in ("next dev", "4010", "2.0G", "app", "feature-done", "merged", "wt-feature-done"):
            self.assertIn(part, row)
        self.assertIn("next preview", out)
        self.assertIn(cli.FINISHED_NOTE, out)
        code, out, _ = self.run_cli("ps", "--json")
        data = json.loads(out)
        self.assertEqual([s["pid"] for s in data["servers"]], [10, 11])
        self.assertTrue(data["servers"][0]["finished"])
        self.assertEqual(data["servers"][1]["state"], "default")

    def test_ps_empty(self):
        code, out, _ = self.run_cli("ps")
        self.assertIn("No dev servers running.", out)

    def test_watch_once_dry_run_and_log(self):
        self.proc.add(10, ["node", NEXT, "dev", "-p", "4010"], cwd=str(self.repo.main), rss=6 * GIB)
        with mock.patch("devguard.watch.os.kill") as kill:
            code, out, _ = self.run_cli("watch", "--once")
        kill.assert_not_called()
        self.assertEqual(code, 0)
        self.assertIn("WOULD-STOP port 4010", out)
        self.assertIn("dry run; use --apply to stop", out)
        code, out, _ = self.run_cli("watch", "--log")
        self.assertIn("WOULD-STOP port 4010", out)

    def test_watch_once_quiet_when_fine(self):
        self.proc.add(10, ["node", NEXT, "dev"], cwd=str(self.repo.main), rss=GIB)
        code, out, _ = self.run_cli("watch", "--once")
        self.assertIn("All dev servers are within limits.", out)

    def test_port_list_shows_collisions(self):
        a = self.repo.worktree("a", commit=False, push=False)
        self.config.write_text(f'[port.overrides]\n"{a}" = 4555\n"{self.repo.main}" = 4555\n')
        code, out, _ = self.run_cli("port", "--list", str(self.repo.main))
        self.assertEqual(code, 1)
        self.assertEqual(out.count("COLLISION"), 2)
        self.assertIn("chance that any two collide", out)

    def test_config_summary_and_usage(self):
        code, out, _ = self.run_cli("config")
        self.assertIn(f"Config file: {self.config}", out)
        self.assertIn(str(self.tmp / "state"), out)
        with contextlib.redirect_stdout(io.StringIO()) as help_text:
            self.assertEqual(cli.main([]), 2)
        self.assertIn("usage: devguard", help_text.getvalue())

    def test_sweep_rejects_non_repo(self):
        code, _, err = self.run_cli("sweep", str(self.tmp))
        self.assertEqual(code, 2)
        self.assertIn("not a git repository", err)


if __name__ == "__main__":
    unittest.main()
