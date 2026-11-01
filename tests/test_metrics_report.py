"""metrics.jsonl writing and rotation, watch rate limits and restart loops, and devguard report."""

from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import time
import unittest
from pathlib import Path
from unittest import mock

from helpers import GIB, FakeProc, Repo, TempCase

from devguard import cli, metrics, watch
from devguard import report as rep
from devguard.config import Config
from devguard.procs import LinuxProcSource, MemInfo
from devguard.servers import Server

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "dev-watch.log"
NEXT = "/w/node_modules/.bin/next"
T0 = 1_790_000_000  # a fixed point in time for synthetic samples


def server(pid, rss_gb, cwd="/w/app", state="open", exempt=False, project="app", worktree="/w/app", age=600):
    return Server(
        pid=pid, kind="dev", tool="next", argv=["node", NEXT, "dev"], port=str(4000 + pid), rss=int(rss_gb * GIB),
        tree=[pid], identities={pid: str(pid * 10)}, age=age, cwd=cwd, cwd_gone=state == "removed",
        exempt_memory=exempt, project=project, worktree=worktree, branch="feat/x", state=state, reason=f"state {state}",
    )  # fmt: skip


# --- metrics ---------------------------------------------------------------


class MetricsWriteTest(TempCase):
    def setUp(self):
        super().setUp()
        self.state = self.tmp / "state"
        self.config = Config(state_dir=str(self.state))

    def record(self, t=T0, servers=None, actions=None, apply=True):
        mem = MemInfo(int(19.5 * GIB), int(12.25 * GIB), int(4.2 * GIB))
        return metrics.entry(servers or [], mem, actions or [], apply, self.config, t)

    def test_entry_is_compact_and_rounded(self):
        s = server(7, 4.71234, cwd="/w/app/apps/web", exempt=True)
        stop = watch.Action("stop", server(8, 6), "over the 5 GiB limit", "hard", "logged")
        quiet = watch.Action("warn", s, "over 3 GiB, restart it soon", "soft", "quiet")
        r = self.record(servers=[s, server(8, 6)], actions=[stop, quiet])
        self.assertEqual(r["mem"], {"total": 19.5, "avail": 12.25, "swap": 4.2})
        self.assertEqual(r["lim"], {"soft": 3.0, "hard": 5.0, "floor": 1.5})
        self.assertEqual(r["mode"], "apply")
        first = r["srv"][0]
        self.assertEqual(
            (first["gb"], first["dir"], first["exempt"], first["proj"], first["up"]), (4.71, "apps/web", True, "app", 600)
        )
        self.assertNotIn("exempt", r["srv"][1])
        self.assertNotIn("dir", r["srv"][1])
        self.assertEqual(r["act"][0], {"verb": "stop", "pid": 8, "rule": "hard", "why": "over the 5 GiB limit"})
        self.assertTrue(r["act"][1]["quiet"])
        self.assertEqual(self.record(actions=[stop], apply=False)["act"][0]["verb"], "would-stop")
        held = watch.Action("hold", server(9, 1), "its work is done", "merged", "hold")
        self.assertEqual(self.record(actions=[held])["act"][0]["verb"], "hold")
        self.assertNotIn("act", self.record())
        self.assertLess(len(json.dumps(r, separators=(",", ":"))), 700)

    def test_append_reads_back_with_mode_600(self):
        self.assertTrue(metrics.append(self.state, self.record(T0), T0))
        self.assertTrue(metrics.append(self.state, self.record(T0 + 60), T0 + 60))
        path = self.state / metrics.FILE
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual([r["t"] for r in metrics.read(self.state)], [T0, T0 + 60])
        self.assertEqual([r["t"] for r in metrics.read(self.state, since=T0 + 1)], [T0 + 60])

    def test_existing_file_is_tightened_to_600(self):
        self.state.mkdir()
        path = self.state / metrics.FILE
        path.write_text(json.dumps(self.record(T0)) + "\n")
        path.chmod(0o644)
        metrics.append(self.state, self.record(T0 + 60), T0 + 60)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_read_skips_broken_lines(self):
        self.state.mkdir()
        (self.state / metrics.FILE).write_text('{"t": 5}\nnot json\n{"no": "t"}\n[1]\n{"t": 9}\n')
        self.assertEqual([r["t"] for r in metrics.read(self.state)], [5, 9])

    def test_rotates_after_a_day_and_reads_across_files(self):
        metrics.append(self.state, self.record(T0), T0)
        metrics.append(self.state, self.record(T0 + 3600), T0 + 3600)
        later = T0 + metrics.SEGMENT_MAX_SECONDS + 10
        metrics.append(self.state, self.record(later), later)
        self.assertEqual([p.name for p in metrics.archives(self.state)], [f"{metrics.FILE}.{T0}"])
        self.assertEqual([r["t"] for r in metrics.read(self.state)], [T0, T0 + 3600, later])
        self.assertEqual([r["t"] for r in metrics.read(self.state, since=later)], [later])

    def test_rotates_when_large(self):
        with mock.patch.object(metrics, "SEGMENT_MAX_BYTES", 300):
            for i in range(4):
                metrics.append(self.state, self.record(T0 + i), T0 + i)
        self.assertGreaterEqual(len(metrics.archives(self.state)), 1)
        self.assertEqual(len(list(metrics.read(self.state))), 4)

    def test_prunes_old_archives_and_caps_total_size(self):
        self.state.mkdir()
        now = T0 + 40 * 86400
        for day in (0, 15, 25, 35):
            p = self.state / f"{metrics.FILE}.{T0 + day * 86400}"
            p.write_text("x" * 1000 + "\n")
            os.utime(p, (T0 + day * 86400 + 3600,) * 2)
        metrics.prune(self.state, now)
        self.assertEqual(len(metrics.archives(self.state)), 3)  # day 0 is older than 30 days
        with mock.patch.object(metrics, "KEEP_BYTES", 2500):
            metrics.prune(self.state, now)
        self.assertEqual(
            [p.name for p in metrics.archives(self.state)],
            [f"{metrics.FILE}.{T0 + 25 * 86400}", f"{metrics.FILE}.{T0 + 35 * 86400}"],
        )

    def test_write_failure_never_raises(self):
        (self.state / metrics.FILE).mkdir(parents=True)  # a directory where the file should be
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertFalse(metrics.append(self.state, self.record(), T0))
        self.assertIn("could not write metrics", err.getvalue())
        blocker = self.tmp / "file"
        blocker.write_text("")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(metrics.append(blocker / "state", self.record(), T0))


