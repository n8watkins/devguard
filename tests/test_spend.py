"""spend: per-turn token spend from Claude Code and Codex session logs."""

from __future__ import annotations

import json
import os
import time
import tomllib
from pathlib import Path
from unittest import mock

from helpers import GIB, TempCase

from devguard import spend, status, watch
from devguard.config import Config, ConfigError, parse
from devguard.procs import MemInfo, Snapshot


def claude_prompt(text="go"):
    return {"type": "user", "message": {"role": "user", "content": text}, "cwd": "/w/app", "timestamp": "2026-11-09T10:00:00Z"}


def claude_tool_result():
    return {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": "SECRET-OUTPUT"}]}}


def claude_reply(msg_id, fresh=100, written=50, out=10, read=99_999):
    usage = {"input_tokens": fresh, "cache_creation_input_tokens": written, "output_tokens": out, "cache_read_input_tokens": read}
    return {"type": "assistant", "message": {"id": msg_id, "usage": usage, "content": [{"type": "text", "text": "x"}]}}


def codex_total(inp, cached, out):
    usage = {"input_tokens": inp, "cached_input_tokens": cached, "output_tokens": out, "reasoning_output_tokens": 0}
    return {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": usage}}}


CODEX_TURN = {"type": "event_msg", "payload": {"type": "task_started"}}
CODEX_META = {"type": "session_meta", "payload": {"cwd": "/w/codex"}}


class SpendTest(TempCase):
    def setUp(self):
        super().setUp()
        self.home = self.tmp / "home"
        self.state = self.tmp / "state"
        patcher = mock.patch.dict(os.environ, {"DEVGUARD_AGENT_HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def log(self, rel, entries):
        path = self.home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as handle:
            for e in entries:
                handle.write(json.dumps(e) + "\n")
        return path

    def scan(self, minutes=30):
        return {s.path: s for s in spend.scan(self.state, minutes)}

    def test_claude_turn_counts_fresh_input_cache_writes_and_output_once_per_message(self):
        path = self.log(".claude/projects/-w-app/s.jsonl", [
            claude_prompt(), claude_reply("m1"), claude_reply("m1"), claude_tool_result(), claude_reply("m2"),
        ])  # fmt: skip
        s = self.scan()[str(path)]
        self.assertEqual((s.turn, s.total, s.calls, s.cwd), (320, 320, 2, "/w/app"))

    def test_a_new_prompt_starts_a_new_turn_and_logs_are_read_incrementally(self):
        path = self.log(".claude/projects/-w-app/s.jsonl", [claude_prompt(), claude_reply("m1")])
        self.assertEqual(self.scan()[str(path)].turn, 160)
        offset = self.scan()[str(path)].offset
        self.log(".claude/projects/-w-app/s.jsonl", [claude_prompt("again"), claude_reply("m2", fresh=1000)])
        s = self.scan()[str(path)]
        self.assertGreater(s.offset, offset)
        self.assertEqual((s.turn, s.total, s.calls), (1060, 1220, 1))

    def test_a_partial_last_line_waits_for_the_next_pass(self):
        path = self.log(".claude/projects/-w-app/s.jsonl", [claude_prompt(), claude_reply("m1")])
        with path.open("a") as handle:
            handle.write(json.dumps(claude_reply("m2"))[:30])
        self.assertEqual(self.scan()[str(path)].turn, 160)
        with path.open("a") as handle:
            handle.write(json.dumps(claude_reply("m2"))[30:] + "\n")
        self.assertEqual(self.scan()[str(path)].turn, 320)

    def test_codex_turn_is_the_growth_of_the_running_total(self):
        path = self.log(".codex/sessions/2026/11/09/rollout-1.jsonl", [
            CODEX_META, CODEX_TURN, codex_total(1000, 400, 100), codex_total(3000, 2000, 300),
            CODEX_TURN, codex_total(5000, 3500, 400),
        ])  # fmt: skip
        s = self.scan()[str(path)]
        self.assertEqual((s.agent, s.cwd, s.total, s.turn, s.calls), ("codex", "/w/codex", 1900, 600, 1))

    def test_quiet_sessions_are_left_out_and_forgotten(self):
        path = self.log(".claude/projects/-w-app/s.jsonl", [claude_prompt(), claude_reply("m1")])
        self.assertIn(str(path), self.scan())
        old = time.time() - 3600
        os.utime(path, (old, old))
        self.assertEqual(self.scan(), {})
        self.assertNotIn(str(path), json.loads((self.state / spend.STATE_FILE).read_text()))

    def test_state_keeps_numbers_only(self):
        self.log(".claude/projects/-w-app/s.jsonl", [claude_prompt("my secret plan"), claude_tool_result(), claude_reply("m1")])
        self.scan()
        text = (self.state / spend.STATE_FILE).read_text()
        self.assertNotIn("secret plan", text)
        self.assertNotIn("SECRET-OUTPUT", text)

    def test_warns_once_per_threshold_step(self):
        s = spend.Session("claude", "/x", turn=5_000_000)
        self.assertEqual(spend.to_warn([s], 5_000_000), [s])
        s.warned = s.turn
        s.turn = 9_000_000
        self.assertEqual(spend.to_warn([s], 5_000_000), [])
        s.turn = 10_000_000
        self.assertEqual(spend.to_warn([s], 5_000_000), [s])
        self.assertEqual(spend.to_warn([s], 0), [])

    def test_watch_logs_spend_once_and_reports_an_alert(self):
        self.log(".claude/projects/-w-app/s.jsonl", [claude_prompt(), claude_reply("m1", fresh=6_000_000)])
        config = Config(state_dir=str(self.state))
        lines = []
        log = mock.Mock(write=lines.append)
        alerts = watch.check_spend(config, log, time.time())
        self.assertEqual(len(lines), 1)
        self.assertIn("SPEND claude in /w/app: 6.0M tokens in 1 calls since the last prompt", lines[0])
        self.assertEqual(alerts[0]["kind"], "spend")
        self.assertEqual(alerts[0]["turn_tokens"], 6_000_060)
        watch.check_spend(config, log, time.time())
        self.assertEqual(len(lines), 1, "a second pass in the same turn does not log again")
        config.spend.enabled = False
        self.assertEqual(watch.check_spend(config, log, time.time()), [])

    def test_status_renders_a_spend_alert(self):
        doc = status.build([], MemInfo(16 * GIB, 8 * GIB, 0), Snapshot({}), os.getuid(), 0, "apply", None, {}, 3600, ["devguard"])
        doc["alerts"].append(spend.alert(spend.Session("codex", "/x", cwd="/w/a", turn=7_000_000, calls=3), 0))
        lines = [line for line in status.render(doc).splitlines() if line.startswith("Spend")]
        self.assertEqual(lines, ["Spend: codex in /w/a spent 7,000,000 tokens in 3 calls since the last prompt"])

    def test_config(self):
        config = parse(tomllib.loads("[spend]\nwarn_tokens = 2000000\nactive_minutes = 10\n"))
        self.assertEqual((config.spend.warn_tokens, config.spend.active_minutes, config.spend.enabled), (2_000_000, 10.0, True))
        with self.assertRaises(ConfigError):
            parse({"spend": {"warn_tokns": 1}})


class DescribeTest(TempCase):
    def test_tokens(self):
        self.assertEqual([spend.tokens(n) for n in (12, 4_500, 6_000_000)], ["12", "4k", "6.0M"])

    def test_describe_uses_the_log_folder_without_a_cwd(self):
        s = spend.Session("claude", str(Path("/h/.claude/projects/-w-app/s.jsonl")), turn=1, calls=1)
        self.assertIn("claude in -w-app", spend.describe(s))
