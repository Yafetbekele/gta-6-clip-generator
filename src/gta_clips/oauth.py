"""Authorize publishing accounts and refresh their OAuth tokens."""

from base64 import urlsafe_b64encode
from collections.abc import Callable
from getpass import getpass
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
import json
import os
import re
import secrets
import time
import webbrowser

import requests

from .db import connect

GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
TIKTOK_TOKEN = "https://open.tiktokapis.com/v2/oauth/token/"
# Instagram publishing goes through Meta's Unified login (Facebook Login for Business). New Meta
# apps cannot use "API setup with Instagram login", so tokens come from the app dashboard instead.
INSTAGRAM_GRAPH = "https://graph.facebook.com"
INSTAGRAM_DEFAULT_VERSION = "v25.0"
TWITCH_TOKEN = "https://id.twitch.tv/oauth2/token"
GOOGLE_SCOPES = ("https://www.googleapis.com/auth/youtube.upload",
                 "https://www.googleapis.com/auth/youtube.readonly",
                 "https://www.googleapis.com/auth/yt-analytics.readonly")


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


def token_key(platform: str, account: str | None = None) -> str:
    """Each account set keeps its own login per platform; no account means the original single login."""
    return f"{platform}@{account}" if account else platform


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
    if (url.scheme, url.netloc, url.path or "/") != (expected.scheme, expected.netloc, expected.path or "/"):
        raise ValueError("Callback URL does not match the registered redirect URI")
    parameters = parse_qs(url.query)
    if parameters.get("state") != [state]:
        raise ValueError("The OAuth state did not match")
    if not parameters.get("code"):
        raise ValueError("Authorization did not return a code")
    return parameters


def _loopback_code(
    label: str, state: str, build_url: Callable[[str], str], *,
    fixed_redirect: str | None = None,
) -> tuple[str, str]:
    expected_redirect = ""
    received = {}

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            try:
                received.update(_verify_callback(
                    f"http://{self.headers.get('Host', '')}{self.path}",
                    expected_redirect, state,
                ))
            except ValueError:
                self.send_error(400, "OAuth callback rejected")
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Authorization received. You can close this page.")

    port = urlsplit(fixed_redirect).port if fixed_redirect else 0
    with HTTPServer(("127.0.0.1", port or 0), Callback) as server:
        expected_redirect = fixed_redirect or f"http://127.0.0.1:{server.server_port}/oauth"
        url = build_url(expected_redirect)
        print(f"Open this URL to authorize {label}:\n{url}", flush=True)
        webbrowser.open(url)
        server.timeout = 1
        deadline = time.monotonic() + 180
        while not received and time.monotonic() < deadline:
            server.handle_request()
    if not received:
        raise RuntimeError(f"{label} authorization timed out")
    return received["code"][0], expected_redirect


def _google_authorize(state: str) -> dict:
    verifier = secrets.token_urlsafe(64)
    challenge = urlsafe_b64encode(sha256(verifier.encode()).digest()).decode().rstrip("=")
    client_id = env("YOUTUBE_CLIENT_ID")
    secret = env("YOUTUBE_CLIENT_SECRET")

    def build_url(redirect: str) -> str:
        query = urlencode({
            "client_id": client_id, "redirect_uri": redirect, "response_type": "code",
            # Read-only scopes let `gta-clips stats` read each Short's views and watch time.
            "scope": " ".join(GOOGLE_SCOPES), "state": state,
            "access_type": "offline", "prompt": "consent",
            "code_challenge": challenge, "code_challenge_method": "S256",
        })
        return f"https://accounts.google.com/o/oauth2/v2/auth?{query}"

    code, redirect = _loopback_code("YouTube", state, build_url)
    return _token_request(GOOGLE_TOKEN, {
        "client_id": client_id, "client_secret": secret, "redirect_uri": redirect,
        "grant_type": "authorization_code", "code": code, "code_verifier": verifier,
    })


def _twitch_authorize(state: str) -> dict:
    client_id = env("TWITCH_CLIENT_ID")
    secret = env("TWITCH_CLIENT_SECRET")

    def build_url(redirect: str) -> str:
        query = urlencode({"client_id": client_id, "redirect_uri": redirect, "response_type": "code",
                           "scope": "user:read:chat", "state": state})
        return f"https://id.twitch.tv/oauth2/authorize?{query}"

    code, redirect = _loopback_code("Twitch chat", state, build_url, fixed_redirect="http://localhost:3000")
    return _token_request(TWITCH_TOKEN, {
        "client_id": client_id, "client_secret": secret, "redirect_uri": redirect,
        "grant_type": "authorization_code", "code": code,
    })


def instagram_version() -> str:
    version = os.getenv("INSTAGRAM_GRAPH_VERSION") or INSTAGRAM_DEFAULT_VERSION
    if not re.fullmatch(r"v\d+\.\d+", version):
        raise ValueError("INSTAGRAM_GRAPH_VERSION must look like v25.0")
    return version