# --- watch: metrics, rate limits and restart loops ----------------------------


class WatchMonitoringTest(TempCase):
    def setUp(self):
        super().setUp()
        self.proc = FakeProc(self.tmp / "proc")
        self.source = LinuxProcSource(self.proc.root)
        self.config = Config(state_dir=str(self.tmp / "state"))
        self.config.watch.fetch_interval_minutes = 0
        self.lines = []
        self.log = watch.Log(self.tmp / "state" / "watch.log", echo=self.lines.append)
        self.now = float(T0)
        self.killed = []

    def kill(self, pid, sig):
        self.killed.append(pid)
        with contextlib.suppress(FileNotFoundError):
            self.proc.remove(pid)

    def run_pass(self, apply=True, minutes=1):
        actions = watch.run_once(self.source, self.config, apply, self.log, kill=self.kill, sleep=lambda _: None,
                                 self_pid=1, clock=lambda: self.now)  # fmt: skip
        self.now += minutes * 60
        return actions

    def logged(self):
        path = self.tmp / "state" / "watch.log"
        return path.read_text().splitlines() if path.exists() else []

    def test_each_pass_writes_a_sample(self):
        repo = Repo(self.tmp / "r")
        self.proc.add(10, ["node", NEXT, "dev", "-p", "4010"], cwd=str(repo.main), rss=int(3.5 * GIB), start_ticks=5)
        self.run_pass(apply=False)
        self.run_pass(apply=False)
        samples = list(metrics.read(self.tmp / "state"))
        self.assertEqual(len(samples), 2)
        self.assertEqual(samples[0]["mode"], "dry")
        self.assertEqual(samples[0]["srv"][0]["port"], "4010")
        self.assertEqual(samples[0]["srv"][0]["state"], "default")
        self.assertEqual(samples[0]["act"], [{"verb": "warn", "pid": 10, "rule": "soft", "why": "over 3 GiB, restart it soon"}])
        self.assertTrue(samples[1]["act"][0]["quiet"])

    def test_metrics_can_be_turned_off(self):
        self.config.watch.metrics = False
        self.run_pass()
        self.assertFalse((self.tmp / "state" / metrics.FILE).exists())

    def test_watch_survives_unwritable_metrics(self):
        (self.tmp / "state" / metrics.FILE).mkdir(parents=True)
        self.proc.add(10, ["node", NEXT, "dev", "-p", "4010"], cwd="/", rss=6 * GIB, start_ticks=5)
        with contextlib.redirect_stderr(io.StringIO()):
            actions = self.run_pass()
        self.assertEqual([a.rule for a in actions], ["hard"])
        self.assertEqual(self.killed, [10])

    def test_warnings_are_rate_limited(self):
        self.proc.add(10, ["node", NEXT, "dev", "-p", "4010"], cwd="/", rss=int(3.2 * GIB), start_ticks=5)
        for _ in range(5):
            self.run_pass()
        self.assertEqual(len(self.logged()), 1)
        self.assertEqual(len(self.lines), 5)  # still echoed every pass
        self.proc.remove(10)
        self.proc.add(10, ["node", NEXT, "dev", "-p", "4010"], cwd="/", rss=int(3.8 * GIB), start_ticks=5)
        self.run_pass()  # grew by 0.6 GiB
        self.assertEqual(len(self.logged()), 2)
        self.run_pass()
        self.assertEqual(len(self.logged()), 2)
        self.now += watch.WARN_REPEAT_SECONDS
        self.run_pass()  # 15 minutes later
        self.assertEqual(len(self.logged()), 3)

    def test_a_new_process_is_warned_again(self):
        self.proc.add(10, ["node", NEXT, "dev"], cwd="/", rss=int(3.2 * GIB), start_ticks=5)
        self.run_pass()
        self.proc.remove(10)
        self.proc.add(10, ["node", NEXT, "dev"], cwd="/", rss=int(3.2 * GIB), start_ticks=6)  # pid reused
        self.run_pass()
        self.assertEqual(len(self.logged()), 2)
        state = json.loads((self.tmp / "state" / watch.STATE_FILE).read_text())
        self.assertEqual(list(state["warned"]), ["warn:10:6"])  # the old process was forgotten

    def test_dry_run_would_stops_are_rate_limited(self):
        self.proc.add(10, ["node", NEXT, "dev"], cwd="/", rss=6 * GIB, start_ticks=5)
        for _ in range(3):
            self.run_pass(apply=False)
        self.assertEqual(len(self.logged()), 1)
        self.assertIn("WOULD-STOP", self.logged()[0])

    def test_corrupt_state_starts_fresh(self):
        (self.tmp / "state").mkdir()
        (self.tmp / "state" / watch.STATE_FILE).write_text("{not json")
        self.proc.add(10, ["node", NEXT, "dev"], cwd="/", rss=int(3.2 * GIB), start_ticks=5)
        self.run_pass()
        self.assertEqual(len(self.logged()), 1)
        self.assertEqual(stat.S_IMODE((self.tmp / "state" / watch.STATE_FILE).stat().st_mode), 0o600)

    def test_restart_loop_is_held_once_then_resumes(self):
        repo = Repo(self.tmp / "r")
        wt = repo.worktree("done")
        repo.merge("done")

        def restart(pid, rss=GIB):
            self.proc.add(pid, ["node", NEXT, "dev", "-p", "4050"], cwd=str(wt), rss=rss, start_ticks=pid)

        for pid in (20, 21, 22):  # stopped three times, restarted each time
            restart(pid)
            self.run_pass()
        self.assertEqual(self.killed, [20, 21, 22])
        restart(23)
        actions = self.run_pass()
        self.assertEqual(actions[0].outcome, "loop")
        self.assertEqual(self.killed, [20, 21, 22])
        self.assertIn("LOOP port 4050 pid 23", self.logged()[-1])
        self.assertIn("likely by an agent", self.logged()[-1])
        for _ in range(3):  # held: no more stops and no more log lines
            self.assertEqual(self.run_pass()[0].outcome, "hold")
        self.assertEqual(len([line for line in self.logged() if "port 4050" in line]), 4)
        samples = list(metrics.read(self.tmp / "state"))
        self.assertEqual([s["act"][0]["verb"] for s in samples], ["stop", "stop", "stop", "loop", "hold", "hold", "hold"])
        # the memory limits still apply while held
        self.proc.remove(23)
        restart(24, rss=6 * GIB)
        actions = self.run_pass()
        self.assertEqual(self.killed[-1], 24)
        self.assertEqual([(a.verb, a.rule) for a in actions], [("hold", "merged"), ("stop", "hard")])
        self.now += watch.LOOP_HOLD_SECONDS
        restart(25)
        self.run_pass()
        self.assertEqual(self.killed[-1], 25)  # the hold expired: stops resume


