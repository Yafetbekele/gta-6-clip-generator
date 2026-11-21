"""Which clip each post is, and how it performed, through the platforms' official APIs.

Posts come from three places: the API publisher (`uploads.remote_id`), the browser uploader's
ledger, and links recorded by hand with `gta-clips posted`. Each stats fetch adds a snapshot to
``post_stats``; nothing is overwritten.
"""

from datetime import date, datetime
from pathlib import Path
import logging
import re
import sqlite3
import time

import requests

from .config import Config
from .db import connect
from .oauth import INSTAGRAM_GRAPH, access_token, instagram_version

YOUTUBE_VIDEOS = "https://www.googleapis.com/youtube/v3/videos"
YOUTUBE_ANALYTICS = "https://youtubeanalytics.googleapis.com/v2/reports"
# A post's numbers are fetched again after this long.
REFRESH_SECONDS = 6 * 3600
# Stop refreshing posts this old; their numbers have settled.
SETTLED_SECONDS = 60 * 86400

PATTERNS = {
    "youtube": re.compile(r"(?:youtube\.com/(?:shorts/|watch\?v=)|youtu\.be/)([\w-]{6,})"),
    "tiktok": re.compile(r"tiktok\.com/@[\w.]+/video/(\d+)"),
    "instagram": re.compile(r"instagram\.com/(?:[\w.]+/)?(?:reel|reels|p)/([\w-]+)"),
}


def parse_url(url: str) -> tuple[str, str] | None:
    """(platform, video id) for a post link, or None."""
    for platform, pattern in PATTERNS.items():
        match = pattern.search(url or "")
        if match:
            return platform, match.group(1)
    return None


def record(config: Config, url: str, clip_id: str, source: str = "manual") -> tuple[str, str]:
    parsed = parse_url(url)
    if parsed is None:
        raise ValueError("Give a YouTube, TikTok or Instagram post link")
    with connect(config.paths.database) as db:
        clip = db.execute("SELECT account FROM clips WHERE id=?", (clip_id,)).fetchone()
        if clip is None:
            raise ValueError(f"No clip {clip_id}; the Clip ID is in the clip's .txt note")
        db.execute("""INSERT INTO posts(platform, video_id, clip_id, account, url, source) VALUES (?, ?, ?, ?, ?, ?)
                      ON CONFLICT(platform, video_id) DO UPDATE SET clip_id=excluded.clip_id,
                      account=excluded.account, url=excluded.url""",
                   (*parsed, clip_id, clip["account"], url, source))
    return parsed


SHORTCODE = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
# Instagram media ids start with milliseconds since this moment.
INSTAGRAM_EPOCH_MS = 1314220021721
# A post and the uploader's record of finishing it must be this close to be matched.
MATCH_SECONDS = 180


def posted_at(platform: str, video_id: str) -> float | None:
    """When a TikTok or Instagram post was published, read from its id; None if unknown."""
    try:
        if platform == "tiktok" and video_id.isdecimal():
            return float(int(video_id) >> 32)
        if platform == "instagram" and video_id and all(ch in SHORTCODE for ch in video_id):
            number = 0
            for ch in video_id:
                number = number * 64 + SHORTCODE.index(ch)
            return ((number >> 23) + INSTAGRAM_EPOCH_MS) / 1000
    except ValueError:
        pass
    return None


