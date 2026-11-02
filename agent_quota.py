#!/usr/bin/env python3
"""Local subscription quota snapshots for Codex CLI and Claude Code."""

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import time


WINDOWS = {"five_hour": 5 * 3600, "seven_day": 7 * 86400}
LABELS = {"five_hour": "5h", "seven_day": "week"}
DEFAULT_CACHE = "~/.cache/agent-quota/claude.json"
CLOCK_SKEW_SECONDS = 5


def number(value):
    """JSON booleans, strings and non-finite numbers are not measurements."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            if math.isfinite(value):
                return float(value)
        except OverflowError:
            pass
    return None


def timestamp(value):
    result = number(value)
    if result is None and isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                result = parsed.timestamp()
        except (ValueError, OverflowError, OSError):
            pass
    # Restrict to positive, representable Unix seconds, never milliseconds.
    return result if result is not None and 0 < result < 253402300800 else None


def iso(value):
    if value is None:
        return None
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


def reverse_lines(path, block_size=65536):
    """Read appended logs backwards without loading the conversation into memory."""
    with path.open("rb") as stream:
        position = stream.seek(0, os.SEEK_END)
        carry = b""
        while position:
            count = min(position, block_size)
            position -= count
            stream.seek(position)
            parts = (stream.read(count) + carry).split(b"\n")
            carry = parts[0]
            yield from reversed(parts[1:])
        if carry:
            yield carry


def latest_codex(directory):
    """Compare the last quota event in each chronologically appended session."""
    best = None
    rank = -1
    read_error = False
    try:
        for path in directory.rglob("*.jsonl"):
            try:
                modified = path.stat().st_mtime
                for line in reverse_lines(path):
                    if b'"rate_limits"' not in line:
                        continue
                    try:
                        event = json.loads(line)
                    except (ValueError, UnicodeError, RecursionError):
                        continue
                    if not isinstance(event, dict) or event.get("type") != "event_msg":
                        continue
                    payload = event.get("payload")
                    if not isinstance(payload, dict) or payload.get("type") != "token_count":
                        continue
                    if "rate_limits" not in payload:
                        continue
                    observed = timestamp(event.get("timestamp"))
                    # mtime only ranks malformed events; it never supplies record age.
                    candidate_rank = observed if observed is not None else modified
                    if candidate_rank >= rank:
                        rank = candidate_rank
                        best = (observed, payload["rate_limits"])
                    break
            except OSError:
                read_error = True
    except OSError:
        read_error = True
    # An unreadable session might contain a newer event. Do not claim freshness.
    if read_error:
        return None, {}, "unreadable_session"
    if best is None:
        return None, {}, "no_record"
    observed, limits = best
    raw = {}
    if isinstance(limits, dict):
        for slot in ("primary", "secondary"):
            window = limits.get(slot)
            if not isinstance(window, dict):
                continue
            minutes = number(window.get("window_minutes"))
            for name, seconds in WINDOWS.items():
                if minutes == seconds / 60:
                    # Duplicate durations are ambiguous, rather than two separate quotas.
                    if name in raw:
                        raw[name] = None
                    else:
                        raw[name] = {
                            "used_percentage": window.get("used_percent"),
                            "resets_at": window.get("resets_at"),
                        }
    return observed, raw, None


def read_cache(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, "no_record"
    except (OSError, ValueError, UnicodeError, RecursionError):
        return {}, "invalid_cache"
    if not isinstance(value, dict) or not isinstance(value.get("rate_limits"), dict):
        return {}, "invalid_cache"
    return value, None


def cache_windows(cache):
    """Whitelist quota fields and retain a separate observation time per window."""
    raw = {}
    for name in WINDOWS:
        value = cache.get("rate_limits", {}).get(name)
        if isinstance(value, dict):
            used = number(value.get("used_percentage"))
            raw[name] = {
                "used_percentage": used if used is not None and 0 <= used <= 100 else None,
                "resets_at": timestamp(value.get("resets_at")),
                "observed_at": timestamp(value.get("observed_at", cache.get("observed_at"))),
            }
    return raw


def save_statusline(path, data, now):
    """Missing input never rejuvenates an old cache or becomes a zero reading."""
    limits = data.get("rate_limits") if isinstance(data, dict) else None
    if not isinstance(limits, dict) or not any(name in limits for name in WINDOWS):
        return
    cache, _ = read_cache(path)
    raw = cache_windows(cache)
    for name in WINDOWS:
        if name not in limits:
            continue
        value = limits[name] if isinstance(limits[name], dict) else {}
        used = number(value.get("used_percentage"))
        raw[name] = {
            "used_percentage": used if used is not None and 0 <= used <= 100 else None,
            "resets_at": timestamp(value.get("resets_at")),
            "observed_at": now,
        }
    # This saves only the whitelist, never session IDs, prompts, paths or cost data.
    output = {"observed_at": now, "rate_limits": raw}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=".quota-", delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(output, stream, allow_nan=False, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def window_status(value, observed, seconds, now, stale_after):
    value = value if isinstance(value, dict) else {}
    seen = timestamp(value.get("observed_at", observed))
    used = number(value.get("used_percentage"))
    reset = timestamp(value.get("resets_at"))
    age = max(0, now - seen) if seen is not None and seen <= now + CLOCK_SKEW_SECONDS else None
    result = {
        "used_percentage": None,
        "resets_at": iso(reset),
        "remaining_seconds": None if reset is None else max(0, reset - now),
        "observed_at": iso(seen),
        "age_seconds": age,
        "status": "unknown",
    }
    if age is None or used is None or not 0 <= used <= 100 or reset is None:
        return result
    if reset <= now:
        result["status"] = "expired"
        return result
    if not seen <= reset <= seen + seconds:
        return result
    result["used_percentage"] = used
    result["status"] = "stale" if age > stale_after else "current"
    return result


def weekly_pace(week):
    result = {
        "status": "unknown",
        "delta_pp": None,
        "expected_used_percentage": None,
        "daily_budget_percentage": None,
        "baseline_daily_percentage": 100 / 7,
    }
    if week["status"] != "current":
        return result
    remaining = week["remaining_seconds"]
    expected = 100 * (1 - remaining / WINDOWS["seven_day"])
    delta = week["used_percentage"] - expected
    result.update(
        status="ahead" if delta > 1e-9 else "behind" if delta < -1e-9 else "on_track",
        delta_pp=0.0 if abs(delta) < 1e-9 else delta,
        expected_used_percentage=expected,
        daily_budget_percentage=(100 - week["used_percentage"]) / (remaining / 86400),
    )
    return result


def engine_status(raw, observed, issue, source, now, stale_after):
    windows = {
        name: window_status(raw.get(name), observed, seconds, now, stale_after)
        for name, seconds in WINDOWS.items()
    }
    return {"source": source, "issue": issue, "windows": windows,
            "pace": weekly_pace(windows["seven_day"])}


def snapshot(args):
    readings = {}
    stale_after = args.stale_hours * 3600
    if args.engine in ("all", "codex"):
        observed, raw, issue = latest_codex(Path(args.codex_dir).expanduser())
        readings["codex"] = (raw, observed, issue, "session_log")
    if args.engine in ("all", "claude"):
        cache, issue = read_cache(Path(args.claude_cache).expanduser())
        readings["claude"] = (cache_windows(cache), None, issue, "cache")
    # Logs may grow while reading; evaluate every window against the finish time.
    now = time.time()
    engines = {
        name: engine_status(*reading, now, stale_after)
        for name, reading in readings.items()
    }
    return {"schema_version": 1, "generated_at": iso(now), "engines": engines}


def duration(seconds):
    if seconds is None:
        return "unknown"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h{minutes:02}m"
    days, hours = divmod(hours, 24)
    return f"{days}d{hours:02}h"


def engine_line(name, engine):
    parts = []
    for key, window in engine["windows"].items():
        used = window["used_percentage"]
        text = f"{used:g}%" if used is not None else "unknown"
        details = f"reset {duration(window['remaining_seconds'])}, age {duration(window['age_seconds'])}"
        if window["status"] == "stale":
            details += ", stale record"
        elif window["status"] == "expired":
            details += ", reset passed"
        parts.append(f"{LABELS[key]} {text} ({details})")
    pace = engine["pace"]
    if pace["delta_pp"] is None:
        parts.append("pace unknown")
    else:
        label = pace["status"].replace("_", " ")
        parts.append(f"pace {pace['delta_pp']:+.1f}pp {label}")
    return f"{name.title()} " + "; ".join(parts)


def status_line(data):
    return " | ".join(engine_line(name, value) for name, value in data["engines"].items())


def check_line(data, warn, pace_enabled):
    alerts = []
    for name, engine in data["engines"].items():
        for key, window in engine["windows"].items():
            label = f"{name.title()} {LABELS[key]}"
            state = window["status"]
            if state == "stale":
                alerts.append(f"{label}: stale record (age {duration(window['age_seconds'])})")
            elif state != "current":
                reason = "reset passed, usage unknown" if state == "expired" else "usage unknown"
                alerts.append(f"{label}: {reason}")
            elif window["used_percentage"] >= warn:
                alerts.append(f"{label}: {window['used_percentage']:g}% >= {warn:g}%")
        pace = engine["pace"]
        if pace_enabled and pace["status"] == "ahead":
            alerts.append(f"{name.title()}: pace +{pace['delta_pp']:.1f}pp ahead")
    return (1, "WARN " + " | ".join(alerts)) if alerts else (0, "OK " + status_line(data))


def positive(value):
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive finite number")
    return parsed


def percentage(value):
    parsed = float(value)
    if not math.isfinite(parsed) or not 0 <= parsed <= 100:
        raise argparse.ArgumentTypeError("must be between 0 and 100")
    return parsed


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    for name in ("status", "check", "statusline"):
        command = commands.add_parser(name)
        command.add_argument("--claude-cache", default=DEFAULT_CACHE, metavar="PATH")
        command.add_argument("--stale-hours", type=positive, default=6, metavar="HOURS")
        if name != "statusline":
            command.add_argument("--codex-dir", default="~/.codex/sessions", metavar="PATH")
            command.add_argument("--engine", choices=("all", "codex", "claude"), default="all")
        if name == "status":
            command.add_argument("--json", action="store_true", help="print a versioned JSON snapshot")
        elif name == "check":
            command.add_argument("--warn", type=percentage, default=80, metavar="PERCENT")
            command.add_argument("--pace", action="store_true", help="also warn when ahead of weekly pace")
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    if args.command == "statusline":
        now = time.time()
        try:
            data = json.load(sys.stdin)
            if not isinstance(data, dict):
                raise ValueError("expected an object")
        except (ValueError, UnicodeError, RecursionError):
            print("Claude usage unknown (invalid statusline input)")
            return 2
        path = Path(args.claude_cache).expanduser()
        try:
            save_statusline(path, data, now)
        except (OSError, ValueError):
            print("Claude usage unknown (cache write failed)")
            return 2
        # Only Claude is rendered here, keeping the per-message hook inexpensive.
        cache, issue = read_cache(path)
        now = time.time()
        engine = engine_status(cache_windows(cache), None, issue, "cache", now, args.stale_hours * 3600)
        print(engine_line("claude", engine))
        return 0
    data = snapshot(args)
    if args.command == "check":
        code, line = check_line(data, args.warn, args.pace)
        print(line)
        return code
    if args.json:
        print(json.dumps(data, allow_nan=False, sort_keys=True))
    else:
        print(status_line(data))
    return 0


if __name__ == "__main__":
    sys.exit(main())