# --- report: parsing and import -----------------------------------------------


class ParseTest(unittest.TestCase):
    def test_rule_of(self):
        cases = {
            "over the 5 GiB limit": "hard",
            "over 3 GiB, restart it soon": "soft",
            "WSL has only 1.4 GiB available (floor 1.5 GiB)": "floor",
            "only 1.0 GiB available (floor 1.5 GiB); this is the largest dev server": "floor",
            "its work is done (branch a is merged into origin/main)": "merged",
            "its work is done (branch a was merged and deleted on origin)": "gone",
            "its work is done (branch a was deleted on origin (usually after a merge))": "gone",
            "its work is done (its worktree was removed)": "removed",
            "something else": "other",
        }
        for reason, rule in cases.items():
            self.assertEqual(rep.rule_of("stop", reason), rule, reason)
        self.assertEqual(rep.rule_of("loop", "stopped 3 times"), "loop")

    def test_parse_both_formats(self):
        lines = [
            "2026-11-01 09:48:43 STOP port 4101 pid 2433379 next dev 2.1G ~/code/app-wt/search: "
            "its work is done (branch s is merged into origin/main)",
            "2026-11-01 09:48:44 WOULD-STOP port 4102 pid 7 vite preview 812M /x/y: "
            "over the 5 GiB limit (dry run; use --apply to stop)",
            "2026-11-01 09:48:45 LOOP port 4103 pid 8 next dev 1.0G /x/z: stopped 3 times in 60 minutes and restarted each time",
            "2026-11-01 07:44:51 WARN port 4519 pid 1667282 3.1 GiB /x/apps/hub: over 3 GiB, restart it soon",
            "2026-11-01 07:44:52 STOP port ? pid 5 6.1 GiB /x/q: over the 5 GiB limit",
            "2026-11-01 07:44:53 ERROR pass failed: OSError: boom",
            "garbage",
        ]  # fmt: skip
        events = rep.parse_log(lines, "devguard")
        expected = [("stop", "merged"), ("would-stop", "hard"), ("loop", "loop"), ("warn", "soft"), ("stop", "hard")]
        self.assertEqual([(e.verb, e.rule) for e in events], expected)
        self.assertEqual([e.gb for e in events], [2.1, 0.79, 1.0, 3.1, 6.1])
        self.assertEqual(events[0].where, "~/code/app-wt/search")
        self.assertEqual(events[3].pid, 1667282)

    def test_parse_since(self):
        self.assertEqual(rep.parse_since("7d"), 7 * 86400)
        self.assertEqual(rep.parse_since("12h"), 12 * 3600)
        self.assertEqual(rep.parse_since("2w"), 14 * 86400)
        self.assertEqual(rep.parse_since("30m"), 1800)
        with self.assertRaises(ValueError):
            rep.parse_since("week")


