"""Deterministic, opt-in Studio/web uploads. No AI, API keys or session extraction."""

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import time
from contextlib import contextmanager, ExitStack
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .db import worker_lock, WorkerBusy


@contextmanager
def upload_slot(state):
    """Bound browser uploads across scheduler and manual processes."""
    with ExitStack() as stack:
        for slot in range(2):
            try:
                stack.enter_context(worker_lock(state, f'browser-upload-slot-{slot}'))
                break
            except WorkerBusy:
                continue
        else:
            raise WorkerBusy('Two uploads are already running; retry when one finishes')
        yield

PLATFORMS = ("youtube", "tiktok", "instagram")
START = {
    "youtube": "https://studio.youtube.com/",
    "tiktok": "https://www.tiktok.com/tiktokstudio/upload",
    "instagram": "https://www.instagram.com/",
}


@dataclass
class Clip:
    file: Path
    clip_id: str
    digest: str
    title: str
    description: str


def scan(root: Path, account: str) -> list[Clip]:
    """Read only explicitly delimited public metadata; never upload sign-in notes."""
    result = []
    seen = set()
    for file in sorted(root.rglob("*.mp4")):
        if file.parent.name != account:
            continue
        note = file.with_suffix(".txt").read_text(encoding="utf-8-sig")
        match = re.search(
            r"^Title:\n(.+?)\n\nDescription:\n(.*?)\n\nHashtags:\n(.*?)\n\nClip ID: ([^\n]+)\s*$",
            note.replace("\r\n", "\n"), re.M | re.S,
        )
        if not match:
            raise ValueError(f"Missing or malformed metadata: {file.name}")
        title, description, hashtags, clip_id = (s.strip() for s in match.groups())
        if not title or len(title) > 100 or any(c in title for c in "<>\n"):
            raise ValueError(f"Title must be 1–100 characters without <, > or newlines: {file.name}")
        description = f"{description}\n\n{hashtags}".strip()
        if len(description) > 5000:
            raise ValueError(f"Description exceeds 5000 characters: {file.name}")
        if not re.search(r"https://(?:www\.)?twitch\.tv/[\w]+", description):
            raise ValueError(f"Missing streamer credit link: {file.name}")
        with file.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        if (clip_id, digest) not in seen:
            result.append(Clip(file.resolve(), clip_id, digest, title, description))
            seen.add((clip_id, digest))
    return result


