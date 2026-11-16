"""Authorize publishing accounts and refresh their OAuth tokens."""

from base64 import urlsafe_b64encode
from getpass import getpass
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
import json
import os
import secrets
import time
import webbrowser

import requests

from .db import connect

GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
TIKTOK_TOKEN = "https://open.tiktokapis.com/v2/oauth/token/"
INSTAGRAM_GRAPH = "https://graph.instagram.com"


def env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Set {name} before using this platform")
    return value


def _token_request(url: str, fields: dict, method: str = "POST") -> dict:
    response = requests.request(
        method, url, **({"params": fields} if method == "GET" else {"data": fields}), timeout=30
    )
    if not response.ok:
        raise RuntimeError(f"OAuth token request failed (HTTP {response.status_code})")
    result = response.json()
    if isinstance(result.get("data"), list) and result["data"]:
        result = result["data"][0]
    if not result.get("access_token"):
        raise RuntimeError("OAuth provider did not return an access token")
    return result


def _save_token(path: Path, platform: str, payload: dict, previous: dict | None = None) -> dict:
    token = {**(previous or {}), **payload}
    token["expires_at"] = time.time() + int(payload.get("expires_in", 3600))
    with connect(path) as db:
        db.execute(
            """INSERT INTO oauth(platform, token) VALUES (?, ?)
               ON CONFLICT(platform) DO UPDATE SET token=excluded.token""",
            (platform, json.dumps(token)),
        )
    return token


def _verify_callback(callback: str, redirect: str, state: str) -> dict:
    url = urlsplit(callback)
    expected = urlsplit(redirect)
    if (url.scheme, url.netloc, url.path) != (expected.scheme, expected.netloc, expected.path):
        raise ValueError("Callback URL does not match the registered redirect URI")
    parameters = parse_qs(url.query)
    if parameters.get("state") != [state]:
        raise ValueError("The OAuth state did not match")
    if not parameters.get("code"):
        raise ValueError("Authorization did not return a code")
    return parameters


def _google_authorize(state: str) -> dict:
    verifier = secrets.token_urlsafe(64)
    challenge = urlsafe_b64encode(sha256(verifier.encode()).digest()).decode().rstrip("=")
    client_id = env("YOUTUBE_CLIENT_ID")
    secret = env("YOUTUBE_CLIENT_SECRET")
    expected_redirect = ""
    received = {}

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            try:
                received.update(_verify_callback(
                    f"http://127.0.0.1:{self.server.server_port}{self.path}",
                    expected_redirect, state,
                ))
            except ValueError:
                self.send_error(400, "OAuth callback rejected")
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Authorization received. You can close this page.")

    with HTTPServer(("127.0.0.1", 0), Callback) as server:
        expected_redirect = f"http://127.0.0.1:{server.server_port}/oauth"
        query = urlencode({
            "client_id": client_id, "redirect_uri": expected_redirect, "response_type": "code",
            "scope": "https://www.googleapis.com/auth/youtube.upload", "state": state,
            "access_type": "offline", "prompt": "consent",
            "code_challenge": challenge, "code_challenge_method": "S256",
        })
        url = f"https://accounts.google.com/o/oauth2/v2/auth?{query}"
        print(f"Open this URL to authorize YouTube:\n{url}", flush=True)
        webbrowser.open(url)
        server.timeout = 1
        deadline = time.monotonic() + 180
        while not received and time.monotonic() < deadline:
            server.handle_request()
    if not received:
        raise RuntimeError("YouTube authorization timed out")
    return _token_request(GOOGLE_TOKEN, {
        "client_id": client_id, "client_secret": secret, "redirect_uri": expected_redirect,
        "grant_type": "authorization_code", "code": received["code"][0], "code_verifier": verifier,
    })


def authorize(path: Path, platform: str):
    state = secrets.token_urlsafe(32)
    if platform == "youtube":
        token = _google_authorize(state)
    else:
        prefix = platform.upper()
        client_id = env("TIKTOK_CLIENT_KEY" if platform == "tiktok" else "INSTAGRAM_CLIENT_ID")
        secret = env(f"{prefix}_CLIENT_SECRET")
        redirect = env(f"{prefix}_REDIRECT_URI")
        destination = urlsplit(redirect)
        if destination.scheme != "https" or not destination.hostname or destination.query or destination.fragment:
            raise ValueError(f"{prefix}_REDIRECT_URI must be an HTTPS callback URL without a query or fragment")
        instagram = platform == "instagram"
        key = "client_id" if instagram else "client_key"
        host = "https://www.instagram.com/oauth/authorize" if instagram else "https://www.tiktok.com/v2/auth/authorize/"
        scope = "instagram_business_basic,instagram_business_content_publishing" if instagram else "video.upload"
        query = {key: client_id, "redirect_uri": redirect, "response_type": "code", "scope": scope, "state": state}
        if instagram:
            query["enable_fb_login"] = "0"
        print(f"Open this URL to authorize {platform}:\n{host}?{urlencode(query)}")
        callback = getpass("Paste the complete URL from the callback address bar: ").strip()
        code = _verify_callback(callback, redirect, state)["code"][0]
        token = _token_request("https://api.instagram.com/oauth/access_token" if instagram else TIKTOK_TOKEN, {
            key: client_id, "client_secret": secret, "redirect_uri": redirect,
            "grant_type": "authorization_code", "code": code,
        })
        if instagram:
            token = {**token, **_token_request(f"{INSTAGRAM_GRAPH}/access_token", {
                "grant_type": "ig_exchange_token", "client_secret": secret,
                "access_token": token["access_token"],
            }, "GET")}
    _save_token(path, platform, {**token, "authorization_id": secrets.token_hex(16)})
    print(f"Authorized {platform}. Tokens are stored in the local database.")


def access_token(path: Path, platform: str) -> tuple[str, dict]:
    with connect(path) as db:
        saved = db.execute("SELECT token FROM oauth WHERE platform=?", (platform,)).fetchone()
    if saved is None:
        raise RuntimeError(f"Run gta-clips auth {platform} first")
    token = json.loads(saved["token"])
    if token["expires_at"] <= time.time() + (86400 * 5 if platform == "instagram" else 120):
        if platform == "instagram":
            new = _token_request(f"{INSTAGRAM_GRAPH}/refresh_access_token", {
                "grant_type": "ig_refresh_token", "access_token": token["access_token"],
            }, "GET")
        else:
            tiktok = platform == "tiktok"
            prefix = "TIKTOK" if tiktok else "YOUTUBE"
            key = "client_key" if tiktok else "client_id"
            new = _token_request(TIKTOK_TOKEN if tiktok else GOOGLE_TOKEN, {
                key: env(f"{prefix}_{'CLIENT_KEY' if tiktok else 'CLIENT_ID'}"),
                "client_secret": env(f"{prefix}_CLIENT_SECRET"),
                "grant_type": "refresh_token", "refresh_token": token["refresh_token"],
            })
        token = _save_token(path, platform, new, token)
    return token["access_token"], token