class AppNamesTest(TempCase):
    def test_git_then_known_project_prefix_then_path(self):
        repo = Repo(self.tmp / "r")
        (repo.main / "apps" / "web").mkdir(parents=True)
        names = rep.app_names(
            {str(repo.main / "apps" / "web"), str(self.tmp / "r" / "app-feature" / "apps" / "web"), "/gone/elsewhere"},
            {"?": {""}},
        )
        self.assertEqual(names[str(repo.main / "apps" / "web")], "app apps/web")
        self.assertEqual(names[str(self.tmp / "r" / "app-feature" / "apps" / "web")], "app apps/web")
        self.assertEqual(names["/gone/elsewhere"], "/gone/elsewhere")
        known = {"shop": {"", "tools/admin"}}
        names = rep.app_names(
            {
                "/c/shop-x",
                "/c/shop-wt/feat-2/apps/web",
                "/c/shop-wt/feat-3/tools/admin",
                "/c/shop-y/src",
                "/c/shop-z/tools/admin",
            },
            known,
        )
        self.assertEqual(names["/c/shop-x"], "shop")
        self.assertEqual(names["/c/shop-wt/feat-2/apps/web"], "shop apps/web")
        self.assertEqual(names["/c/shop-wt/feat-3/tools/admin"], "shop tools/admin")
        self.assertEqual(names["/c/shop-y/src"], "shop")
        self.assertEqual(names["/c/shop-z/tools/admin"], "shop tools/admin")


