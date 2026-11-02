"""All inputs are invented and stay in temporary directories."""

from contextlib import redirect_stdout
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import agent_quota as quota


NOW = datetime(2030, 6, 18, 12, tzinfo=timezone.utc).timestamp()
SCRIPT = Path(quota.__file__).resolve()


class QuotaTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="quota-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.sessions = self.root / "sessions"
        self.sessions.mkdir()
        self.cache = self.root / "cache" / "claude.json"

    def cli(self, command="status", *extra, stdin=""):
        args = [command, "--claude-cache", str(self.cache)]
        if command != "statusline":
            args += ["--codex-dir", str(self.sessions)]
        args += list(extra)
        output = io.StringIO()
        with patch("agent_quota.time.time", return_value=NOW), \
                patch("sys.stdin", io.StringIO(stdin)), redirect_stdout(output):
            code = quota.main(args)
        return code, output.getvalue().strip()

    def data(self, *extra):
        code, output = self.cli("status", "--json", *extra)
        self.assertEqual(code, 0)
        return json.loads(output)

    def event(self, at=NOW - 60, five=32, week=56, five_reset=NOW + 7200,
              week_reset=NOW + 302400):
        limits = {
            "primary": {"used_percent": five, "window_minutes": 300, "resets_at": five_reset},
            "secondary": {"used_percent": week, "window_minutes": 10080, "resets_at": week_reset},
        }
        return {"timestamp": quota.iso(at), "type": "event_msg",
                "payload": {"type": "token_count", "rate_limits": limits}}

    def write_session(self, *events, name="2030/06/18/session.jsonl"):
        path = self.sessions / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
        return path

    def claude_input(self, five=24, week=43):
        return {"rate_limits": {
            "five_hour": {"used_percentage": five, "resets_at": NOW + 10800},
            "seven_day": {"used_percentage": week, "resets_at": NOW + 302400},
        }}

    def write_cache(self, at=NOW - 120, five=24, week=43):
        value = self.claude_input(five, week)
        value["observed_at"] = at
        self.cache.parent.mkdir(parents=True, exist_ok=True)
        self.cache.write_text(json.dumps(value), encoding="utf-8")

    def test_status_json_and_weekly_budget(self):
        self.write_session(self.event())
        self.write_cache()
        data = self.data()
        self.assertEqual(data["schema_version"], 1)
        codex = data["engines"]["codex"]
        self.assertEqual(codex["windows"]["five_hour"]["used_percentage"], 32)
        self.assertEqual(codex["windows"]["seven_day"]["age_seconds"], 60)
        self.assertEqual(codex["windows"]["seven_day"]["remaining_seconds"], 302400)
        self.assertEqual(codex["pace"]["status"], "ahead")
        self.assertAlmostEqual(codex["pace"]["delta_pp"], 6)
        self.assertAlmostEqual(codex["pace"]["daily_budget_percentage"], 44 / 3.5)
        self.assertEqual(data["engines"]["claude"]["pace"]["status"], "behind")

    def test_status_is_one_line_and_reports_age_and_reset(self):
        self.write_session(self.event())
        self.write_cache()
        code, output = self.cli()
        self.assertEqual(code, 0)
        self.assertNotIn("\n", output)
        self.assertIn("5h 32% (reset 2h00m, age 1m)", output)
        self.assertIn("pace +6.0pp ahead", output)
        self.assertIn("pace -7.0pp behind", output)

    def test_newest_event_across_files_beats_mtime_and_filename(self):
        older = self.write_session(self.event(at=NOW - 200, five=11), name="z.jsonl")
        self.write_session(self.event(five=32), name="a.jsonl")
        os.utime(older, (NOW + 600, NOW + 600))
        self.assertEqual(self.data()["engines"]["codex"]["windows"]["five_hour"]["used_percentage"], 32)

    def test_malformed_lines_and_unrelated_messages_are_skipped(self):
        path = self.write_session(self.event())
        with path.open("ab") as stream:
            stream.write(b'{broken JSON\n\xff\n')
            stream.write(json.dumps({"type": "response_item", "rate_limits": {"fake": 99}}).encode() + b"\n")
            stream.write(b'{"rate_limits":')
        self.assertEqual(self.data()["engines"]["codex"]["windows"]["five_hour"]["used_percentage"], 32)

    def test_reverse_lines_crosses_blocks_and_supports_no_final_newline(self):
        path = self.root / "lines"
        path.write_bytes(b"first\nsecond-long\nthird")
        self.assertEqual(list(quota.reverse_lines(path, block_size=5)),
                         [b"third", b"second-long", b"first"])

    def test_missing_data_is_unknown_not_zero(self):
        data = self.data()
        for engine in data["engines"].values():
            self.assertEqual(engine["issue"], "no_record")
            for window in engine["windows"].values():
                self.assertIsNone(window["used_percentage"])
                self.assertIsNone(window["age_seconds"])
            self.assertIsNone(engine["pace"]["delta_pp"])
        self.assertIn("unknown", self.cli()[1])

    def test_latest_null_does_not_resurrect_old_reading(self):
        newer = self.event()
        newer["payload"]["rate_limits"] = None
        self.write_session(self.event(at=NOW - 120, five=91), newer)
        self.assertIsNone(self.data()["engines"]["codex"]["windows"]["five_hour"]["used_percentage"])

    def test_changed_format_is_unknown(self):
        newer = self.event()
        newer["payload"]["rate_limits"] = {"primary": {"percent": 60}}
        self.write_session(newer)
        self.assertIsNone(self.data()["engines"]["codex"]["windows"]["five_hour"]["used_percentage"])

    def test_codex_windows_are_identified_by_duration(self):
        event = self.event()
        limits = event["payload"]["rate_limits"]
        limits["primary"], limits["secondary"] = limits["secondary"], limits["primary"]
        self.write_session(event)
        windows = self.data()["engines"]["codex"]["windows"]
        self.assertEqual(windows["five_hour"]["used_percentage"], 32)
        self.assertEqual(windows["seven_day"]["used_percentage"], 56)

    def test_duplicate_duration_is_unknown(self):
        event = self.event()
        event["payload"]["rate_limits"]["secondary"]["window_minutes"] = 300
        self.write_session(event)
        self.assertIsNone(self.data()["engines"]["codex"]["windows"]["five_hour"]["used_percentage"])

    def test_old_record_is_stale_and_does_not_produce_pace(self):
        self.write_session(self.event(at=NOW - 7 * 3600, five_reset=NOW - 1))
        week = self.data()["engines"]["codex"]["windows"]["seven_day"]
        self.assertEqual(week["status"], "stale")
        self.assertEqual(week["used_percentage"], 56)
        code, output = self.cli("check", "--engine", "codex", "--pace")
        self.assertEqual(code, 1)
        self.assertIn("stale record", output)
        self.assertNotIn("ahead", output)

    def test_stale_cutoff_is_configurable(self):
        self.write_cache(at=NOW - 7 * 3600)
        self.assertEqual(self.data("--stale-hours", "8")["engines"]["claude"]
                         ["windows"]["seven_day"]["status"], "current")

    def test_reset_passed_is_unknown_and_never_assumed_zero(self):
        self.write_session(self.event(five_reset=NOW - 1, week_reset=NOW))
        codex = self.data()["engines"]["codex"]
        for window in codex["windows"].values():
            self.assertEqual(window["status"], "expired")
            self.assertIsNone(window["used_percentage"])
            self.assertEqual(window["remaining_seconds"], 0)
        self.assertIsNone(codex["pace"]["delta_pp"])

    def test_future_observation_and_impossible_reset_are_unknown(self):
        for event in [self.event(at=NOW + 10), self.event(week_reset=NOW + 8 * 86400)]:
            with self.subTest(event=event):
                self.write_session(event)
                self.assertIsNone(self.data()["engines"]["codex"]["windows"]["seven_day"]["used_percentage"])

    def test_record_appended_during_read_uses_time_after_all_reads(self):
        self.write_session(self.event())
        self.write_cache()
        clock = [NOW]
        reverse_lines = quota.reverse_lines
        read_cache = quota.read_cache

        def append_then_read(path):
            clock[0] = NOW + 20
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(self.event(at=clock[0], week=57)) + "\n")
            yield from reverse_lines(path)
            clock[0] = NOW + 30

        def finish_cache_read(path):
            cache = read_cache(path)
            clock[0] = NOW + 60
            return cache

        output = io.StringIO()
        with patch("agent_quota.time.time", side_effect=lambda: clock[0]), \
                patch("agent_quota.reverse_lines", side_effect=append_then_read), \
                patch("agent_quota.read_cache", side_effect=finish_cache_read), \
                redirect_stdout(output):
            code = quota.main(["status", "--json", "--codex-dir", str(self.sessions),
                               "--claude-cache", str(self.cache)])
        self.assertEqual(code, 0)
        data = json.loads(output.getvalue())
        self.assertEqual(data["generated_at"], quota.iso(NOW + 60))
        codex = data["engines"]["codex"]
        week = codex["windows"]["seven_day"]
        self.assertEqual(week["status"], "current")
        self.assertEqual(week["used_percentage"], 57)
        self.assertEqual(week["age_seconds"], 40)
        self.assertEqual(week["remaining_seconds"], 302340)
        self.assertAlmostEqual(codex["pace"]["delta_pp"], 57 - 100 * (1 - 302340 / 604800))
        self.assertEqual(data["engines"]["claude"]["windows"]["seven_day"]["age_seconds"], 180)

    def test_small_clock_skew_is_current_with_zero_age(self):
        for ahead in (3, 5):
            with self.subTest(ahead=ahead):
                self.write_session(self.event(at=NOW + ahead))
                self.write_cache(at=NOW + ahead)
                data = self.data()
                self.assertEqual(data["generated_at"], quota.iso(NOW))
                for name, engine in data["engines"].items():
                    for window in engine["windows"].values():
                        self.assertEqual(window["status"], "current", name)
                        self.assertEqual(window["observed_at"], quota.iso(NOW + ahead))
                        self.assertEqual(window["age_seconds"], 0)
                    self.assertIsNotNone(engine["pace"]["delta_pp"])
                self.assertEqual(data["engines"]["codex"]["windows"]["seven_day"]["used_percentage"], 56)
                self.assertEqual(data["engines"]["claude"]["windows"]["seven_day"]["used_percentage"], 43)

    def test_invalid_event_timestamp_does_not_become_file_mtime(self):
        event = self.event()
        event["timestamp"] = "unrecognized"
        self.write_session(event)
        window = self.data()["engines"]["codex"]["windows"]["five_hour"]
        self.assertIsNone(window["age_seconds"])
        self.assertIsNone(window["used_percentage"])

    def test_unreadable_log_is_not_silently_ignored(self):
        self.write_session(self.event())
        with patch("agent_quota.reverse_lines", side_effect=PermissionError):
            codex = self.data()["engines"]["codex"]
        self.assertEqual(codex["issue"], "unreadable_session")
        self.assertIsNone(codex["windows"]["five_hour"]["used_percentage"])

    def test_check_threshold_is_inclusive_and_one_line(self):
        self.write_session(self.event(five=80, week=43))
        self.write_cache()
        code, output = self.cli("check", "--warn", "80", "--pace")
        self.assertEqual(code, 1)
        self.assertIn("5h: 80% >= 80%", output)
        self.assertNotIn("\n", output)

    def test_check_pace_is_opt_in(self):
        self.write_session(self.event())
        self.write_cache()
        self.assertEqual(self.cli("check")[0], 0)
        code, output = self.cli("check", "--pace")
        self.assertEqual(code, 1)
        self.assertIn("pace +6.0pp ahead", output)

    def test_check_healthy_exit_zero(self):
        self.write_session(self.event(week=43))
        self.write_cache()
        code, output = self.cli("check", "--pace")
        self.assertEqual(code, 0)
        self.assertTrue(output.startswith("OK "))

    def test_check_missing_and_expired_raise_attention(self):
        self.assertEqual(self.cli("check")[0], 1)
        self.write_session(self.event(five_reset=NOW - 1))
        code, output = self.cli("check", "--engine", "codex")
        self.assertEqual(code, 1)
        self.assertIn("reset passed, usage unknown", output)

    def test_on_track_does_not_trigger_pace(self):
        self.write_session(self.event(week=50))
        self.assertEqual(self.cli("check", "--engine", "codex", "--pace")[0], 0)
        self.assertEqual(self.data()["engines"]["codex"]["pace"]["status"], "on_track")

    def test_statusline_saves_only_documented_quota_fields(self):
        data = self.claude_input()
        data.update(session_id="invented-id", transcript_path="private-transcript", cwd="private-directory")
        data["rate_limits"]["five_hour"]["extra"] = "discard this"
        code, output = self.cli("statusline", stdin=json.dumps(data))
        self.assertEqual(code, 0)
        self.assertIn("Claude 5h 24%", output)
        self.assertNotIn("\n", output)
        saved = json.loads(self.cache.read_text())
        self.assertEqual(set(saved), {"observed_at", "rate_limits"})
        self.assertEqual(set(saved["rate_limits"]["five_hour"]),
                         {"used_percentage", "resets_at", "observed_at"})
        self.assertEqual(saved["observed_at"], NOW)
        self.assertNotIn("private", self.cache.read_text())
        if os.name == "posix":
            self.assertEqual(self.cache.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.cache.parent.glob(".quota-*")), [])

    def test_statusline_missing_fields_reads_cache_without_refreshing_it(self):
        self.write_cache(at=NOW - 7 * 3600)
        before = self.cache.read_bytes()
        code, output = self.cli("statusline", stdin='{"context_window":{"used_percentage":99}}')
        self.assertEqual(code, 0)
        self.assertIn("stale record", output)
        self.assertEqual(self.cache.read_bytes(), before)

    def test_statusline_partial_update_preserves_each_window_age(self):
        self.write_cache(at=NOW - 7 * 3600)
        data = self.claude_input()
        del data["rate_limits"]["seven_day"]
        self.cli("statusline", stdin=json.dumps(data))
        windows = self.data()["engines"]["claude"]["windows"]
        self.assertEqual(windows["five_hour"]["age_seconds"], 0)
        self.assertEqual(windows["seven_day"]["age_seconds"], 7 * 3600)
        self.assertEqual(windows["seven_day"]["status"], "stale")

    def test_statusline_null_window_invalidates_old_reading(self):
        self.write_cache()
        self.cli("statusline", stdin='{"rate_limits":{"five_hour":null}}')
        windows = self.data()["engines"]["claude"]["windows"]
        self.assertIsNone(windows["five_hour"]["used_percentage"])
        self.assertEqual(windows["seven_day"]["age_seconds"], 120)

    def test_partial_hook_can_recover_from_nonfinite_cached_window(self):
        self.write_cache(week=float("nan"))
        data = self.claude_input()
        del data["rate_limits"]["seven_day"]
        code, output = self.cli("statusline", stdin=json.dumps(data))
        self.assertEqual(code, 0, output)
        saved = self.cache.read_text()
        self.assertNotIn("NaN", saved)
        self.assertIsNone(json.loads(saved)["rate_limits"]["seven_day"]["used_percentage"])

    def test_deeply_broken_json_line_does_not_hide_valid_event(self):
        path = self.write_session(self.event())
        with path.open("ab") as stream:
            stream.write(b'{"rate_limits":' + b"[" * 2000 + b"0" + b"]" * 2000 + b"}\n")
        self.assertEqual(self.data()["engines"]["codex"]["windows"]["five_hour"]["used_percentage"], 32)

    def test_statusline_without_quota_does_not_create_cache(self):
        for data in ({}, {"rate_limits": None}, {"rate_limits": {}},
                     {"rate_limits": {"spend_limit": {"used_percentage": 50}}}):
            with self.subTest(data=data):
                code, output = self.cli("statusline", stdin=json.dumps(data))
                self.assertEqual(code, 0)
                self.assertIn("unknown", output)
                self.assertFalse(self.cache.exists())

    def test_bad_statusline_input_fails_without_overwriting_cache(self):
        self.write_cache()
        before = self.cache.read_bytes()
        for text in ("{broken", "[]", "null", ""):
            with self.subTest(text=text):
                code, output = self.cli("statusline", stdin=text)
                self.assertEqual(code, 2)
                self.assertIn("invalid statusline input", output)
                self.assertEqual(self.cache.read_bytes(), before)

    def test_cache_write_error_returns_two(self):
        with patch("agent_quota.os.replace", side_effect=PermissionError):
            code, output = self.cli("statusline", stdin=json.dumps(self.claude_input()))
        self.assertEqual(code, 2)
        self.assertIn("cache write failed", output)
        self.assertEqual(list(self.cache.parent.glob(".quota-*")), [])

    def test_bad_cache_and_missing_timestamp_are_unknown(self):
        self.cache.parent.mkdir(parents=True)
        for text in ("{broken", "[]", '{"rate_limits":null}', json.dumps(self.claude_input())):
            with self.subTest(text=text):
                self.cache.write_text(text)
                self.assertIsNone(self.data()["engines"]["claude"]["windows"]["seven_day"]["used_percentage"])
        self.cache.write_bytes(b"\xff")
        self.assertEqual(self.data()["engines"]["claude"]["issue"], "invalid_cache")

    def test_invalid_percentages_are_not_zero(self):
        for value in (True, -1, 101, "42", None, float("nan"), float("inf"), 10 ** 400):
            with self.subTest(value=str(value)):
                self.write_session(self.event(five=value))
                self.assertIsNone(self.data()["engines"]["codex"]["windows"]["five_hour"]["used_percentage"])

    def test_real_zero_is_preserved(self):
        self.write_session(self.event(five=0, week=0))
        self.assertEqual(self.data()["engines"]["codex"]["windows"]["five_hour"]["used_percentage"], 0)

    def test_rfc3339_cache_timestamp_and_offsets(self):
        self.write_cache(at="2030-06-18T13:58:00+02:00")
        self.assertEqual(self.data()["engines"]["claude"]["windows"]["seven_day"]["age_seconds"], 120)
        self.assertIsNone(quota.timestamp("2030-06-18T12:00:00"))
        self.assertIsNone(quota.timestamp(NOW * 1000))

    def test_engine_filter_avoids_unrequested_reads(self):
        self.write_session(self.event(week=43))
        with patch("agent_quota.read_cache", side_effect=AssertionError("unexpected read")):
            code, _ = self.cli("check", "--engine", "codex", "--pace")
        self.assertEqual(code, 0)

    def test_cli_subprocess_and_defaults_use_isolated_home(self):
        env = dict(os.environ, HOME=str(self.root), PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run([sys.executable, str(SCRIPT), "status", "--json"],
                                text=True, capture_output=True, env=env, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["engines"]["claude"]["issue"], "no_record")
        result = subprocess.run([sys.executable, str(SCRIPT), "check"],
                                text=True, capture_output=True, env=env, check=False)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertNotIn("\n", result.stdout.strip())

    def test_invalid_cli_options_exit_two(self):
        for args in (["check", "--warn", "nan"], ["check", "--warn", "101"],
                     ["status", "--stale-hours", "0"], ["status", "--stale-hours", "inf"]):
            with self.subTest(args=args), redirect_stdout(io.StringIO()), \
                    patch("sys.stderr", io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    quota.main(args)
                self.assertEqual(error.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
