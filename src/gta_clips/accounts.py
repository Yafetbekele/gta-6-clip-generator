"""Account sets: which clips belong to which group of YouTube, TikTok and Instagram accounts."""

from pathlib import Path
import logging
import time

from .config import PLATFORMS, Config
from .db import WorkerBusy, connect, worker_lock
from .live import pick_account
from .oauth import connected, token_key

UNASSIGNED = "(no account set)"


def summary(config: Config) -> list[dict]:
    """Per account set: its feed, clip counts by state and which logins are connected."""
    logins = connected(config.paths.database)
    with connect(config.paths.database) as db:
        counts = {}
        for row in db.execute("""SELECT COALESCE(account, '') AS account, state, COUNT(*) AS clips,
                                        SUM(created >= unixepoch() - 86400) AS today
                                 FROM clips GROUP BY 1, 2"""):
            entry = counts.setdefault(row["account"], {"pending": 0, "approved": 0, "rejected": 0, "today": 0})
            entry[row["state"]] = entry.get(row["state"], 0) + row["clips"]
            entry["today"] += row["today"] or 0
        waiting = dict(db.execute("""SELECT COALESCE(account, ''), COUNT(*) FROM live_bursts
                                     WHERE state='waiting' GROUP BY 1""").fetchall())
    feeds = {account: feed for feed in config.feeds for account in feed.accounts}
    rows = []
    for account in config.accounts:
        clips = counts.get(account.name, {"pending": 0, "approved": 0, "rejected": 0, "today": 0})
        rows.append({
            "name": account.name, "title": account.title, "category": account.category,
            "language": account.language, "channels": account.channels, **clips,
            "email": account.email, "destinations": dict(account.destinations),
            # Sets with the same category and language watch one shared group of channels.
            "feed_channels": feeds[account.name].channels, "shared_with": len(feeds[account.name].accounts),
            "waiting": waiting.get(account.name, 0),
            "logins": {platform: token_key(platform, account.name) in logins for platform in PLATFORMS},
        })
    if "" in counts or "" in waiting:
        clips = counts.get("", {"pending": 0, "approved": 0, "rejected": 0, "today": 0})
        rows.append({"name": "", "title": UNASSIGNED, "category": "", "language": "", "channels": 0,
                     "feed_channels": 0, "shared_with": 0,
                     **clips, "waiting": waiting.get("", 0),
                     "logins": {platform: platform in logins for platform in PLATFORMS}})
    return rows


def print_summary(config: Config):
    if not config.accounts:
        print("No account sets. Add [[accounts]] tables to config.toml (see config.example.toml).")
    for row in summary(config):
        where = f'{row["category"]} ({row["language"] or "any language"}), {row["channels"]} channels' \
            if row["category"] else "clips made before account sets"
        logins = ", ".join(f'{platform} {"connected" if ok else "not connected"}'
                           for platform, ok in row["logins"].items())
        print(f'{row["title"]}: {where}')
        print(f'    {row["pending"]} to review, {row["approved"]} approved, {row["rejected"]} rejected, '
              f'{row["waiting"]} waiting for a VOD, {row["today"]} made in the last 24 hours')
        print(f"    {logins}")


def _matching_feed(config: Config, category: str, language: str):
    for feed in config.feeds:
        if feed.accounts and feed.category.casefold() == (category or "").casefold() \
                and feed.language in ("", language or ""):
            return feed
    return None


def sort_existing(config: Config, wait_seconds: float = 300) -> dict:
    """Give earlier live windows and clips an account set, and move clip files into its folder.

    Only windows whose category and stream language match an account set are assigned. Clips with
    any upload record keep their place, so nothing queued for an earlier login changes account.
    Files move only after the database agrees, and ratings are never touched.
    """
    if not config.accounts:
        raise ValueError("Add [[accounts]] to config.toml first")
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            # The render worker writes new clips; wait for it between batches.
            with worker_lock(config.paths.data_dir, "render-live"), worker_lock(config.paths.data_dir, "publish"):
                return _sort(config)
        except WorkerBusy:
            if time.monotonic() >= deadline:
                raise
            time.sleep(5)


def _sort(config: Config) -> dict:
    result = {"windows": 0, "clips": 0, "moved": 0, "missing": 0}
    with connect(config.paths.database) as db:
        db.execute("BEGIN IMMEDIATE")
        bursts = db.execute(
            """SELECT b.stream_id, b.anchor, b.game_name, b.clip_id, f.language
               FROM live_bursts b LEFT JOIN burst_features f USING (stream_id, anchor)
               LEFT JOIN uploads u ON u.clip_id = b.clip_id
               WHERE b.account IS NULL AND u.clip_id IS NULL
               ORDER BY b.anchor""").fetchall()
        for burst in bursts:
            feed = _matching_feed(config, burst["game_name"], burst["language"])
            if feed is None:
                continue
            account = pick_account(db, config, feed.key, burst["anchor"])
            db.execute("UPDATE live_bursts SET account=? WHERE stream_id=? AND anchor=?",
                       (account, burst["stream_id"], burst["anchor"]))
            result["windows"] += 1
            if burst["clip_id"]:
                db.execute("UPDATE clips SET account=? WHERE id=? AND account IS NULL", (account, burst["clip_id"]))
                result["clips"] += 1
        moves = db.execute(
            """SELECT c.id, c.path, c.account FROM clips c LEFT JOIN uploads u ON u.clip_id = c.id
               WHERE c.account IS NOT NULL AND u.clip_id IS NULL""").fetchall()
    base = config.paths.data_dir
    for clip in moves:
        current = Path(clip["path"])
        wanted = Path("clips") / clip["account"] / current.name
        if current == wanted:
            continue
        source, target = base / current, base / wanted
        if not source.is_file():
            result["missing"] += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with connect(config.paths.database) as db:
            updated = db.execute("UPDATE clips SET path=? WHERE id=? AND path=?",
                                 (wanted.as_posix(), clip["id"], clip["path"])).rowcount
            if updated != 1:
                continue
            try:
                source.replace(target)
            except OSError:
                db.rollback()
                logging.exception("Could not move %s; it stays where it was", source.name)
                continue
        manifest = source.with_suffix(".edit.json")
        if manifest.is_file():
            manifest.replace(target.with_suffix(".edit.json"))
        result["moved"] += 1
    return result