class ImportTest(TempCase):
    """The synthetic dev-watch log covers every report section that log-only data can fill."""

    def setUp(self):
        super().setUp()
        self.config = Config(state_dir=str(self.tmp / "state"))
        inputs = rep.load(self.tmp / "state", 0, FIXTURE)
        self.r = rep.build(inputs, self.config, 0, time.time())
        self.text = rep.render(self.r)

    def test_sources_mark_imported_data(self):
        self.assertEqual(self.r["passes"], 0)
        self.assertEqual(self.r["sources"]["dev-watch"]["events"], 25)
        self.assertIn("imported from dev-watch log", self.text)
        self.assertIn("events only, no memory samples", self.text)
        self.assertNotIn("Memory\n", self.text)

    def test_actions_by_reason(self):
        by = {(a["verb"], a["rule"]): a["dev-watch"] for a in self.r["actions"]}
        self.assertEqual(by[("warn", "soft")], {"events": 13, "servers": 2})
        self.assertEqual(by[("stop", "hard")]["events"], 3)
        self.assertEqual(by[("stop", "merged")]["events"], 4)
        self.assertEqual(by[("stop", "floor")]["events"], 3)
        self.assertEqual(by[("stop", "removed")]["events"], 1)
        self.assertEqual(by[("stop", "gone")]["events"], 1)
        self.assertEqual(self.r["stops"], {"total": 12, "finished": 6, "hard": 3, "floor": 3})
        self.assertIn("DEV-WATCH (IMPORTED)", self.text)

    def test_offenders_restarts_and_loops(self):
        top = self.r["offenders"][0]
        self.assertEqual(top["app"], "/home/dev/code/shop-a/apps/web")
        self.assertEqual((top["hardStops"], top["importedServersOverSoft"]), (3, 3))
        self.assertEqual(top["maxPeakGb"], 5.3)
        self.assertEqual(self.r["restartedStops"], 5)  # 2 hard-limit stops and 3 of the 4 finished stops
        web = next(x for x in self.r["restarts"] if x["where"].endswith("shop-a/apps/web"))
        self.assertEqual((web["restarted"], web["stops"], web["rules"]), (2, 3, {"hard": 2}))
        loops = {lp["where"].rsplit("/code/", 1)[1]: lp for lp in self.r["loops"]}
        self.assertEqual(loops["shop-b"]["stops"], 4)
        self.assertEqual(loops["shop-b"]["rule"], "merged")
        self.assertEqual(loops["shop-a/apps/web"]["rule"], "hard")
        self.assertEqual(self.r["warningStreak"]["count"], 12)
        self.assertIn("Stop-restart loops", self.text)
        self.assertIn("Repeat offenders", self.text)

    def test_suggestions_from_imported_data(self):
        texts = [s["text"] for s in self.r["suggestions"]]
        joined = "\n".join(texts)
        self.assertIn("shop-a/apps/web hit the hard limit 3 times", joined)
        self.assertIn("3 stops happened because memory fell below 1.5 GiB", joined)
        self.assertIn("2 stops of /home/dev/code/shop-a/apps/web were followed by a restart within 10 minutes", joined)
        self.assertIn("shop-b was stopped 4 times in 6 minutes after its work was done", joined)
        self.assertIn("6 of 12 stops were dev servers left running in finished worktrees", joined)
        self.assertIn("Soft warnings repeated every minute", joined)
        self.assertTrue(all(s["evidence"] for s in self.r["suggestions"]))
        self.assertIn("What to change", self.text)
        self.assertIn("evidence:", self.text)

    def test_since_filters_and_missing_file_is_an_error(self):
        inputs = rep.load(self.tmp / "state", time.time(), FIXTURE)
        r = rep.build(inputs, self.config, time.time(), time.time())
        self.assertEqual(r["sources"], {})
        self.assertIn("No data in this period yet.", rep.render(r))
        with self.assertRaises(ValueError):
            rep.load(self.tmp / "state", 0, self.tmp / "missing.log")


