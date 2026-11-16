"""Shared HTTP and checkpoint handling for upload adapters."""

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from sqlite3 import Row
from typing import Callable
from urllib.parse import urlsplit
import re

import requests


class UploadError(RuntimeError):
    def __init__(self, message: str, *, retry: bool = False, delay: int = 0, held: bool = False):
        super().__init__(message)
        self.retry = retry
        self.delay = delay
        self.held = held


@dataclass(frozen=True)
class Result:
    state: str
    remote_id: str | None
    detail: str
    delay: int = 0


@dataclass
class Upload:
    clip: Row
    file: Path
    token: str
    account: dict
    checkpoint: dict
    persist: Callable[[dict], None]

    def save(self, **values):
        self.checkpoint.update(values)
        self.persist(self.checkpoint)

    @property
    def auth(self):
        return {"Authorization": f"Bearer {self.token}"}

    @property
    def caption(self):
        return f'{self.clip["title"]}\n\n{self.clip["description"]}\n\n{self.clip["hashtags"]}'.strip()


def request(method: str, url: str, **kwargs):
    try:
        return requests.request(method, url, timeout=(15, 180), allow_redirects=False, **kwargs)
    except (requests.ConnectionError, requests.Timeout) as exc:
        raise UploadError(f"Network error ({type(exc).__name__})", retry=True) from None


def verify_url(url: str, domain: str) -> str:
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.username or parsed.password or not parsed.hostname or
            not (parsed.hostname == domain or parsed.hostname.endswith(f".{domain}"))):
        raise UploadError("Unexpected upload URL from provider", held=True)
    return url


def safe_code(value) -> str:
    code = str(value)
    return code if re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", code) else "unknown"


def retry_after(response) -> int:
    value = response.headers.get("Retry-After", "")
    if value.isdecimal():
        return int(value)
    if value:
        try:
            retry = parsedate_to_datetime(value)
            if retry.tzinfo is None:
                retry = retry.replace(tzinfo=timezone.utc)
            return max(0, int((retry - datetime.now(timezone.utc)).total_seconds()))
        except (ValueError, TypeError, OverflowError):
            pass
    return 0


def check(response, *expected):
    if response.status_code in expected:
        return response
    try:
        payload = response.json()
        error = payload.get("error", {})
        code = error.get("code") or error.get("type")
        if error.get("errors"):
            code = error["errors"][0].get("reason") or code
        transient = error.get("is_transient") or code in {
            "quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded", "userRateLimitExceeded",
            "uploadLimitExceeded", "rate_limit_exceeded", "spam_risk_too_many_pending_share",
            4, 17, 32, 613,
        }
    except (ValueError, TypeError, AttributeError, KeyError, IndexError):
        code, transient = None, False
    status = response.status_code
    delay = retry_after(response)
    if code in {"quotaExceeded", "dailyLimitExceeded", "uploadLimitExceeded",
                "spam_risk_too_many_pending_share"}:
        delay = max(delay, 3600)
    raise UploadError(f"HTTP {status}" + (f" ({safe_code(code)})" if code else ""),
                      retry=bool(transient or status in (408, 425, 429) or status >= 500), delay=delay)
