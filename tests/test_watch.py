"""watch: policy decisions, stopping, logging and the systemd timer install."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path

from helpers import GIB, FakeProc, Repo, TempCase

from devguard import watch
from devguard.config import Config
from devguard.procs import LinuxProcSource, MemInfo
from devguard.servers import Server

NEXT = "/w/node_modules/.bin/next"


def server(pid, rss_gb, kind="dev", state="open", cwd="/w/app", exempt=False):
    return Server(
        pid=pid, kind=kind, tool="next", argv=["node", NEXT, kind], port=str(4000 + pid), rss=int(rss_gb * GIB),
        tree=[pid], identities={pid: "1"}, age=60, cwd=cwd, cwd_gone=state == "removed",
        exempt_memory=exempt, state=state, reason=f"state {state}",
    )  # fmt: skip


PLENTY = MemInfo(16 * GIB, 8 * GIB, 0)
LOW = MemInfo(16 * GIB, int(1.0 * GIB), 0)


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.config = Config()

    def verbs(self, servers, mem=PLENTY):
        return [(a.verb, a.server.pid) for a in watch.plan(servers, mem, self.config)]

    def test_within_limits_does_nothing(self):
        self.assertEqual(self.verbs([server(1, 1.0), server(2, 2.9)]), [])

    def test_soft_and_hard_limits(self):
        self.assertEqual(self.verbs([server(1, 3.5), server(2, 5.5)]), [("warn", 1), ("stop", 2)])

    def test_limits_apply_to_dev_servers_only(self):
        self.assertEqual(self.verbs([server(1, 6, kind="preview")]), [])

    def test_finished_servers_are_stopped_once(self):
        actions = watch.plan([server(1, 6, state="merged"), server(2, 0.1, state="gone"), server(3, 0.1, state="removed")],
                             PLENTY, self.config)  # fmt: skip
        self.assertEqual([(a.verb, a.server.pid) for a in actions], [("stop", 1), ("stop", 2), ("stop", 3)])
        self.assertIn("its work is done", actions[0].reason)

    def test_stop_finished_can_be_turned_off(self):
        self.config.watch.stop_finished = False
        self.assertEqual(self.verbs([server(1, 0.5, state="merged")]), [])

    def test_floor_stops_the_largest_eligible(self):
        servers = [server(1, 2.0), server(2, 2.5), server(3, 0.5), server(4, 4.0, kind="preview")]
        self.assertEqual(self.verbs(servers, LOW), [("stop", 2)])

    def test_floor_ignores_small_servers(self):
        self.assertEqual(self.verbs([server(1, 0.9)], LOW), [])

    def test_floor_skips_already_stopped(self):
        self.assertEqual(self.verbs([server(1, 6.0), server(2, 2.0)], LOW), [("stop", 1), ("stop", 2)])

    def test_exempt_env_skips_limits_but_not_floor(self):
        self.assertEqual(self.verbs([server(1, 6.0, exempt=True)]), [])
        self.assertEqual(self.verbs([server(1, 6.0, exempt=True)], LOW), [("stop", 1)])

    def test_protected_paths_are_never_stopped(self):
        self.config.watch.protected_paths = ["/w/keep"]
        servers = [server(1, 9, cwd="/w/keep/app", state="merged"), server(2, 9, cwd="/w/keeper")]
        self.assertEqual(self.verbs(servers, LOW), [("stop", 2)])


class StopTest(TempCase):
    def test_signals_tree_and_skips_reused_pids(self):
        proc = FakeProc(self.tmp / "proc")
        proc.add(10, ["node", NEXT, "dev"], start_ticks=111)
        proc.add(11, ["node", "child"], ppid=10, start_ticks=222)
        source = LinuxProcSource(proc.root)
        s = server(10, 1)
        s.tree, s.identities = [10, 11, 12], {10: "111", 11: "999", 12: "333"}  # 11 reused, 12 gone
        sent = []

        def kill(pid, sig):
            sent.append((pid, sig))
            if sig == signal.SIGTERM:
                proc.remove(pid)

        watch.stop_server(s, source, grace=5, kill=kill, sleep=lambda _: None)
        self.assertEqual(sent, [(10, signal.SIGTERM)])

    def test_sigkill_after_grace(self):
        proc = FakeProc(self.tmp / "proc")
        proc.add(10, ["node", NEXT, "dev"], start_ticks=111)
        source = LinuxProcSource(proc.root)
        s = server(10, 1)
        s.identities = {10: "111"}
        sent, slept = [], []
        watch.stop_server(s, source, grace=2, kill=lambda p, sig: sent.append(sig), sleep=slept.append)
        self.assertEqual(sent, [signal.SIGTERM, signal.SIGKILL])
        self.assertEqual(sum(slept), 2)


class RunOnceTest(TempCase):
    def setUp(self):
        super().setUp()
        self.proc = FakeProc(self.tmp / "proc")
        self.config = Config(state_dir=str(self.tmp / "state"))
        self.config.watch.fetch_interval_minutes = 0
        self.lines = []
        self.log = watch.Log(self.tmp / "state" / "watch.log", echo=self.lines.append)
        self.repo = Repo(self.tmp / "repos")

    def test_dry_run_logs_and_never_kills(self):
        wt = self.repo.worktree("done")
        self.repo.merge("done")
        big = self.repo.worktree("big")
        self.proc.add(10, ["node", NEXT, "dev", "-p", "4010"], cwd=str(wt), start_ticks=5)
        self.proc.add(11, ["node", NEXT, "dev", "-p", "4011"], cwd=str(big), rss=6 * GIB, start_ticks=6)
        self.proc.add(12, ["node", NEXT, "dev", "-p", "4012"], cwd=str(self.repo.main), start_ticks=7)
        killed = []
        actions = watch.run_once(LinuxProcSource(self.proc.root), self.config, apply=False, log=self.log,
                                 kill=lambda p, s: killed.append(p), self_pid=1)  # fmt: skip
        self.assertEqual(killed, [])
        self.assertEqual(sorted(a.server.pid for a in actions), [10, 11])
        log = (self.tmp / "state" / "watch.log").read_text()
        self.assertIn("WOULD-STOP port 4010", log)
        self.assertIn("merged into origin/main", log)
        self.assertIn("WOULD-STOP port 4011", log)
        self.assertIn("over the 5 GiB limit", log)
        self.assertNotIn("4012", log)

    def test_apply_stops(self):
        wt = self.repo.worktree("gone")
        self.repo.merge("gone", delete=True, squash=True)
        self.proc.add(10, ["node", NEXT, "dev", "-p", "4010"], cwd=str(wt), start_ticks=5)
        killed = []

        def kill(pid, sig):
            killed.append((pid, sig))
            self.proc.remove(pid)

        watch.run_once(LinuxProcSource(self.proc.root), self.config, apply=True, log=self.log, kill=kill,
                       sleep=lambda _: None, self_pid=1)  # fmt: skip
        self.assertEqual(killed, [(10, signal.SIGTERM)])
        self.assertIn("STOP port 4010", self.lines[0])
        self.assertIn("deleted on origin", self.lines[0])

    def test_log_rotates(self):
        path = self.tmp / "state" / "watch.log"
        path.parent.mkdir(parents=True)
        path.write_text("x" * (watch.LOG_MAX_BYTES + 1))
        watch.Log(path, echo=None).write("hello")
        self.assertTrue(path.with_suffix(".log.1").exists())
        self.assertIn("hello", path.read_text())


class RealProcessTest(TempCase):
    """Starts a disposable process that looks like `next dev` and lets watch stop it."""

    @unittest.skipUnless(Path("/proc/self/stat").exists(), "needs /proc")
    def test_apply_stops_a_real_fake_server_over_the_limit(self):
        bin_dir = self.tmp / "app" / "node_modules" / ".bin"
        bin_dir.mkdir(parents=True)
        script = bin_dir / "next"
        script.write_text("import time\nblob = bytearray(64 * 1024 * 1024)\nwhile True:\n    time.sleep(1)\n")
        # argv[0] reads "node", like a real `node .../.bin/next dev` process.
        child = subprocess.Popen(["node", str(script), "dev", "-p", "4999"], executable=sys.executable, cwd=self.tmp / "app")
        try:
            config = Config(state_dir=str(self.tmp / "state"))
            config.watch.roots = [str(self.tmp)]
            config.watch.hard_gb = 0.03  # about 32 MiB
            config.watch.soft_gb = 0.01
            config.watch.floor_gb = 0
            config.watch.grace_seconds = 5
            source = LinuxProcSource()
            deadline = time.time() + 10
            while time.time() < deadline:
                found = [s for s in watch.discover(source, config) if s.pid == child.pid]
                if found and found[0].rss > 40 * 1024 * 1024:
                    break
                time.sleep(0.1)
            lines = []
            actions = watch.run_once(source, config, apply=True, log=watch.Log(self.tmp / "state" / "w.log", lines.append))
            self.assertEqual([a.server.pid for a in actions], [child.pid])
            self.assertEqual(child.wait(timeout=10), -signal.SIGTERM)
            self.assertIn("over the 0.03 GiB limit", lines[0])
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()


class InstallTest(TempCase):
    def runner(self, systemd_ok=True):
        calls = []

        def run(argv):
            calls.append(argv)
            return 0 if systemd_ok else 1

        return calls, run

    @unittest.skipUnless(Path("/run/systemd/system").is_dir(), "systemd not running here")
    def test_install_writes_units_and_enables_timer(self):
        calls, run = self.runner()
        out = []
        code = watch.install(apply=False, config_path=Path("/c/devguard.toml"), directory=self.tmp, run=run, out=out.append)
        self.assertEqual(code, 0)
        service = (self.tmp / watch.SERVICE).read_text()
        self.assertIn("-m devguard --config /c/devguard.toml watch --once", service)
        self.assertNotIn("--apply", service)
        self.assertIn("OnUnitActiveSec=1min", (self.tmp / watch.TIMER).read_text())
        self.assertIn(["systemctl", "--user", "enable", "--now", watch.TIMER], calls)
        self.assertTrue(any("dry run" in line for line in out))
        watch.install(apply=True, config_path=None, directory=self.tmp, run=run, out=out.append)
        self.assertIn("watch --once --apply", (self.tmp / watch.SERVICE).read_text())
        watch.uninstall(directory=self.tmp, run=run, out=out.append)
        self.assertFalse((self.tmp / watch.SERVICE).exists())
        self.assertIn(["systemctl", "--user", "disable", "--now", watch.TIMER], calls)

    def test_install_without_systemd_explains(self):
        calls, run = self.runner(systemd_ok=False)
        out = []
        self.assertEqual(watch.install(False, None, directory=self.tmp, run=run, out=out.append), 1)
        self.assertFalse((self.tmp / watch.SERVICE).exists())
        text = "\n".join(out)
        self.assertIn("devguard watch", text)
        if watch.is_wsl():
            self.assertIn("systemd=true", text)
            self.assertIn("wsl.exe --shutdown", text)

    def test_units_quote_paths(self):
        service, _ = watch.units(True, Path("/my configs/d.toml"), "/usr/bin")
        self.assertIn("'/my configs/d.toml'", service)
        self.assertIn(os.path.basename(sys.executable), service)


if __name__ == "__main__":
    unittest.main()