# --- report: metrics-based sections and suggestion rules ----------------------


def srv(pid, gb, up, proj="shop", d="apps/web", wt=None, exempt=False, kind="dev"):
    s = {"pid": pid, "kind": kind, "tool": "next", "port": str(4000 + pid), "proj": proj, "wt": wt or f"~/c/{proj}-{pid}",
         "state": "open", "gb": gb, "up": up}  # fmt: skip
    if d:
        s["dir"] = d
    if exempt:
        s["exempt"] = True
    return s


def sample(t, avail=10.0, servers=(), acts=(), total=20.0, swap=0.5, mode="apply"):
    r = {"t": t, "mode": mode, "mem": {"total": total, "avail": avail, "swap": swap},
         "lim": {"soft": 3.0, "hard": 5.0, "floor": 1.5}, "srv": list(servers)}  # fmt: skip
    if acts:
        r["act"] = list(acts)
    return r


class MetricsReportTest(unittest.TestCase):
    def build(self, samples, events=()):
        return rep.build(rep.Inputs(samples=samples, events=list(events)), Config(), T0 - 1, T0 + 86400)

    def heavy_app_samples(self):
        """Four runs of `shop apps/web`; three pass 3 GiB about 10 minutes after start."""
        samples = []
        t = T0
        for run, peaks in enumerate(([1.0, 2.0, 3.5, 4.0], [1.5, 3.2, 3.6, 3.9], [2.0, 2.5, 3.1, 3.3], [0.8, 1.0, 1.1, 1.2])):
            pid = 100 + run
            for i, gb in enumerate(peaks):
                acts = (
                    [{"verb": "warn", "pid": pid, "rule": "soft", "why": "over 3 GiB", **({"quiet": True} if i else {})}]
                    if gb > 3
                    else []
                )
                samples.append(sample(t, servers=[srv(pid, gb, 300 * (i + 1))], acts=acts))
                t += 60
        return samples

    def test_memory_concurrency_and_cost(self):
        samples = [
            sample(T0, avail=10, servers=[srv(1, 2.0, 60)]),
            sample(T0 + 60, avail=2.0, servers=[srv(1, 2.0, 120), srv(2, 1.0, 60)], swap=3.25),
            sample(T0 + 120, avail=1.0, servers=[srv(1, 2.0, 180), srv(2, 1.0, 120), srv(3, 4.0, 60, exempt=True)]),
            sample(T0 + 180, avail=1.2, servers=[srv(1, 2.0, 240), srv(4, 0.5, 60, kind="preview")]),
        ]
        r = self.build(samples)
        m = r["memory"]
        self.assertEqual((m["passesUnderFloor"], m["secondsUnderFloor"]), (2, 120))
        self.assertEqual((m["passesNearFloor"], m["secondsNearFloor"]), (1, 60))
        self.assertEqual((m["lowestAvailableGb"], m["peakUsedGb"], m["peakSwapGb"]), (1.0, 19.0, 3.25))
        c = r["concurrency"]
        self.assertEqual((c["peakDevServers"], c["medianDevServers"], c["medianDevServersUnderFloor"]), (3, 1.5, 2))
        self.assertEqual(r["exempt"], [{"app": "shop apps/web", "runs": 1, "peakGb": 4.0, "medianPeakGb": 4.0}])
        self.assertEqual(r["gibHours"][0]["project"], "shop")
        self.assertAlmostEqual(r["gibHours"][0]["gibHours"], round((2 + 3 + 7 + 2.5) / 60, 1))
        text = rep.render(r)
        for part in ("under the floor:  2m (2 passes)", "peak swap 3.2G", "dev servers at once: median 1.5, peak 3",
                     "Memory-exempt servers", "Most expensive projects"):  # fmt: skip
            self.assertIn(part, text)

    def test_runs_time_to_limit_and_soft_suggestion(self):
        r = self.build(self.heavy_app_samples())
        o = r["offenders"][0]
        self.assertEqual((o["app"], o["runs"], o["runsOverSoft"], o["worktrees"]), ("shop apps/web", 4, 3, 4))
        self.assertEqual(o["medianSecondsToSoft"], 900)
        self.assertEqual(o["typicalPeakGb"], 3.9)
        joined = "\n".join(s["text"] for s in r["suggestions"])
        self.assertIn("shop apps/web passed 3 GiB in 3 of 4 runs, median 15m after start", joined)
        self.assertIn("Turbopack", joined)
        self.assertNotIn("Soft warnings repeated", joined)  # quiet repeats are not spam
        warn = next(a for a in r["actions"] if a["verb"] == "warn")
        self.assertEqual(warn["devguard"], {"events": 7, "servers": 3})

    def test_soft_suggestion_needs_enough_runs(self):
        r = self.build(self.heavy_app_samples()[:8])  # two runs only
        self.assertFalse(any("passed 3 GiB" in s["text"] for s in r["suggestions"]))

    def floor_samples(self, crowd):
        samples, t = [], T0
        for i in range(10):  # calm: one or two servers
            samples.append(sample(t, servers=[srv(1, 2.0, 60 * i + 60)] + ([srv(2, 1.0, 60)] if i % 2 else [])))
            t += 60
        for k in range(2):  # memory runs out with `crowd` servers up
            servers = [srv(10 + j, 2.5, 600) for j in range(crowd)]
            act = {"verb": "stop", "pid": 10 + k, "rule": "floor", "why": "only 1.2 GiB available (floor 1.5 GiB)"}
            samples.append(sample(t, avail=1.2, servers=servers, acts=[act]))
            t += 60
        return samples

    def test_floor_with_many_servers_suggests_fewer(self):
        r = self.build(self.floor_samples(crowd=5))
        self.assertEqual(r["concurrency"]["medianDevServersAtFloorStops"], 5)
        joined = "\n".join(s["text"] for s in r["suggestions"])
        self.assertIn("Floor stops happened with 5+ dev servers running: lower slot.count", joined)
        self.assertIn(".wslconfig", joined)

    def test_floor_with_few_servers_does_not_blame_concurrency(self):
        r = self.build(self.floor_samples(crowd=2))
        self.assertFalse(any("servers running" in s["text"] for s in r["suggestions"]))
        self.assertFalse(any("fell below" in s["text"] for s in r["suggestions"]))  # only 2 floor stops

    def test_restart_after_stop_seen_in_samples(self):
        stop = {"verb": "stop", "pid": 1, "rule": "hard", "why": "over the 5 GiB limit"}
        wt = "~/c/shop-a"
        samples = [
            sample(T0, servers=[srv(1, 5.5, 3000, wt=wt)], acts=[stop]),
            sample(T0 + 300, servers=[srv(2, 0.5, 120, wt=wt)]),  # started 3 minutes after the stop
            sample(T0 + 3600, servers=[srv(2, 5.5, 3420, wt=wt)], acts=[{**stop, "pid": 2}]),
            sample(T0 + 3660, servers=[srv(3, 0.4, 30, wt=wt)]),
            sample(T0 + 9000, servers=[srv(3, 5.6, 5370, wt=wt)], acts=[{**stop, "pid": 3}]),
            sample(T0 + 9000 + 700, servers=[srv(4, 0.4, 30, wt=wt)]),  # more than 10 minutes later
        ]
        r = self.build(samples)
        self.assertEqual(r["restarts"][0]["where"], "~/c/shop-a/apps/web")
        self.assertEqual((r["restarts"][0]["restarted"], r["restarts"][0]["stops"]), (2, 3))
        self.assertEqual(r["loops"], [])  # an hour apart is not a loop
        joined = "\n".join(s["text"] for s in r["suggestions"])
        self.assertIn("2 stops of ~/c/shop-a/apps/web were followed by a restart within 10 minutes", joined)
        self.assertIn("protected_paths", joined)
        # a server that was already running elsewhere in the worktree is not a restart
        samples[1]["srv"][0]["up"] = 4000
        samples[3]["srv"][0]["up"] = 4000
        self.assertEqual(self.build(samples)["restartedStops"], 0)

    def test_quiet_period_suggests_nothing(self):
        r = self.build([sample(T0 + i * 60, servers=[srv(1, 1.0, 60 * i + 60)]) for i in range(30)])
        self.assertEqual(r["suggestions"], [])
        self.assertIn("Nothing stands out in this period.", rep.render(r))

    def test_same_pass_stops_are_not_a_loop(self):
        events = [rep.Event(T0, "stop", "gone", pid, 0.5, "~/c/x", "dev-watch", "x") for pid in (1, 2, 3)]
        self.assertEqual(self.build([], events)["loops"], [])

    def test_dry_run_suggests_apply(self):
        act = {"verb": "would-stop", "pid": 1, "rule": "merged", "why": "its work is done"}
        r = self.build([sample(T0, servers=[srv(1, 1.0, 60)], acts=[act], mode="dry")])
        self.assertTrue(any("watch --install --apply" in s["text"] for s in r["suggestions"]))
        self.assertIn("would stop (dry run): branch merged", rep.render(r))

    def test_watch_log_fills_only_times_without_metrics(self):
        state = Path(self._tmp_dir())
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(T0 - 3600))
        same = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(T0 + 30))
        (state / "watch.log").write_text(
            f"{stamp} STOP port 4101 pid 9 next dev 2.1G ~/c/old: its work is done (branch a is merged into origin/main)\n"
            f"{same} STOP port 4102 pid 1 next dev 5.5G ~/c/shop-1/apps/web: over the 5 GiB limit\n"
        )
        metrics.append(state, sample(T0, servers=[srv(1, 5.5, 60)], acts=[{"verb": "stop", "pid": 1, "rule": "hard"}]), T0)
        metrics.append(state, sample(T0 + 60), T0 + 60)
        r = rep.build(rep.load(state, 0), Config(), 0, T0 + 120)
        self.assertEqual(r["stops"], {"total": 2, "finished": 1, "hard": 1, "floor": 0})

    def _tmp_dir(self):
        import tempfile

        d = tempfile.TemporaryDirectory(prefix="devguard-test-")
        self.addCleanup(d.cleanup)
        return d.name


class ReportCliTest(TempCase):
    def run_cli(self, *args):
        config = self.tmp / "c.toml"
        config.write_text(f'state_dir = "{self.tmp / "state"}"\n')
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--config", str(config), *args])
        return code, out.getvalue(), err.getvalue()

    def test_text_json_and_errors(self):
        now = time.time()
        metrics.append(self.tmp / "state", sample(int(now) - 60, servers=[srv(1, 1.0, 60)]), now)
        code, out, _ = self.run_cli("report")
        self.assertEqual(code, 0)
        self.assertIn("Watch passes recorded: 1 (1 apply)", out)
        code, out, _ = self.run_cli("report", "--json", "--since", "30d", "--import-dev-watch", str(FIXTURE))
        data = json.loads(out)
        self.assertEqual(data["passes"], 1)
        self.assertGreater(now - data["since"], 29 * 86400)
        code, _, err = self.run_cli("report", "--since", "soon")
        self.assertEqual(code, 1)
        self.assertIn("--since", err)
        code, _, err = self.run_cli("report", "--import-dev-watch", str(self.tmp / "nope.log"))
        self.assertEqual(code, 1)
        self.assertIn("cannot read", err)


if __name__ == "__main__":
    unittest.main()