def _instagram_token(expected_handle: str | None) -> dict:
    """A long-lived token pasted from the Meta app dashboard (Unified login), checked before saving.

    The dashboard's "Generate token" button gives a token tied to one Instagram professional
    account. It is not refreshed with ig_refresh_token; when Meta stops accepting it, generate a
    new one in the dashboard and run this again.
    """
    account_id = input("Instagram account ID from the Meta app dashboard (digits): ").strip()
    if not account_id.isdecimal():
        raise ValueError("The Instagram account ID is the long number under the account name")
    secret = getpass("Paste the token from Generate token (hidden), then Enter: ").strip()
    if not secret:
        raise ValueError("No token was pasted")
    base = f"{INSTAGRAM_GRAPH}/{instagram_version()}"
    response = requests.get(f"{base}/{account_id}", params={"fields": "id,username", "access_token": secret},
                            timeout=30)
    if not response.ok:
        code = response.json().get("error", {}).get("code") if response.content else None
        raise RuntimeError(f"Meta did not accept that token for account {account_id} "
                           f"(HTTP {response.status_code}, error {code})")
    username = response.json().get("username", "")
    if expected_handle and username.casefold() != expected_handle.lstrip("@").casefold():
        raise RuntimeError(f"That token belongs to @{username}, not @{expected_handle.lstrip('@')}")
    limit = requests.get(f"{base}/{account_id}/content_publishing_limit",
                         params={"fields": "quota_usage", "access_token": secret}, timeout=30)
    if not limit.ok:
        raise RuntimeError(f"That token cannot read the publishing limit (HTTP {limit.status_code}); "
                           "check instagram_content_publish in the login configuration")
    print(f"Token works for @{username}.")
    return {"access_token": secret, "user_id": account_id, "username": username,
            "login": "unified", "expires_in": 10 * 365 * 86400}


def authorize(path: Path, platform: str, account: str | None = None, expected_handle: str | None = None):
    if account and platform == "twitch":
        raise ValueError("Twitch chat uses one login for every account set")
    state = secrets.token_urlsafe(32)
    if platform == "youtube":
        token = _google_authorize(state)
    elif platform == "twitch":
        token = _twitch_authorize(state)
    elif platform == "instagram":
        token = _instagram_token(expected_handle)
    else:
        client_id = env("TIKTOK_CLIENT_KEY")
        secret = env("TIKTOK_CLIENT_SECRET")
        redirect = env("TIKTOK_REDIRECT_URI")
        destination = urlsplit(redirect)
        if destination.scheme != "https" or not destination.hostname or destination.query or destination.fragment:
            raise ValueError("TIKTOK_REDIRECT_URI must be an HTTPS callback URL without a query or fragment")
        query = {"client_key": client_id, "redirect_uri": redirect, "response_type": "code",
                 "scope": "video.upload", "state": state}
        url = f"https://www.tiktok.com/v2/auth/authorize/?{urlencode(query)}"
        print(f"Open this URL to authorize {platform}:\n{url}")
        callback = getpass("Paste the complete URL from the callback address bar: ").strip()
        code = _verify_callback(callback, redirect, state)["code"][0]
        token = _token_request(TIKTOK_TOKEN, {
            "client_key": client_id, "client_secret": secret, "redirect_uri": redirect,
            "grant_type": "authorization_code", "code": code,
        })
    _save_token(path, token_key(platform, account), {**token, "authorization_id": secrets.token_hex(16)})
    where = f" for account set {account}" if account else ""
    print(f"Authorized {platform}{where}. Tokens are stored in the local database.")


def connected(path: Path) -> set[str]:
    """Saved login keys, such as youtube@gta6-1. Never returns token contents."""
    with connect(path) as db:
        return {row[0] for row in db.execute("SELECT platform FROM oauth")}


def access_token(path: Path, platform: str, account: str | None = None) -> tuple[str, dict]:
    key = token_key(platform, account)
    with connect(path) as db:
        saved = db.execute("SELECT token FROM oauth WHERE platform=?", (key,)).fetchone()
    if saved is None:
        option = f" --account {account}" if account else ""
        raise RuntimeError(f"Run gta-clips auth {platform}{option} first")
    token = json.loads(saved["token"])
    if platform == "instagram":
        # Unified login tokens cannot be refreshed here; Meta's error on use says when to paste a new one.
        return token["access_token"], token
    if token["expires_at"] <= time.time() + 120:
        if platform == "twitch":
            new = _token_request(TWITCH_TOKEN, {
                "client_id": env("TWITCH_CLIENT_ID"), "client_secret": env("TWITCH_CLIENT_SECRET"),
                "grant_type": "refresh_token", "refresh_token": token["refresh_token"],
            })
        else:
            tiktok = platform == "tiktok"
            prefix = "TIKTOK" if tiktok else "YOUTUBE"
            id_field = "client_key" if tiktok else "client_id"
            new = _token_request(TIKTOK_TOKEN if tiktok else GOOGLE_TOKEN, {
                id_field: env(f"{prefix}_{'CLIENT_KEY' if tiktok else 'CLIENT_ID'}"),
                "client_secret": env(f"{prefix}_CLIENT_SECRET"),
                "grant_type": "refresh_token", "refresh_token": token["refresh_token"],
            })
        token = _save_token(path, key, new, token)
    return token["access_token"], token
