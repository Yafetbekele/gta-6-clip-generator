"""Loopback-only dashboard: what the watcher sees, what it recorded, and the settings that drive it."""

from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
import json
import logging
import secrets
import subprocess
import sys
import time
import urllib.request
import webbrowser

from .config import Config, ConfigError, load_config
from .db import connect
from .live import STATUS_FILE as WATCH_STATUS, STOP_FILE as WATCH_STOP
from .runner import STATUS_FILE as RUN_STATUS, STOP_FILE as RUN_STOP, start_detached
from .settings_edit import SETTINGS, current_values, save

APP = "gta-clips-dashboard"
REVIEW_PORT = 8765
# Status files older than this mean the process that writes them is not running.
FRESH_SECONDS = 90


def _status(path: Path, now: float) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value["fresh"] = now - float(value.get("updated") or 0) < FRESH_SECONDS
    return value


def _rows(db, sql: str, *params) -> list[dict]:
    return [dict(row) for row in db.execute(sql, params)]


def overview(config: Config, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    data = config.paths.data_dir
    watcher = _status(data / WATCH_STATUS, now)
    runner = _status(data / RUN_STATUS, now)
    live = watcher if watcher and watcher["fresh"] and watcher.get("state") != "stopped" else None
    channels = live["channels"] if live else []
    midnight = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    day, week, hour_start = now - 86400, now - 7 * 86400, int(now // 3600) * 3600
    with connect(config.paths.database) as db:
        counts = db.execute(
            """SELECT
                 (SELECT COUNT(*) FROM live_bursts WHERE anchor >= ? AND trigger_kind='burst') AS bursts_today,
                 (SELECT COUNT(*) FROM live_bursts WHERE anchor >= ?) AS windows_today,
                 (SELECT COUNT(*) FROM live_bursts WHERE state='waiting') AS waiting_for_vod,
                 (SELECT COUNT(*) FROM clips c WHERE c.state='pending'
                    AND NOT EXISTS (SELECT 1 FROM clip_ratings r WHERE r.clip_id=c.id)) AS unrated,
                 (SELECT COUNT(*) FROM clips) AS clips,
                 (SELECT COUNT(*) FROM clip_ratings) AS ratings,
                 (SELECT ROUND(AVG(score), 2) FROM clip_ratings) AS average_rating""",
            (midnight, midnight),
        ).fetchone()
        viewers = _rows(db, """SELECT ts, SUM(viewers) AS viewers, COUNT(*) AS channels
                               FROM channel_snapshots WHERE watched=1 AND ts >= ?
                               GROUP BY ts ORDER BY ts""", day)
        hourly = {}
        for row in db.execute("""SELECT (anchor / 3600) * 3600 AS hour, trigger_kind, COUNT(*) AS n
                                 FROM live_bursts WHERE anchor >= ? GROUP BY hour, trigger_kind""",
                              (hour_start - 23 * 3600,)):
            hourly.setdefault(row["hour"], {})[row["trigger_kind"]] = row["n"]
        windows = [{"hour": hour, "burst": hourly.get(hour, {}).get("burst", 0),
                    "near_miss": hourly.get(hour, {}).get("near_miss", 0),
                    "control": hourly.get(hour, {}).get("control", 0)}
                   for hour in range(hour_start - 23 * 3600, hour_start + 1, 3600)]
        minute_start = int(now // 60) * 60
        per_minute = {row["minute"]: row["messages"] for row in db.execute(
            """SELECT (ts / 60) * 60 AS minute, SUM(messages) AS messages FROM chat_seconds
               WHERE ts >= ? GROUP BY minute""", (minute_start - 59 * 60,))}
        chat = [{"minute": minute, "messages": per_minute.get(minute, 0)}
                for minute in range(minute_start - 59 * 60, minute_start + 1, 60)]
        watched = {row["streamer"]: row for row in _rows(
            db, """SELECT streamer, SUM(watched) * 5 AS minutes_watched, MAX(viewers) AS peak_viewers,
                          MIN(viewer_rank) AS best_rank
                   FROM channel_snapshots WHERE ts >= ? GROUP BY streamer""", week)}
        recorded = {row["streamer"]: row for row in _rows(
            db, """SELECT b.streamer, SUM(b.trigger_kind='burst') AS bursts, COUNT(*) AS windows,
                          COUNT(b.clip_id) AS clips, COUNT(r.score) AS rated,
                          ROUND(AVG(r.score), 2) AS average_rating, MAX(b.anchor) AS last_window
                   FROM live_bursts b LEFT JOIN clip_ratings r ON r.clip_id=b.clip_id
                   WHERE b.anchor >= ? GROUP BY b.streamer""", week)}
        streamers = []
        for name in watched.keys() | recorded.keys():
            streamers.append({"streamer": name, "minutes_watched": 0, "peak_viewers": None,
                              "best_rank": None, "bursts": 0, "windows": 0, "clips": 0, "rated": 0,
                              "average_rating": None, "last_window": None,
                              **watched.get(name, {}), **recorded.get(name, {})})
        streamers.sort(key=lambda row: (-(row["windows"] or 0), -(row["minutes_watched"] or 0)))
        recent = _rows(db, """SELECT b.anchor, b.streamer, b.trigger_kind, b.messages, b.state, b.clip_id,
                                     b.detail, b.last_error, b.attempts, f.viewer_count, f.unique_chatters,
                                     ROUND(b.messages / NULLIF(f.baseline_per_10s, 0), 1) AS spike_ratio,
                                     b.account,
                                     r.score
                              FROM live_bursts b
                              LEFT JOIN burst_features f ON f.stream_id=b.stream_id AND f.anchor=b.anchor
                              LEFT JOIN clip_ratings r ON r.clip_id=b.clip_id
                              ORDER BY b.anchor DESC LIMIT 30""")
        sessions = _rows(db, """SELECT id, started, ended, exit_reason FROM watch_sessions
                                ORDER BY id DESC LIMIT 10""")
        unavailable = _rows(db, """SELECT streamer, reason, noted FROM unclippable_streams
                                   ORDER BY noted DESC LIMIT 10""")
    from .accounts import summary
    from .live import describe_feeds
    accounts = summary(config)
    for account in accounts:
        watched = [item for item in channels if item["watching"] and account["name"] in (item.get("accounts") or [])]
        account["watching_now"] = len(watched)
        account["viewers_now"] = sum(item["viewers"] or 0 for item in watched)
    return {
        "now": now, "game": describe_feeds(config), "max_channels": config.total_channels,
        "accounts": accounts,
        "schedule": config.runtime.describe(),
        "in_window": config.runtime.watching_at(datetime.fromtimestamp(now)),
        "watcher": watcher, "runner": runner, "channels": channels,
        "totals": {**dict(counts), "watching_now": sum(1 for item in channels if item["watching"]),
                   "viewers_now": sum(item["viewers"] or 0 for item in channels if item["watching"])},
        "viewers_24h": viewers, "windows_24h": windows, "chat_60m": chat,
        "streamers_7d": streamers[:30], "recent_windows": recent, "sessions": sessions,
        "unavailable": unavailable,
    }


def settings_payload(config: Config) -> dict:
    return {"values": current_values(config),
            "fields": [{"name": f"{s.table}.{s.key}", "kind": s.kind, "label": s.label, "help": s.help,
                        "min": s.minimum, "max": s.maximum, "group": s.group} for s in SETTINGS]}


def _spawn_review(config_path: Path):
    executable = Path(sys.executable)
    if sys.platform == "win32" and executable.with_name("pythonw.exe").exists():
        executable = executable.with_name("pythonw.exe")
    flags = 0x00000008 | 0x00000200 | 0x08000000 if sys.platform == "win32" else 0
    subprocess.Popen([str(executable), "-m", "gta_clips", "--config", str(config_path), "review-web",
                      "--port", str(REVIEW_PORT)], cwd=config_path.parent, creationflags=flags,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def control(config: Config, config_path: Path, action: str) -> str:
    data = config.paths.data_dir
    runner = _status(data / RUN_STATUS, time.time())
    running = bool(runner and runner["fresh"] and runner.get("state") == "running")
    if action == "start":
        if running:
            return "The schedule is already running."
        (data / RUN_STOP).unlink(missing_ok=True)
        start_detached(config_path)
        return "Starting the schedule. The watcher starts if it is inside the watch hours."
    if action == "stop":
        if not running:
            return "The schedule is not running."
        (data / RUN_STOP).write_text("stop", encoding="utf-8")
        return "Stopping. A clip that is rendering finishes first, which can take a few minutes."
    if action == "restart-watcher":
        (data / WATCH_STOP).write_text("stop", encoding="utf-8")
        return ("The watcher is stopping and will start again with the saved settings." if running else
                "The watcher is stopping. Start the schedule to run it again.")
    if action == "review":
        _spawn_review(config_path)
        return f"http://127.0.0.1:{REVIEW_PORT}"
    raise ValueError("Unknown action")


def make_server(config: Config, config_path: Path, port: int = 8766):
    token = secrets.token_urlsafe(32)
    state = {"config": config}

    def latest() -> Config:
        # Pick up hand edits to config.toml; keep the last valid settings if it has a mistake.
        try:
            state["config"] = load_config(config_path)
        except ConfigError:
            pass
        return state["config"]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, status, value, content_type="application/json"):
            data = json.dumps(value).encode() if content_type == "application/json" else value
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                             "script-src 'self' 'unsafe-inline'; img-src 'self' data:")
            self.end_headers()
            self.wfile.write(data)

        def local_host(self):
            # Exact names only, which blocks DNS-rebinding hosts.
            port = self.server.server_port
            return self.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}")

        def do_GET(self):
            if not self.local_host():
                return self.reply(403, {"error": "Local access only"})
            path = urlsplit(self.path).path
            if path == "/health":
                return self.reply(200, {"app": APP, "config": str(config_path)})
            if path == "/":
                page = Path(__file__).with_name("dashboard.html").read_text(encoding="utf-8")
                return self.reply(200, page.replace("__TOKEN__", token).encode(), "text/html; charset=utf-8")
            try:
                if path == "/api/overview":
                    return self.reply(200, overview(latest()))
                if path == "/api/settings":
                    return self.reply(200, settings_payload(latest()))
            except Exception as error:  # Show the problem in the page instead of dropping the connection.
                logging.exception("Dashboard request failed")
                return self.reply(500, {"error": f"Could not read the clip database: {error}"})
            self.reply(404, {"error": "Not found"})

        def do_POST(self):
            if not self.local_host() or not secrets.compare_digest(self.headers.get("X-Dashboard-Token", ""), token):
                return self.reply(403, {"error": "Reload the dashboard and try again"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 20000:
                    raise ValueError("Invalid request size")
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError("Invalid request")
                if self.path == "/api/settings":
                    state["config"] = save(config_path, body.get("changes"))
                    return self.reply(200, {"saved": True, **settings_payload(state["config"])})
                if self.path == "/api/control":
                    message = control(latest(), config_path, str(body.get("action")))
                    return self.reply(200, {"message": message})
            except (ConfigError, ValueError, TypeError) as error:
                return self.reply(400, {"error": str(error)})
            except OSError as error:
                return self.reply(500, {"error": f"Could not complete that: {error.strerror or error}"})
            self.reply(404, {"error": "Not found"})

    server = _Server(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server


class _Server(ThreadingHTTPServer):
    # On Windows, address reuse lets a second dashboard bind the same port, and the two then
    # split requests with different tokens. Refusing the bind makes the second launch reuse the first.
    allow_reuse_address = sys.platform != "win32"


def serve(config: Config, config_path: Path, port: int = 8766, open_browser: bool = False):
    url = f"http://127.0.0.1:{port}"
    try:
        server = make_server(config, config_path, port)
    except OSError:
        # A second launch reuses this project's running dashboard.
        with urllib.request.urlopen(url + "/health", timeout=3) as response:
            existing = json.load(response)
        if existing != {"app": APP, "config": str(config_path)}:
            raise RuntimeError("Dashboard port is occupied by a different application")
        if open_browser:
            webbrowser.open(url)
        return
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    finally:
        server.server_close()
