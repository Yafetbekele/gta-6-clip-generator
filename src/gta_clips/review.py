"""Local CLI for previewing and approving the exact clip metadata to upload."""

from pathlib import Path
import json
import os
import re
import shutil
import subprocess
import sys

from .config import PLATFORMS, Config
from .db import connect
from .metadata import attribution

VISIBILITIES = ("private", "unlisted", "public")


def rate(config: Config, clip_id: str, score: int, notes: str):
    if type(score) is not int or not 1 <= score <= 5:
        raise ValueError("Rate clip value from 1 (poor) to 5 (excellent)")
    with connect(config.paths.database) as db:
        get_clip(db, clip_id)
        db.execute("""INSERT INTO clip_ratings(clip_id, score, notes) VALUES (?, ?, ?)
                    ON CONFLICT(clip_id) DO UPDATE SET score=excluded.score,
                    notes=excluded.notes, updated=unixepoch()""", (clip_id, score, notes))
    print(f"Saved rating {score}/5 for {clip_id}")


def export_ratings(config: Config, destination: Path):
    with connect(config.paths.database) as db:
        rows = db.execute("""SELECT c.id, c.streamer, c.path, c.start, c.duration,
            c.transcript, r.score, r.notes, b.anchor, b.messages, b.game_name,
            f.baseline_per_10s, f.observed_seconds, f.multiplier, f.minimum_messages,
            f.warmup_seconds, b.messages / NULLIF(f.baseline_per_10s, 0) AS spike_ratio
            FROM clips c LEFT JOIN clip_ratings r ON r.clip_id=c.id
            LEFT JOIN live_bursts b ON b.clip_id=c.id
            LEFT JOIN burst_features f ON f.stream_id=b.stream_id AND f.anchor=b.anchor
            ORDER BY c.created, c.id""").fetchall()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps([dict(row) for row in rows], indent=2), encoding="utf-8")
    print(f"Exported {len(rows)} clips and ratings to {destination.resolve()}")


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