def match_by_time(config: Config) -> int:
    """Give unmatched TikTok and Instagram posts their clip, from the uploader's finish time.

    A post is matched only when exactly one uploader record on the same platform and account
    finished within MATCH_SECONDS of the post's own timestamp, and that clip has no post there yet.
    """
    ledger = config.paths.data_dir / "browser-upload" / "posts.sqlite3"
    if not ledger.is_file():
        return 0
    source = sqlite3.connect(f"file:{ledger.as_posix()}?mode=ro", uri=True)
    try:
        finished = source.execute("""SELECT platform, account, clip_id, finished FROM posts
                                     WHERE state='posted' AND finished IS NOT NULL""").fetchall()
    finally:
        source.close()
    matched = 0
    with connect(config.paths.database) as db:
        taken = {(row[0], row[1]) for row in db.execute("SELECT platform, clip_id FROM posts WHERE clip_id IS NOT NULL")}
        known = {row[0] for row in db.execute("SELECT id FROM clips")}
        for post in db.execute("""SELECT platform, video_id, account FROM posts WHERE clip_id IS NULL
                                  AND platform IN ('tiktok', 'instagram')""").fetchall():
            when = posted_at(post["platform"], post["video_id"])
            if when is None:
                continue
            near = [row for row in finished if row[0] == post["platform"] and row[1] == post["account"]
                    and abs(row[3] - when) <= MATCH_SECONDS and (row[0], row[2]) not in taken and row[2] in known]
            if len(near) == 1:
                db.execute("UPDATE posts SET clip_id=? WHERE platform=? AND video_id=? AND clip_id IS NULL",
                           (near[0][2], post["platform"], post["video_id"]))
                taken.add((post["platform"], near[0][2]))
                matched += 1
        # The id's own time is the publish time; fill it in where the collector left it out.
        for platform, video_id in db.execute("""SELECT DISTINCT platform, video_id FROM post_stats
                                               WHERE published IS NULL AND platform IN ('tiktok', 'instagram')""").fetchall():
            when = posted_at(platform, video_id)
            if when is not None:
                db.execute("UPDATE post_stats SET published=? WHERE platform=? AND video_id=? AND published IS NULL",
                           (when, platform, video_id))
    return matched


def sync(config: Config) -> int:
    """Copy posts the publisher and the browser uploader know about into ``posts``."""
    found = []
    with connect(config.paths.database) as db:
        for row in db.execute("""SELECT u.clip_id, u.platform, u.remote_id, c.account FROM uploads u
                                 JOIN clips c ON c.id = u.clip_id WHERE u.remote_id IS NOT NULL"""):
            found.append((row["platform"], row["remote_id"], row["clip_id"], row["account"], "", "publisher"))
    ledger = config.paths.data_dir / "browser-upload" / "posts.sqlite3"
    if ledger.is_file():
        source = sqlite3.connect(f"file:{ledger.as_posix()}?mode=ro", uri=True)
        try:
            for platform, account, clip_id, url in source.execute(
                    "SELECT platform, account, clip_id, url FROM posts WHERE state='posted' AND url != ''"):
                parsed = parse_url(url)
                if parsed and parsed[0] == platform:
                    found.append((platform, parsed[1], clip_id, account, url, "browser"))
        finally:
            source.close()
    added = 0
    with connect(config.paths.database) as db:
        known = {row[0] for row in db.execute("SELECT id FROM clips")}
        for platform, video_id, clip_id, account, url, origin in found:
            added += db.execute("INSERT OR IGNORE INTO posts(platform, video_id, clip_id, account, url, source) "
                                "VALUES (?, ?, ?, ?, ?, ?)",
                                (platform, video_id, clip_id if clip_id in known else None, account, url,
                                 origin)).rowcount
    return added


def _due(db, platform: str, now: float) -> list[sqlite3.Row]:
    return db.execute(
        # Only API snapshots count as fetched: the browser collector's carry no likes or watch
        # time, so they must not hold back an API read.
        """SELECT p.video_id, p.account, MAX(CASE WHEN s.likes IS NOT NULL THEN s.fetched END) AS fetched,
                  MIN(s.published) AS published
           FROM posts p LEFT JOIN post_stats s USING (platform, video_id)
           WHERE p.platform=? GROUP BY p.video_id, p.account""", (platform,)).fetchall()


