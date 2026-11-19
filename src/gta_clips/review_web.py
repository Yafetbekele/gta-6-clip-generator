"""Loopback-only video review and persistent ratings, with no upload actions."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, unquote
import json
import hashlib
import re
import secrets
import webbrowser
import urllib.request

from .config import Config
from .db import connect
from .review import rate


def make_server(config: Config, port: int = 8765):
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, status, value, content_type="application/json", headers=None):
            data = json.dumps(value).encode() if content_type == "application/json" else value
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            for name, header in (headers or {}).items():
                self.send_header(name, header)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def local_host(self):
            # Exact names only, which still blocks DNS-rebinding hosts.
            port = self.server.server_port
            return self.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}")

        def do_GET(self):
            if not self.local_host():
                return self.reply(403, {"error": "Local access only"})
            path = urlsplit(self.path).path
            if path == "/health":
                return self.reply(200, {"app": "gta-clips-review", "database": str(config.paths.database)})
            if path == "/":
                page = Path(__file__).with_name("review_web.html").read_text(encoding="utf-8")
                return self.reply(200, page.replace("__TOKEN__", token).encode(), "text/html; charset=utf-8")
            if path == "/api/clips":
                with connect(config.paths.database) as db:
                    rows = db.execute("""SELECT c.id,c.streamer,c.title,c.duration,c.created,c.path,c.account,
                        r.score,r.notes FROM clips c LEFT JOIN clip_ratings r ON r.clip_id=c.id
                        ORDER BY c.created DESC,c.id""").fetchall()
                clips = []
                for row in rows:
                    clip = dict(row)
                    relative = clip.pop('path')
                    if not (config.paths.data_dir / relative).is_file():
                        continue  # Deleted video; its rating stays in the database and exports.
                    clip['video_version'] = hashlib.sha256(relative.encode()).hexdigest()[:16]
                    try:
                        manifest = json.loads((config.paths.data_dir / relative).with_suffix('.edit.json').read_text(encoding='utf-8'))
                        clip['hook'] = manifest.get('hook')
                    except (OSError, ValueError, AttributeError):
                        clip['hook'] = None
                    clips.append(clip)
                return self.reply(200, clips)
            if path.startswith("/video/"):
                with connect(config.paths.database) as db:
                    row = db.execute("SELECT path FROM clips WHERE id=?", (unquote(path[7:]),)).fetchone()
                if not row:
                    return self.reply(404, {"error": "Unknown clip"})
                video = config.paths.data_dir / row["path"]
                if not video.is_file():
                    return self.reply(404, {"error": "Video file missing"})
                size = video.stat().st_size
                start, end = 0, size - 1
                partial = self.headers.get("Range")
                unsatisfiable = {"Content-Range": f"bytes */{size}"}
                if partial:
                    match = re.fullmatch(r"bytes=(\d*)-(\d*)", partial)
                    if not match or not any(match.groups()):
                        return self.reply(416, {"error": "Invalid range"}, headers=unsatisfiable)
                    left, right = match.groups()
                    if left:
                        start = int(left)
                        end = min(int(right), end) if right else end
                    else:
                        start = max(0, size - int(right))
                    if start > end or start >= size:
                        return self.reply(416, {"error": "Range outside video"}, headers=unsatisfiable)
                self.send_response(206 if partial else 200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(end - start + 1))
                if partial:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.end_headers()
                try:
                    with video.open("rb") as source:
                        source.seek(start)
                        remaining = end - start + 1
                        while remaining:
                            chunk = source.read(min(256 * 1024, remaining))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            remaining -= len(chunk)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass
                return
            self.reply(404, {"error": "Not found"})

        def do_POST(self):
            if not self.local_host() or not secrets.compare_digest(self.headers.get("X-Review-Token", ""), token):
                return self.reply(403, {"error": "Reload the review page and try again"})
            if self.path != "/api/rate":
                return self.reply(404, {"error": "Not found"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 20000:
                    raise ValueError("Invalid request size")
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict) or not isinstance(body.get("clip_id"), str):
                    raise ValueError("Select a clip")
                if not isinstance(body.get("notes", ""), str) or len(body.get("notes", "")) > 4000:
                    raise ValueError("Notes must be at most 4000 characters")
                watched = body.get("watched_seconds")
                if watched is not None and (type(watched) not in (int, float) or not 0 <= watched <= 86400):
                    raise ValueError("watched_seconds must be a number of seconds")
                rate(config, body["clip_id"], body.get("score"), body.get("notes", ""),
                     float(watched) if watched is not None else None)
            except (ValueError, TypeError) as error:
                return self.reply(400, {"error": str(error)})
            self.reply(200, {"saved": True})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server


def serve(config: Config, port: int = 8765, open_browser: bool = False):
    url = f"http://127.0.0.1:{port}"
    try:
        server = make_server(config, port)
    except OSError:
        # Repeated Desktop launches reuse only this project's review server.
        with urllib.request.urlopen(url + "/health", timeout=3) as response:
            existing = json.load(response)
        if existing != {"app": "gta-clips-review", "database": str(config.paths.database)}:
            raise RuntimeError("Review port is occupied by a different application")
        if open_browser:
            webbrowser.open(url)
        return
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    finally:
        server.server_close()
