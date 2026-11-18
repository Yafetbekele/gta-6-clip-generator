"""Local CLI for previewing and approving the exact clip metadata to upload."""

from pathlib import Path
import csv
import json
import math
import os
import re
import shutil
import subprocess
import sys

from .config import PLATFORMS, Config
from .db import connect
from .metadata import attribution

VISIBILITIES = ("private", "unlisted", "public")


def rate(config: Config, clip_id: str, score: int, notes: str, watched_seconds: float | None = None):
    """Save the current rating and keep every earlier one in rating_events."""
    if type(score) is not int or not 1 <= score <= 5:
        raise ValueError("Rate clip value from 1 (poor) to 5 (excellent)")
    if watched_seconds is not None and not (math.isfinite(watched_seconds) and watched_seconds >= 0):
        raise ValueError("watched_seconds must be a nonnegative number")
    with connect(config.paths.database) as db:
        get_clip(db, clip_id)
        db.execute("""INSERT INTO clip_ratings(clip_id, score, notes) VALUES (?, ?, ?)
                    ON CONFLICT(clip_id) DO UPDATE SET score=excluded.score,
                    notes=excluded.notes, updated=unixepoch()""", (clip_id, score, notes))
        db.execute("INSERT INTO rating_events(clip_id, score, notes, watched_seconds) VALUES (?, ?, ?, ?)",
                   (clip_id, score, notes, watched_seconds))
    print(f"Saved rating {score}/5 for {clip_id}")


RATINGS_QUERY = """SELECT c.id, c.streamer, c.path, c.start, c.duration, c.created, c.state,
    c.transcript, r.score, r.notes, r.updated AS rated_at,
    (SELECT COUNT(*) FROM rating_events e WHERE e.clip_id=c.id) AS rating_count,
    (SELECT MAX(watched_seconds) FROM rating_events e WHERE e.clip_id=c.id) AS watched_seconds,
    COALESCE(b.trigger_kind, cf.source) AS source, b.anchor, b.messages, b.game_name,
    f.baseline_per_10s, f.observed_seconds, f.multiplier, f.minimum_messages,
    f.warmup_seconds, b.messages / NULLIF(f.baseline_per_10s, 0) AS spike_ratio,
    f.unique_chatters, f.peak_messages, f.peak_unique_chatters, f.peak_offset_s,
    f.viewer_count, f.viewer_rank, f.language AS stream_language, f.stream_age_s,
    f.covered_seconds, f.muted_overlap_s,
    cf.chat_score, cf.audio_score, cf.twitch_clip_score, cf.motion_seconds, cf.total_score,
    cf.speech_seconds, cf.word_count, cf.language AS speech_language, cf.language_probability
    FROM clips c LEFT JOIN clip_ratings r ON r.clip_id=c.id
    LEFT JOIN live_bursts b ON b.clip_id=c.id
    LEFT JOIN burst_features f ON f.stream_id=b.stream_id AND f.anchor=b.anchor
    LEFT JOIN clip_features cf ON cf.clip_id=c.id
    ORDER BY c.created, c.id"""


def _rating_rows(config: Config) -> list[dict]:
    with connect(config.paths.database) as db:
        return [dict(row) for row in db.execute(RATINGS_QUERY)]


def export_ratings(config: Config, destination: Path):
    """Write clips, ratings and every recorded feature as JSON, or CSV for a .csv destination."""
    rows = _rating_rows(config)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.suffix.lower() == ".csv":
        with destination.open("w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=list(rows[0]) if rows else ["id"])
            writer.writeheader()
            writer.writerows(rows)
    else:
        destination.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print(f"Exported {len(rows)} clips and ratings to {destination.resolve()}")


def _ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=values.__getitem__)
    ranks = [0.0] * len(values)
    index = 0
    while index < len(order):
        end = index
        while end + 1 < len(order) and values[order[end + 1]] == values[order[index]]:
            end += 1
        for position in range(index, end + 1):
            ranks[order[position]] = (index + end) / 2 + 1
        index = end + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float | None:
    """Rank correlation with tie handling, or None when it is undefined."""
    if len(xs) < 3:
        return None
    rx, ry = _ranks(xs), _ranks(ys)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    covariance = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    spread = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return covariance / spread if spread else None