def _youtube_get(url: str, token: str, params: dict) -> dict:
    response = requests.get(url, params=params, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    if response.status_code in (401, 403):
        raise PermissionError(response.text[:300])
    response.raise_for_status()
    return response.json()


def restrictions(details: dict) -> dict:
    """What a video's ``contentDetails`` says YouTube limited: an age restriction, and countries blocked.

    A Content ID claim on music shows up as ``regionRestriction`` only when the owner blocks some
    countries; a claim that just takes the ad money leaves no trace here.
    """
    region = details.get("regionRestriction") or {}
    return {"age_restricted": int(details.get("contentRating", {}).get("ytRating") == "ytAgeRestricted"),
            # An "allowed" list blocks everywhere else; count it as blocked in all but those.
            "blocked_countries": len(region.get("blocked", [])) or (249 - len(region["allowed"])
                                                                    if "allowed" in region else 0)}


def fetch_youtube(config: Config, now: float | None = None) -> dict:
    """Views, likes and comments for every YouTube post, plus watch time where analytics allow it."""
    now = time.time() if now is None else now
    with connect(config.paths.database) as db:
        rows = [row for row in _due(db, "youtube", now)
                if (row["fetched"] or 0) < now - REFRESH_SECONDS
                and not (row["published"] and row["published"] < now - SETTLED_SECONDS)]
    result = {"fetched": 0, "missing_login": set(), "no_analytics": set()}
    by_account: dict[str, list[str]] = {}
    for row in rows:
        by_account.setdefault(row["account"] or "", []).append(row["video_id"])
    for account, videos in by_account.items():
        try:
            token, _ = access_token(config.paths.database, "youtube", account or None)
        except RuntimeError:
            result["missing_login"].add(account)
            continue
        numbers: dict[str, dict] = {}
        try:
            for index in range(0, len(videos), 50):
                data = _youtube_get(YOUTUBE_VIDEOS, token, {"part": "statistics,snippet,contentDetails",
                                                           "id": ",".join(videos[index:index + 50])})
                for item in data.get("items", []):
                    stats = item.get("statistics", {})
                    published = datetime.fromisoformat(item["snippet"]["publishedAt"].replace("Z", "+00:00"))
                    numbers[item["id"]] = {"published": published.timestamp(),
                                           "views": _int(stats.get("viewCount")),
                                           "likes": _int(stats.get("likeCount")),
                                           "comments": _int(stats.get("commentCount")),
                                           **restrictions(item.get("contentDetails", {}))}
        except PermissionError:
            # Logins made before the read-only scopes were added can only upload.
            result["missing_login"].add(account)
            continue
        try:
            report = _youtube_get(YOUTUBE_ANALYTICS, token, {
                "ids": "channel==MINE", "startDate": "2020-01-01", "endDate": date.today().isoformat(),
                "metrics": "views,averageViewDuration,averageViewPercentage,shares,subscribersGained",
                "dimensions": "video", "filters": "video==" + ",".join(numbers)}) if numbers else {}
            columns = [column["name"] for column in report.get("columnHeaders", [])]
            for values in report.get("rows", []):
                line = dict(zip(columns, values))
                entry = numbers.get(line["video"])
                if entry is not None:
                    entry.update(avg_view_seconds=line.get("averageViewDuration"),
                                 avg_view_percent=line.get("averageViewPercentage"),
                                 shares=line.get("shares"), subscribers_gained=line.get("subscribersGained"))
        except (PermissionError, requests.RequestException) as error:
            logging.info("No YouTube analytics for %s: %s", account or "default login", error)
            result["no_analytics"].add(account)
        with connect(config.paths.database) as db:
            for video_id, entry in numbers.items():
                db.execute("""INSERT OR REPLACE INTO post_stats(platform, video_id, fetched, published, views,
                                  likes, comments, shares, avg_view_seconds, avg_view_percent, subscribers_gained,
                                  age_restricted, blocked_countries)
                              VALUES ('youtube', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                           (video_id, int(now), entry["published"], entry["views"], entry["likes"],
                            entry["comments"], entry.get("shares"), entry.get("avg_view_seconds"),
                            entry.get("avg_view_percent"), entry.get("subscribers_gained"),
                            entry["age_restricted"], entry["blocked_countries"]))
                result["fetched"] += 1
    return result


# Meta keeps no insights for media posted before the account became a professional account.
PRE_CONVERSION = 2108006


def _instagram_get(url: str, token: str, params: dict) -> requests.Response:
    return requests.get(url, params=params, headers={"Authorization": f"Bearer {token}"}, timeout=30)


def fetch_instagram(config: Config, now: float | None = None) -> dict:
    """Views, reach, likes, comments, shares and saves for each Reel of every account with an API login.

    Media is matched to ``posts`` by its media id (posts made by the publisher) or by the shortcode
    in its permalink (posts recorded from links); Reels not yet known are added without a clip.
    Media from before an account turned professional has no insights, so it gets no snapshot and
    keeps the browser collector's view counts.
    """
    now = time.time() if now is None else now
    result = {"fetched": 0, "missing_login": set(), "no_insights": 0}
    try:
        base = f"{INSTAGRAM_GRAPH}/{instagram_version()}"
    except ValueError as error:
        logging.warning("Instagram stats skipped: %s", error)
        return result
    with connect(config.paths.database) as db:
        logins = [row[0].split("@", 1)[1] for row in db.execute(
            "SELECT platform FROM oauth WHERE platform LIKE 'instagram@%'")]
        due = {row["video_id"]: row for row in _due(db, "instagram", now)}
    for account in sorted(logins):
        try:
            token, login = access_token(config.paths.database, "instagram", account)
        except RuntimeError:
            result["missing_login"].add(account)
            continue
        media, url, params = [], f"{base}/{login['user_id']}/media", {
            "fields": "id,permalink,timestamp,like_count,comments_count", "limit": 50}
        for _ in range(4):
            response = _instagram_get(url, token, params)
            if response.status_code in (400, 401, 403) and not media:
                result["missing_login"].add(account)
                break
            response.raise_for_status()
            body = response.json()
            media += body.get("data", [])
            url, params = body.get("paging", {}).get("next"), {}
            if not url:
                break
        for item in media:
            parsed = parse_url(item.get("permalink", ""))
            video_id = item["id"] if item["id"] in due else (parsed[1] if parsed else item["id"])
            row = due.get(video_id)
            if row is not None and ((row["fetched"] or 0) >= now - REFRESH_SECONDS
                                    or (row["published"] and row["published"] < now - SETTLED_SECONDS)):
                continue
            insights = _instagram_get(f"{base}/{item['id']}/insights", token,
                                      {"metric": "views,reach,shares,saved"})
            if not insights.ok:
                if insights.json().get("error", {}).get("error_subcode") != PRE_CONVERSION:
                    logging.info("No Instagram insights for %s: HTTP %s", video_id, insights.status_code)
                result["no_insights"] += 1
                continue
            numbers = {entry["name"]: entry["values"][0]["value"] for entry in insights.json().get("data", [])}
            published = datetime.fromisoformat(item["timestamp"].replace("+0000", "+00:00")).timestamp()
            with connect(config.paths.database) as db:
                if row is None:
                    db.execute("INSERT OR IGNORE INTO posts(platform, video_id, account, url, source) "
                               "VALUES ('instagram', ?, ?, ?, 'instagram-api')",
                               (video_id, account, item.get("permalink", "")))
                elif item.get("permalink"):
                    # Publisher posts have no link; the permalink lets reports join them to the
                    # browser collector's row for the same Reel.
                    db.execute("UPDATE posts SET url=? WHERE platform='instagram' AND video_id=? AND COALESCE(url, '')=''",
                               (item["permalink"], video_id))
                db.execute("""INSERT OR REPLACE INTO post_stats(platform, video_id, fetched, published, views,
                                  likes, comments, shares)
                              VALUES ('instagram', ?, ?, ?, ?, ?, ?, ?)""",
                           (video_id, int(now), published, _int(numbers.get("views")),
                            _int(item.get("like_count")), _int(item.get("comments_count")),
                            _int(numbers.get("shares"))))
            result["fetched"] += 1
    return result


def _int(value) -> int | None:
    return int(value) if value not in (None, "") else None


def latest(db) -> list[sqlite3.Row]:
    """Each post's newest snapshot, with its clip."""
    return db.execute(
        """SELECT p.platform, p.video_id, p.clip_id, p.account, s.*
           FROM posts p JOIN post_stats s USING (platform, video_id)
           WHERE s.fetched = (SELECT MAX(fetched) FROM post_stats x
                              WHERE x.platform = p.platform AND x.video_id = p.video_id)""").fetchall()