def ledger(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("""CREATE TABLE IF NOT EXISTS posts (
        platform TEXT, account TEXT, clip_id TEXT, digest TEXT,
        state TEXT, started REAL, finished REAL, url TEXT, detail TEXT,
        PRIMARY KEY(platform, account, clip_id), UNIQUE(platform, account, digest))""")
    return db


def exists(db, platform, account, clip):
    return db.execute("SELECT 1 FROM posts WHERE platform=? AND account=? AND (clip_id=? OR digest=?)",
                      (platform, account, clip.clip_id, clip.digest)).fetchone() is not None


def allowed(db, platform, account, daily_limit, spacing, now):
    # Rolling 24h and attempts, including uncertain outcomes, count toward limits.
    count, latest = db.execute(
        "SELECT COUNT(*), MAX(started) FROM posts WHERE platform=? AND account=? AND started>?",
        (platform, account, now - 86400),
    ).fetchone()
    return (daily_limit is None or count < daily_limit) and (latest is None or now - latest >= spacing)


def claim(db, platform, account, clip):
    with db:
        db.execute("INSERT INTO posts VALUES (?,?,?,?,?,?,NULL,'','')",
                   (platform, account, clip.clip_id, clip.digest, "held", time.time()))


def finish(db, platform, account, clip, state, url="", detail=""):
    with db:
        db.execute("UPDATE posts SET state=?,finished=?,url=?,detail=? WHERE platform=? AND account=? AND clip_id=?",
                   (state, time.time(), url, detail, platform, account, clip.clip_id))


def button(page, name):
    return page.get_by_role("button", name=name, exact=True)


def verify_account(page, platform, target):
    """Check authenticated identity from the site's own account navigation, not supplied text."""
    page.goto(START[platform], wait_until="domcontentloaded")
    if platform == "youtube":
        page.wait_for_url(re.compile(r"https://studio\.youtube\.com/channel/[^/?]+(?:[/?].*)?$"))
        actual = urlsplit(page.url).path.split("/")[2]
        if actual != target["channel_id"]:
            raise RuntimeError("YouTube channel does not match configured channel_id")
    else:
        # A missing or renamed Profile link fails closed instead of guessing an account.
        profile = page.get_by_role("link", name=re.compile(r"^Profile$", re.I))
        profile.wait_for(state="visible")
        actual = urlsplit(profile.get_attribute("href") or "").path.strip("/").lstrip("@").lower()
        if actual != target["handle"].lstrip("@").lower():
            raise RuntimeError(f"{platform} Profile link does not match configured handle")


def youtube(page, clip, publish):
    button(page, "Create").click()
    page.get_by_text("Upload videos", exact=True).last.click()
    page.locator('input[type="file"]').set_input_files(str(clip.file))
    title = page.get_by_role("textbox", name="Add a title that describes your video (type @ to mention a channel)", exact=True)
    title.fill(clip.title)
    page.get_by_role("textbox", name="Tell viewers about your video (type @ to mention a channel)", exact=True).fill(clip.description)
    page.get_by_role("radio", name="No, it's not made for kids", exact=True).check()
    page.get_by_role("tab", name="Checks", exact=True).click()
    page.get_by_text("Checks complete. No issues found.", exact=True).wait_for(timeout=600_000)
    page.get_by_role("tab", name="Visibility", exact=True).click()
    page.get_by_role("radio", name="Public" if publish else "Private", exact=True).check()
    url = page.locator('a#video-link').first.get_attribute("href") or ""
    if not re.fullmatch(r"https://(?:www\.)?(?:youtube\.com/(?:shorts/|watch\?v=)|youtu\.be/)[\w-]+", url):
        raise RuntimeError("Missing recognizable YouTube video link; leaving draft for review")
    button(page, "Publish" if publish else "Save").click()
    if publish:
        page.get_by_role("heading", name="Video published", exact=True).wait_for(timeout=120_000)
    else:
        # Private save confirmation differs across Studio releases: hold on ambiguity.
        page.get_by_text("Video saved", exact=True).wait_for(timeout=120_000)
    return url


def tiktok(page, clip, publish):
    if not publish:
        raise ValueError("TikTok supports preview or explicit publish; no automated draft mode")
    caption = f"{clip.title}\n\n{clip.description}"
    if len(caption) > 2200:
        raise ValueError("TikTok caption exceeds conservative 2200-character limit")
    page.locator('input[type="file"]').set_input_files(str(clip.file))
    page.locator('[contenteditable="true"]').fill(caption)
    # Require the public audience control to be present; never change private defaults silently.
    page.get_by_text("Everyone", exact=True).wait_for(state="visible")
    button(page, "Post").click(timeout=600_000)
    page.get_by_text("Your video has been uploaded", exact=False).wait_for(timeout=180_000)
    # TikTok may not expose the resulting URL in its success modal.
    return ""


def instagram(page, clip, publish):
    if not publish:
        raise ValueError("Instagram supports preview or explicit publish; no automated draft mode")
    caption = f"{clip.title}\n\n{clip.description}"
    if len(caption) > 2200:
        raise ValueError("Instagram caption exceeds 2200 characters")
    page.get_by_role("link", name=re.compile(r"^New post$|^Create$", re.I)).click()
    page.locator('input[type="file"]').set_input_files(str(clip.file))
    crop = button(page, "Select crop")
    crop.wait_for(state="visible")
    crop.click()
    page.get_by_text("Original", exact=True).click()
    button(page, "Next").click()
    button(page, "Next").click()
    page.get_by_role("textbox", name="Write a caption...", exact=True).fill(caption)
    button(page, "Share").click()
    page.get_by_text(re.compile(r"^Your (?:reel|post) has been shared\.?$", re.I)).wait_for(timeout=180_000)
    return ""


ADAPTERS = {"youtube": youtube, "tiktok": tiktok, "instagram": instagram}


def chrome_executable():
    for variable, suffix in (("PROGRAMFILES", "Google/Chrome/Application/chrome.exe"),
                             ("PROGRAMFILES(X86)", "Google/Chrome/Application/chrome.exe"),
                             ("LOCALAPPDATA", "Google/Chrome/Application/chrome.exe")):
        if os.environ.get(variable):
            candidate = Path(os.environ[variable]) / suffix
            if candidate.is_file():
                return candidate
    raise RuntimeError("Google Chrome is not installed in a standard location")


def chrome_profile(state, platform, account):
    return state / "chrome-profiles" / platform / account


def open_normal_chrome(state, platform, account):
    profile = chrome_profile(state, platform, account).resolve()
    profile.mkdir(parents=True, exist_ok=True)
    # Normal interactive Chrome: no debugging port, automation flags or cookie copying.
    subprocess.Popen([str(chrome_executable()), f"--user-data-dir={profile}",
                      "--new-window", START[platform]])
    print("Opened normal Chrome with a dedicated uploader profile.")
    print("Sign in and select your channel. Then CLOSE this Chrome window completely.")
    print("Choose verify-login in the launcher next. Signing in alone does not verify the uploader.")


def settings(path, account, platform, *, require_identity=True):
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", account):
        raise ValueError("Invalid account folder name")
    target = config["accounts"][account][platform]
    if not require_identity:
        return config, target
    if platform == "youtube":
        if not re.fullmatch(r"UC[\w-]{22}", target["channel_id"]):
            raise ValueError("Configure an exact YouTube channel ID")
    elif not re.fullmatch(r"[\w.]+", target["handle"].lstrip("@")):
        raise ValueError("Configure a valid handle")
    return config, target


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preview", "login", "verify-login", "run", "status", "record-posted", "release"))
    parser.add_argument("--settings", type=Path, default=Path("browser-upload.json"))
    parser.add_argument("--account", required=True)
    parser.add_argument("--platform", choices=PLATFORMS, required=True)
    parser.add_argument("--publish", action="store_true", help="Publish publicly (YouTube otherwise saves privately)")
    parser.add_argument("--max", type=int, default=1, dest="maximum")
    parser.add_argument("--clip-id")
    parser.add_argument("--url", default="")
    parser.add_argument("--confirm-not-posted", action="store_true")
    parser.add_argument("--scheduled", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        config, target = settings(args.settings, args.account, args.platform,
                                  require_identity=args.action != "login")
        base = args.settings.resolve().parent
        state = (base / config.get("state_dir", "data/browser-upload")).resolve()
        root = (base / config["ready_folder"]).resolve()
        if args.maximum < 1:
            raise ValueError("--max must be positive")
        daily = target.get("daily_limit")
        daily = int(daily) if daily is not None else None
        spacing = float(target.get("spacing_minutes", 0)) * 60
        if (daily is not None and daily < 0) or spacing < 0:
            raise ValueError("Posting limits must be nonnegative")
        with ExitStack() as locks:
            locks.enter_context(worker_lock(state, f"browser-upload-{args.platform}-{args.account}"))
            if args.action == 'run':
                locks.enter_context(upload_slot(state))
            db = ledger(state / "posts.sqlite3")
            try:
                if args.scheduled:
                    from .browser_schedule import readiness
                    reason = readiness(db, args.account, args.platform, target, time.time())
                    if reason != 'ready':
                        print(f'Skipped: {reason}')
                        return 0
                return execute(args, config, target, state, root, db, daily, spacing)
            finally:
                db.close()
    except (ValueError, KeyError, OSError, RuntimeError, sqlite3.Error) as exc:
        parser.exit(1, f"Stopped: {exc}\n")
    except Exception as exc:
        # Browser exceptions may include page text or signed-in URLs. Keep logs minimal.
        parser.exit(1, f"Browser stopped ({type(exc).__name__}). Check login, account and current site layout.\n")


def execute(args, config, target, state, root, db, daily, spacing):
    platform, account = args.platform, args.account
    if args.action == "login":
        open_normal_chrome(state, platform, account)
        return 0
    if args.action == "status":
        for row in db.execute("SELECT clip_id,state,url,detail FROM posts WHERE platform=? AND account=?", (platform, account)):
            print(json.dumps(dict(row)))
        return 0
    if args.action == "release":
        if not args.clip_id or not args.confirm_not_posted:
            raise ValueError("Check Studio/profile and remove any draft first; then supply --clip-id and --confirm-not-posted")
        with db:
            count = db.execute("DELETE FROM posts WHERE platform=? AND account=? AND clip_id=? AND state='held'",
                               (platform, account, args.clip_id)).rowcount
        print(f"Released {count} held record(s)")
        return 0
    clips = scan(root, account) if args.action != "verify-login" else []
    if args.clip_id:
        clips = [clip for clip in clips if clip.clip_id == args.clip_id]
        if not clips:
            raise ValueError("Clip ID not found in ready folder")
    if args.action == "record-posted":
        if len(clips) != 1 or not args.clip_id:
            raise ValueError("Supply --clip-id for the already-posted clip")
        if args.url and urlsplit(args.url).hostname not in {"youtube.com", "www.youtube.com", "youtu.be", "www.tiktok.com", "www.instagram.com"}:
            raise ValueError("Supply a platform video URL")
        clip = clips[0]
        if not exists(db, platform, account, clip):
            claim(db, platform, account, clip)
        finish(db, platform, account, clip, "posted", args.url, "Manually reconciled")
        return 0
    if args.action == "preview":
        for clip in clips:
            print(json.dumps({"file": str(clip.file), "clip_id": clip.clip_id, "title": clip.title,
                              "description": clip.description, "destination": target,
                              "already_tracked": exists(db, platform, account, clip)}, ensure_ascii=False))
        return 0
    if args.action == "run" and platform != "youtube" and not args.publish:
        raise ValueError("Use preview first, then run --publish for TikTok/Instagram")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError('Install with: pip install -e ".[browser]" and python -m playwright install chromium') from None
    with sync_playwright() as pw:
        profile = chrome_profile(state, platform, account)
        print("Close the dedicated login window before continuing; Chrome cannot share an open profile.", flush=True)
        context = pw.chromium.launch_persistent_context(str(profile), channel="chrome", headless=False, locale="en-US",
                                                       viewport={"width": 1440, "height": 1000})
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(30_000)
            if args.action == "verify-login":
                verify_account(page, platform, target)
                print("Verified account in automated Chrome. No files uploaded.")
                return 0
            done = 0
            for clip in clips:
                if exists(db, platform, account, clip):
                    continue
                if done >= args.maximum or not allowed(db, platform, account, daily, spacing, time.time()):
                    break
                verify_account(page, platform, target)
                # Commit BEFORE any file is sent. A crash or uncertain result is never auto-retried.
                claim(db, platform, account, clip)
                print(f"Uploading {clip.title}", flush=True)
                try:
                    url = ADAPTERS[platform](page, clip, args.publish)
                except Exception as exc:
                    finish(db, platform, account, clip, "held", detail=f"{type(exc).__name__}: inspect site before retrying")
                    print("Stopped. Upload held for manual inspection; it will not be retried automatically.")
                    return 1
                finish(db, platform, account, clip, "posted" if args.publish else "private", url)
                print(f"Confirmed {'publication' if args.publish else 'private save'}: {url or clip.clip_id}", flush=True)
                done += 1
            print(f"Completed {done} upload(s). Tracked clips and schedule-limited clips were skipped.")
            return 0
        finally:
            context.close()


if __name__ == "__main__":
    raise SystemExit(main())