SUMMARY_FEATURES = ("spike_ratio", "messages", "unique_chatters", "peak_messages", "viewer_count",
                    "stream_age_s", "muted_overlap_s", "chat_score", "audio_score", "twitch_clip_score",
                    "motion_seconds", "total_score", "speech_seconds", "word_count")
SMALL_SAMPLE = 30


def ratings_summary(config: Config) -> dict:
    """Describe how ratings relate to recorded features. Descriptive only; nothing is trained."""
    rated = [row for row in _rating_rows(config) if row["score"] is not None]
    summary = {"rated": len(rated), "by_source": {}, "by_streamer": {}, "correlations": {}}

    def group(key):
        groups = {}
        for row in rated:
            groups.setdefault(row[key] or "unknown", []).append(row["score"])
        return {name: {"n": len(scores), "mean": round(sum(scores) / len(scores), 2)}
                for name, scores in sorted(groups.items(), key=lambda item: -len(item[1]))}

    summary["by_source"] = group("source")
    summary["by_streamer"] = group("streamer")
    for feature in SUMMARY_FEATURES:
        pairs = [(row[feature], row["score"]) for row in rated if row[feature] is not None]
        rho = spearman([x for x, _ in pairs], [y for _, y in pairs])
        if rho is None:
            continue
        quartiles = {}
        if len(pairs) >= 8:
            ordered = sorted(pairs)
            for quarter in range(4):
                part = ordered[quarter * len(ordered) // 4:(quarter + 1) * len(ordered) // 4]
                quartiles[f"Q{quarter + 1}"] = round(sum(y for _, y in part) / len(part), 2)
        summary["correlations"][feature] = {"n": len(pairs), "spearman": round(rho, 3),
                                            "mean_score_by_quartile": quartiles}
    return summary


def print_ratings_summary(config: Config):
    summary = ratings_summary(config)
    print(f'{summary["rated"]} rated clips')
    if summary["rated"] < SMALL_SAMPLE:
        print(f"Warning: with under {SMALL_SAMPLE} ratings, these numbers are mostly noise.")
    for title, key in (("By source", "by_source"), ("By streamer", "by_streamer")):
        print(f"\n{title}")
        for name, values in list(summary[key].items())[:15]:
            print(f'  {name:<24} n={values["n"]:<4} mean={values["mean"]}')
    print("\nSpearman correlation with score (descriptive, not causal)")
    for feature, values in summary["correlations"].items():
        flag = "  (small n)" if values["n"] < SMALL_SAMPLE else ""
        quartiles = " ".join(f"{k}={v}" for k, v in values["mean_score_by_quartile"].items())
        print(f'  {feature:<18} rho={values["spearman"]:+.3f} n={values["n"]}{flag}  {quartiles}')


def get_clip(db, clip_id: str):
    clip = db.execute("SELECT * FROM clips WHERE id=?", (clip_id,)).fetchone()
    if clip is None:
        raise ValueError(f"Unknown clip {clip_id}")
    return clip


def path_for(config: Config, clip) -> Path:
    return config.paths.data_dir / clip["path"]


def list_clips(config: Config, state: str):
    with connect(config.paths.database) as db:
        if state == "all":
            rows = db.execute("SELECT * FROM clips ORDER BY created, id").fetchall()
        else:
            rows = db.execute("SELECT * FROM clips WHERE state=? ORDER BY created, id", (state,)).fetchall()
    for row in rows:
        print(f'{row["id"]}  {row["state"]:<8}  {row["duration"]:g}s  {row["title"]}')
    if not rows:
        print("No clips match.")


def show(config: Config, clip_id: str, preview: bool = False):
    with connect(config.paths.database) as db:
        clip = get_clip(db, clip_id)
        uploads = db.execute(
            "SELECT platform, state, remote_id, detail FROM uploads WHERE clip_id=? ORDER BY platform",
            (clip_id,),
        ).fetchall()
    print(f'{clip["id"]} ({clip["state"]})\n{clip["title"]}\n\n{clip["description"]}\n{clip["hashtags"]}')
    print(f'\nTranscript\n{clip["transcript"] or "(no speech)"}\n\nVideo\n{path_for(config, clip)}')
    for upload in uploads:
        suffix = " | ".join(str(value) for value in (upload["remote_id"], upload["detail"]) if value)
        print(f'{upload["platform"]}: {upload["state"]} {suffix}'.rstrip())
    if preview:
        path = path_for(config, clip)
        if not path.is_file():
            raise ValueError(f"Video file missing for {clip_id}")
        if sys.platform == "win32":
            os.startfile(path)
        else:
            subprocess.run(["open" if sys.platform == "darwin" else "xdg-open", str(path)], check=True)


def edit(config: Config, clip_id: str, title: str | None, description: str | None, hashtags: str | None):
    with connect(config.paths.database) as db:
        db.execute("BEGIN IMMEDIATE")
        clip = get_clip(db, clip_id)
        if clip["state"] != "pending":
            raise ValueError("Only pending clips can be edited")
        title = title.strip() if title is not None else clip["title"]
        if not title or len(title) > 100 or any(character in title for character in "<>\r\n"):
            raise ValueError("Use a title of 1–100 characters without angle brackets or line breaks")
        if description is None:
            description = clip["description"]
        else:
            credit = attribution(clip["streamer"])
            description = description.strip()
            if credit not in description:
                description = f"{description}\n\n{credit}".strip()
        hashtags = hashtags.strip() if hashtags is not None else clip["hashtags"]
        if any(not re.fullmatch(r"#\w+", tag) for tag in hashtags.split()):
            raise ValueError("Enter hashtags separated by spaces")
        if any(character in description for character in "<>"):
            raise ValueError("Remove angle brackets from the description")
        if len(f"{description}\n\n{hashtags}".encode()) > 5000:
            raise ValueError("The combined description and hashtags must fit within 5000 UTF-8 bytes")
        db.execute("UPDATE clips SET title=?, description=?, hashtags=? WHERE id=?",
                   (title, description, hashtags, clip_id))


def decide(
    config: Config, clip_id: str, platforms: list[str], visibility: str | None = None,
    made_for_kids: bool | None = None,
):
    selected = set(platforms)
    if selected - set(PLATFORMS):
        raise ValueError("Unknown upload destination")
    if "youtube" in selected and visibility not in VISIBILITIES:
        raise ValueError("Select YouTube visibility with --youtube-visibility")
    if "youtube" in selected and made_for_kids is None:
        raise ValueError("Select the YouTube audience with --youtube-made-for-kids or --no-youtube-made-for-kids")
    if (visibility or made_for_kids is not None) and "youtube" not in selected:
        raise ValueError("YouTube settings require the YouTube destination")
    with connect(config.paths.database) as db:
        db.execute("BEGIN IMMEDIATE")
        clip = get_clip(db, clip_id)
        if clip["state"] != "pending":
            raise ValueError("Only pending clips can be approved or rejected")
        if selected and not path_for(config, clip).is_file():
            raise ValueError("Cannot approve a missing video")
        if "instagram" in selected and len(f'{clip["title"]}\n\n{clip["description"]}\n\n{clip["hashtags"]}') > 2200:
            raise ValueError("Shorten the title, description, or hashtags before approving for Instagram")
        db.execute("UPDATE clips SET state=? WHERE id=?", ("approved" if selected else "rejected", clip_id))
        for platform in sorted(selected):
            token = db.execute("SELECT token FROM oauth WHERE platform=?", (platform,)).fetchone()
            account = json.loads(token["token"]).get("authorization_id") if token else None
            checkpoint = json.dumps({"authorization_id": account}) if account else "{}"
            youtube = platform == "youtube"
            db.execute(
                """INSERT INTO uploads(clip_id, platform, visibility, made_for_kids, checkpoint)
                   VALUES (?, ?, ?, ?, ?)""",
                (clip_id, platform, visibility if youtube else None,
                 made_for_kids if youtube else None, checkpoint),
            )
    print(f'{clip_id} {"approved for " + ", ".join(sorted(selected)) if selected else "rejected"}.')


def export_clip(config: Config, clip_id: str, destination: Path):
    with connect(config.paths.database) as db:
        clip = get_clip(db, clip_id)
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(path_for(config, clip), destination / f"{clip_id}.mp4")
    (destination / f"{clip_id}.txt").write_text(
        f'{clip["title"]}\n\n{clip["description"]}\n\n{clip["hashtags"]}\n', encoding="utf-8"
    )
    print(f"Exported {clip_id} to {destination.resolve()}")
