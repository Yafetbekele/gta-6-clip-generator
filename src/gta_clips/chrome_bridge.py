"""Authenticated loopback queue for the normal-Chrome YouTube extension.

Nothing posts until the extension is installed and explicitly started. Existing
posting history, ready-folder ownership and pacing remain authoritative.
"""
import argparse
import hmac
import json
import logging
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .browser_upload import ledger, scan, exists, allowed, account_block
from .browser_schedule import posting_spacing
from .db import worker_lock, WorkerBusy

MODE = "normal_chrome_extension"
PORT = 18761
LEASE_SECONDS = 1800


class Queue:
    def __init__(self, settings):
        self.settings = Path(settings).resolve()
        config = self.config()
        self.state = (self.settings.parent / config.get("state_dir", "data/browser-upload")).resolve()
        self.state.mkdir(parents=True, exist_ok=True)
        self.path = self.state / "posts.sqlite3"
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS chrome_jobs (
                    id TEXT PRIMARY KEY, account TEXT, clip_id TEXT, digest TEXT,
                    payload TEXT, state TEXT, created REAL, expires REAL, detail TEXT);
                CREATE TABLE IF NOT EXISTS chrome_attention (
                    account TEXT PRIMARY KEY, detail TEXT, checked REAL);
                CREATE TABLE IF NOT EXISTS chrome_channel_bindings (
                    channel_id TEXT PRIMARY KEY, handle TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS chrome_test_runs (
                    id TEXT PRIMARY KEY, started REAL, active INTEGER);
                CREATE TABLE IF NOT EXISTS chrome_test_accounts (
                    run_id TEXT, account TEXT, state TEXT, detail TEXT,
                    PRIMARY KEY(run_id,account));
                CREATE TABLE IF NOT EXISTS chrome_test_jobs (
                    job_id TEXT PRIMARY KEY, run_id TEXT, account TEXT);
            ''')
            if 'label' not in {r[1] for r in db.execute('PRAGMA table_info(chrome_channel_bindings)')}:
                db.execute('ALTER TABLE chrome_channel_bindings ADD COLUMN label TEXT')

    def config(self):
        return json.loads(self.settings.read_text(encoding="utf-8-sig"))

    @contextmanager
    def db(self):
        db = ledger(self.path)
        try:
            with db:
                yield db
        finally:
            db.close()

    def status(self):
        with self.db() as db:
            active = db.execute("SELECT account,state,expires FROM chrome_jobs WHERE state IN ('reserved','uploading') AND expires>?", (time.time(),)).fetchone()
            pauses = [dict(r) for r in db.execute("SELECT * FROM chrome_attention ORDER BY account")]
            test = db.execute("SELECT id FROM chrome_test_runs WHERE active=1 ORDER BY started DESC LIMIT 1").fetchone()
            tests = [dict(r) for r in db.execute("SELECT account,state,detail FROM chrome_test_accounts WHERE run_id=? ORDER BY account", (test[0],))] if test else None
        config = self.config()
        return {"mode": config.get("youtube_transport", "legacy"),
                "active": dict(active) if active else None, "attention": pauses,
                "test_accounts": tests,
                "accounts": [{"account": a, "channel_id": t["youtube"].get("channel_id", ""),
                              "enabled": t["youtube"].get("auto_post") is True}
                             for a, t in config["accounts"].items() if "youtube" in t]}

    def next(self, now=None):
        now = time.time() if now is None else now
        config = self.config()
        if config.get("youtube_transport") != MODE:
            return {"job": None, "reason": "Replacement is not activated; legacy scheduler remains in charge"}
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            # Expired uploads stay held in the old ledger: never retry an uncertain post.
            db.execute("UPDATE chrome_jobs SET state='expired',detail='Inspect Studio before retrying' WHERE state IN ('reserved','uploading') AND expires<=?", (now,))
            db.execute("UPDATE chrome_test_accounts SET state='held',detail='Test lease expired; inspect Studio' WHERE (run_id,account) IN (SELECT t.run_id,t.account FROM chrome_test_jobs t JOIN chrome_jobs j ON j.id=t.job_id WHERE j.state='expired') AND state IN ('reserved','uploading')")
            if db.execute("SELECT 1 FROM chrome_jobs WHERE state IN ('reserved','uploading')").fetchone():
                return {"job": None, "reason": "An upload is already in progress"}
            test = db.execute("SELECT id FROM chrome_test_runs WHERE active=1 ORDER BY started DESC LIMIT 1").fetchone()
            # Least recently attempted first, rather than always starting with GTA1.
            candidates = []
            for account, targets in config["accounts"].items():
                if test and not db.execute("SELECT 1 FROM chrome_test_accounts WHERE run_id=? AND account=? AND state='pending'", (test[0],account)).fetchone():
                    continue
                target = targets.get("youtube", {})
                if target.get("auto_post") is not True or not re.fullmatch(r"UC[\w-]{22}", target.get("channel_id", "")):
                    if test:self._test_skip(db,test[0],account,"Posting disabled or channel missing")
                    continue
                if account_block(db, "youtube", account, target) or db.execute("SELECT 1 FROM chrome_attention WHERE account=?", (account,)).fetchone():
                    if test:self._test_skip(db,test[0],account,"Account needs manual attention")
                    continue
                latest = db.execute("SELECT MAX(started) FROM posts WHERE platform='youtube' AND account=?", (account,)).fetchone()[0]
                limit = target.get("daily_limit", 3)
                limit = min(3, int(limit)) if limit is not None else 3
                spacing = posting_spacing(account, "youtube", target, latest or 0)
                if allowed(db, "youtube", account, limit, spacing, now):
                    candidates.append((latest or 0, account, target))
                elif test:
                    self._test_skip(db,test[0],account,"Daily limit or posting interval; limit preserved")
            root = (self.settings.parent / config["ready_folder"]).resolve()
            for _, account, target in sorted(candidates, key=lambda r: (r[0], r[1])):
                for clip in scan(root, account):
                    if not clip.file.is_relative_to(root):
                        continue
                    if exists(db, "youtube", account, clip):
                        continue
                    payload = {"id": secrets.token_urlsafe(24), "account": account,
                               "channel_id": target["channel_id"], "clip_id": clip.clip_id,
                               "file": str(clip.file), "title": clip.title, "description": clip.description}
                    binding = db.execute("SELECT handle,label FROM chrome_channel_bindings WHERE channel_id=?", (target["channel_id"],)).fetchone()
                    payload["channel_handle"] = binding[0] if binding else None
                    payload["channel_label"] = binding[1] if binding else None
                    db.execute("INSERT INTO chrome_jobs VALUES(?,?,?,?,?,?,?,?,?)",
                               (payload["id"], account, clip.clip_id, clip.digest,
                                json.dumps(payload), "reserved", now, now + LEASE_SECONDS, ""))
                    if test:
                        db.execute("INSERT INTO chrome_test_jobs VALUES(?,?,?)",(payload["id"],test[0],account))
                        db.execute("UPDATE chrome_test_accounts SET state='reserved' WHERE run_id=? AND account=?",(test[0],account))
                    return {"job": payload}
                if test:self._test_skip(db,test[0],account,"No unposted ready clips")
            if test:
                return {"job":None,"reason":"One-per-account test finished; inspect the test results before enabling regular posting"}
        return {"job": None, "reason": "Waiting for clips, posting interval, login attention or daily quota"}

    def event(self, data, now=None):
        now = time.time() if now is None else now
        action = data.get("event")
        if action not in ("start", "heartbeat", "posted", "failed"):
            raise ValueError("Unknown event")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM chrome_jobs WHERE id=?", (data.get("job_id"),)).fetchone()
            if not row:
                raise ValueError("Unknown job")
            if row["state"] == "posted" and action == "posted":
                return {"ok": True}  # Idempotent confirmation after a lost response.
            if row["state"] not in ("reserved", "uploading") or row["expires"] <= now:
                raise ValueError("Job expired or finished; inspect Studio")
            job = json.loads(row["payload"])
            if action in ("start", "posted") and data.get("channel_id") != job["channel_id"]:
                raise ValueError("Channel identity does not match the reserved job")
            if action == "start":
                if row["state"] != "reserved":
                    raise ValueError("Upload was already started")
                # Revalidate identity, ownership, file digest, quota and mode before sending a file.
                config = self.config()
                target = config["accounts"][row["account"]]["youtube"]
                if config.get("youtube_transport") != MODE or not target.get("auto_post") or target["channel_id"] != job["channel_id"]:
                    raise ValueError("Account configuration changed")
                root = (self.settings.parent / config["ready_folder"]).resolve()
                clip = next((c for c in scan(root, row["account"]) if c.clip_id == row["clip_id"] and c.digest == row["digest"]), None)
                if clip is None or not clip.file.is_relative_to(root) or exists(db, "youtube", row["account"], clip):
                    raise ValueError("Clip changed, disappeared or is already tracked")
                latest = db.execute("SELECT MAX(started) FROM posts WHERE platform='youtube' AND account=?", (row["account"],)).fetchone()[0]
                limit = target.get("daily_limit", 3)
                if not allowed(db, "youtube", row["account"], min(3, int(limit if limit is not None else 3)), posting_spacing(row["account"], "youtube", target, latest or 0), now):
                    raise ValueError("Posting interval or quota changed")
                db.execute("INSERT INTO posts VALUES(?,?,?,?,?,?,NULL,'','')", ("youtube", row["account"], row["clip_id"], row["digest"], "held", now))
                db.execute("UPDATE chrome_jobs SET state='uploading' WHERE id=?", (row["id"],))
                db.execute("INSERT OR REPLACE INTO login_checks VALUES(?,?,?,?,?)", ("youtube", row["account"], job["channel_id"].lower(), 1, now))
            elif action == "posted":
                if row["state"] != "uploading" or not re.fullmatch(r"https://(?:www\.)?(?:youtube\.com/(?:shorts/|watch\?v=)|youtu\.be/)[\w-]+", data.get("url", "")):
                    raise ValueError("Missing confirmed publication URL")
                db.execute("UPDATE posts SET state='posted',finished=?,url=?,detail='Confirmed by normal Chrome extension' WHERE platform='youtube' AND account=? AND clip_id=?", (now, data["url"], row["account"], row["clip_id"]))
                db.execute("UPDATE chrome_jobs SET state='posted' WHERE id=?", (row["id"],))
            elif action == "failed":
                detail = str(data.get("detail", "Inspect upload tab"))[:300]
                db.execute("UPDATE chrome_jobs SET state='held',detail=? WHERE id=?", (detail, row["id"]))
                db.execute("INSERT OR REPLACE INTO chrome_attention VALUES(?,?,?)", (row["account"], detail, now))
                if row["state"] == "uploading":
                    db.execute("UPDATE posts SET finished=?,detail=? WHERE platform='youtube' AND account=? AND clip_id=?", (now, detail, row["account"], row["clip_id"]))
            db.execute("UPDATE chrome_jobs SET expires=? WHERE id=?", (now + LEASE_SECONDS, row["id"]))
            if action != 'heartbeat':
                state = {'start':'uploading','posted':'posted','failed':'held'}[action]
                db.execute("UPDATE chrome_test_accounts SET state=?,detail=? WHERE (run_id,account) IN (SELECT run_id,account FROM chrome_test_jobs WHERE job_id=?)",(state,str(data.get('detail',data.get('url','')))[:300],row['id']))
        return {"ok": True}

    @staticmethod
    def _test_skip(db,run,account,reason):
        db.execute("UPDATE chrome_test_accounts SET state='skipped',detail=? WHERE run_id=? AND account=? AND state='pending'",(reason,run,account))

    def begin_test(self):
        config=self.config()
        run=secrets.token_urlsafe(16)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM chrome_jobs WHERE state IN ('reserved','uploading') AND expires>?",(time.time(),)).fetchone():
                raise ValueError('Wait for the current upload before starting a new test')
            db.execute('UPDATE chrome_test_runs SET active=0')
            db.execute('INSERT INTO chrome_test_runs VALUES(?,?,1)',(run,time.time()))
            for account,targets in config['accounts'].items():
                if re.fullmatch(r'UC[\w-]{22}',targets.get('youtube',{}).get('channel_id','')):
                    db.execute("INSERT INTO chrome_test_accounts VALUES(?,?,'pending','')",(run,account))
        return run

    def bind(self, data):
        channel = data.get("channel_id", "")
        handle = data.get("handle", "").lstrip("@").strip()
        label = str(data.get('label','')).strip()
        configured = [t.get("youtube", {}).get("channel_id") for t in self.config()["accounts"].values()]
        if channel not in configured or not (re.fullmatch(r"[A-Za-z0-9_.-]{3,100}", handle) or (0<len(label)<=100 and not any(ord(c)<32 for c in label))):
            raise ValueError("Use a configured Studio channel and its exact public @handle")
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO chrome_channel_bindings(channel_id,handle,label) VALUES(?,?,?)", (channel, "@" + handle if handle else '',label or None))
        return {"ok": True}


def token_for(state):
    path = state / "chrome-bridge-token.txt"
    if not path.exists():
        with path.open("x", encoding="utf-8") as f:
            f.write(secrets.token_urlsafe(32))
    return path.read_text(encoding="utf-8").strip()


def handler_for(queue, token, port):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # Never write authorization headers or clip captions into server logs.

        def origin(self):
            origin = self.headers.get("Origin", "")
            host = self.headers.get("Host", "")
            return origin if re.fullmatch(r"chrome-extension://[a-p]{32}", origin) and host == f"127.0.0.1:{port}" else None

        def send(self, status, value):
            body = json.dumps(value).encode()
            self.send_response(status)
            origin = self.origin()
            if origin:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):
            if not self.origin():
                return self.send(403, {"error": "Extension origin required"})
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", self.origin())
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def authorized(self):
            # Chrome may omit Origin on an extension GET. A website Origin is
            # always rejected; bearer authentication is mandatory in all cases.
            origin = self.headers.get("Origin")
            valid_origin = origin is None or bool(re.fullmatch(r"chrome-extension://[a-p]{32}", origin))
            return (self.headers.get("Host") == f"127.0.0.1:{port}" and valid_origin
                    and hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token))

        def do_GET(self):
            if not self.authorized():
                return self.send(403, {"error": "Pair this extension first"})
            if self.path == "/status":
                return self.send(200, queue.status())
            self.send(404, {"error": "Not found"})

        def do_POST(self):
            if not self.authorized():
                return self.send(403, {"error": "Pair this extension first"})
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 <= size <= 4096:
                    raise ValueError("Request too large")
                data = json.loads(self.rfile.read(size) or b"{}")
                if not isinstance(data, dict):
                    raise ValueError("Object required")
                if self.path == "/next":
                    result = queue.next()
                elif self.path == "/event":
                    result = queue.event(data)
                elif self.path == "/bind":
                    result = queue.bind(data)
                elif self.path == "/resume" and isinstance(data.get("account"), str):
                    with queue.db() as db:
                        db.execute("DELETE FROM chrome_attention WHERE account=?", (data["account"],))
                    result = {"ok": True}
                else:
                    return self.send(404, {"error": "Not found"})
                self.send(200, result)
            except (ValueError, KeyError, TypeError, sqlite3.IntegrityError) as exc:
                self.send(409, {"error": str(exc)[:200]})
            except Exception:
                logging.exception("Chrome queue request failed")
                self.send(500, {"error": "Queue unavailable; check local logs"})
    return Handler


def activate(settings):
    queue = Queue(settings)
    # Existing scheduled upload batches must finish before changing transports.
    with worker_lock(queue.state, "browser-schedule"):
        config = queue.config()
        backup = queue.state / f"before-chrome-extension-{int(time.time())}.json"
        backup.write_text(json.dumps(config, indent=2), encoding="utf-8")
        config["youtube_transport"] = MODE
        pending = queue.settings.with_suffix(".chrome.tmp")
        pending.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        pending.replace(queue.settings)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", type=Path, default=Path("browser-upload.json"))
    parser.add_argument("--activate", action="store_true")
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()
    queue = Queue(args.settings)
    logging.basicConfig(filename=queue.state / "chrome-bridge.log", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if args.activate:
        activate(args.settings)
    with worker_lock(queue.state, "chrome-bridge"):
        token = token_for(queue.state)
        server = ThreadingHTTPServer(("127.0.0.1", args.port), handler_for(queue, token, args.port))
        logging.info("Chrome bridge listening on loopback port %s", args.port)
        try:
            server.serve_forever()
        finally:
            server.server_close()


if __name__ == "__main__":
    try:
        main()
    except WorkerBusy:
        pass  # The already-running queue owns this profile transport.
