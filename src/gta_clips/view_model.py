"""Predict a clip's views from its recorded stats, using only posts that already have views.

A ridge regression is fitted from each post's clip stats (chat, stream, audio, video), its
platform and its account to the log of its views 24 hours after posting, read from the stats
snapshots. Comparing every post at the same age keeps older posts from winning just for being
older. Each post is one row, so a clip posted on YouTube and Instagram counts twice. A platform
whose posts all show zero views tells nothing about clips and is left out. Until MIN_POSTS posts
qualify there is no model and callers should pick clips at random, so early posts give a fair
sample. Ratings and hand-picked weights play no part.
"""

from dataclasses import dataclass, field
import json
import math
import time

import numpy as np

from .config import Config
from .db import connect

MIN_POSTS = 30
# Every post is compared by its views at this age.
TARGET_AGE = 24 * 3600
# If no snapshot was taken before TARGET_AGE, one taken up to this age stands in for it.
LATEST_STAND_IN = 36 * 3600
PLATFORMS = ("youtube", "instagram", "tiktok")
PLATFORM = "platform="
RIDGE = 10.0
MIN_CATEGORY_POSTS = 3
SUMMARY = "view-model.json"

FEATURE_SQL = """
SELECT c.id, c.duration, COALESCE(c.account, '') AS account, COALESCE(b.trigger_kind, 'vod') AS kind, COALESCE(b.game_name, '') AS game,
       b.messages, f.baseline_per_10s, f.unique_chatters, f.peak_messages, f.peak_unique_chatters,
       f.peak_offset_s, f.viewer_count, f.viewer_rank, f.stream_age_s, f.is_mature,
       cf.speech_seconds, cf.word_count, cf.language_probability, cf.first_word_s, cf.last_word_s,
       s.loudness_lufs, s.loudness_range_lu, s.true_peak_dbfs, s.max_momentary_lufs, s.loudest_at_s,
       s.first_sound_s, s.silence_fraction, s.scene_changes, s.chat_messages, s.chat_before, s.chat_after,
       s.chat_peak_second, s.chat_peak_at_s, s.chat_emotes, s.chat_cheers, s.chat_bits,
       s.broadcaster_type, s.broadcaster_age_days
FROM clips c
LEFT JOIN live_bursts b ON b.clip_id = c.id
LEFT JOIN burst_features f ON f.stream_id = b.stream_id AND f.anchor = b.anchor
LEFT JOIN clip_features cf ON cf.clip_id = c.id
LEFT JOIN clip_stats s ON s.clip_id = c.id
"""

NUMERIC = ("messages", "baseline_per_10s", "unique_chatters", "peak_messages", "peak_unique_chatters",
           "peak_offset_s", "viewer_rank", "is_mature", "speech_seconds", "word_count",
           "language_probability", "first_word_s", "last_word_s", "loudness_lufs", "loudness_range_lu",
           "true_peak_dbfs", "max_momentary_lufs", "loudest_at_s", "first_sound_s", "silence_fraction",
           "scene_changes", "chat_messages", "chat_before", "chat_after", "chat_peak_second",
           "chat_peak_at_s", "chat_emotes", "chat_cheers", "chat_bits", "broadcaster_age_days")


def features(row) -> dict[str, float | None]:
    """Numbers describing one clip; None where it was not recorded."""
    values = {name: row[name] for name in NUMERIC}
    messages, baseline = row["messages"], row["baseline_per_10s"]
    values["spike"] = messages / max(baseline or 0, 1) if messages is not None else None
    values["log_viewers"] = math.log1p(row["viewer_count"]) if row["viewer_count"] is not None else None
    values["stream_age_hours"] = row["stream_age_s"] / 3600 if row["stream_age_s"] is not None else None
    values["words_per_second"] = (row["word_count"] / row["duration"]
                                  if row["word_count"] is not None and row["duration"] else None)
    values["partner"] = (1.0 if row["broadcaster_type"] == "partner" else 0.0) \
        if row["broadcaster_type"] is not None else None
    values[f"kind={row['kind']}"] = 1.0
    if row["game"]:
        values[f"category={row['game']}"] = 1.0
    # Some accounts reach far more people than others; this keeps that from counting as clip quality.
    if row["account"]:
        values[f"account={row['account']}"] = 1.0
    return values


@dataclass
class Model:
    names: list[str]
    medians: np.ndarray
    means: np.ndarray
    scales: np.ndarray
    weights: np.ndarray
    bias: float
    posts: int
    fit_r2: float
    trained: float = field(default_factory=time.time)

    def _matrix(self, rows: list[dict]) -> np.ndarray:
        data = np.array([[row.get(name, _default(name)) for name in self.names] for row in rows], dtype=float)
        # A clip not yet posted has no platform: it is scored as the average of the platforms.
        fill = np.array([self.means[i] if name.startswith(PLATFORM) else self.medians[i]
                         for i, name in enumerate(self.names)])
        missing = np.isnan(data)
        data[missing] = np.take(fill, np.nonzero(missing)[1])
        return (data - self.means) / self.scales

    def predict(self, rows: list[dict]) -> np.ndarray:
        """Predicted log(1 + views 24 hours after posting)."""
        return self._matrix(rows) @ self.weights + self.bias if rows else np.array([])

    def summary(self) -> dict:
        order = np.argsort(-np.abs(self.weights))
        return {
            "trained": self.trained, "posts": self.posts, "target": "log(1 + views 24 hours after posting)",
            "fit_r2": round(self.fit_r2, 3),
            "note": "Measured on the same posts it was fitted to, so it overstates how well it predicts.",
            "strongest": [{"stat": self.names[i], "weight": round(float(self.weights[i]), 3)} for i in order[:15]],
        }


def _default(name: str):
    if name.startswith(PLATFORM):
        return None
    return 0.0 if "=" in name else None


def views_at(snapshots: list[tuple[float, int]], age: float = TARGET_AGE) -> float | None:
    """Views at ``age`` seconds after posting, from (age, views) snapshots; None if not known yet."""
    before = max((item for item in snapshots if item[0] <= age), default=None)
    after = min((item for item in snapshots if item[0] >= age), default=None)
    if after is None:
        return None
    if before is None or after[0] == before[0]:
        return float(after[1]) if after[0] <= LATEST_STAND_IN else None
    share = (age - before[0]) / (after[0] - before[0])
    return before[1] + share * (after[1] - before[1])


def training_rows(db, now: float | None = None) -> tuple[list[dict], list[float]]:
    """One row per post with its clip's features, platform and log views at TARGET_AGE."""
    snapshots: dict[tuple[str, str], list[tuple[float, int]]] = {}
    clips: dict[tuple[str, str], str] = {}
    marks = ",".join("?" * len(PLATFORMS))
    for row in db.execute(
            f"""SELECT p.platform, p.video_id, p.clip_id, s.fetched - s.published AS age, s.views
                FROM posts p JOIN post_stats s USING (platform, video_id)
                WHERE p.platform IN ({marks}) AND p.clip_id IS NOT NULL AND s.views IS NOT NULL
                  AND s.published IS NOT NULL""", PLATFORMS):
        key = (row["platform"], row["video_id"])
        snapshots.setdefault(key, []).append((row["age"], row["views"]))
        clips[key] = row["clip_id"]
    targets = {key: value for key, items in snapshots.items() if (value := views_at(items)) is not None}
    # A platform where every post has zero views says nothing about which clips are better.
    silent = {platform for platform in PLATFORMS
              if not any(value > 0 for (name, _), value in targets.items() if name == platform)}
    targets = {key: value for key, value in targets.items() if key[0] not in silent}
    if not targets:
        return [], []
    wanted = sorted({clips[key] for key in targets})
    found = clip_features(db, wanted)
    rows, values = [], []
    for key, value in sorted(targets.items()):
        if clips[key] in found:
            rows.append({**found[clips[key]], f"{PLATFORM}{key[0]}": 1.0})
            values.append(math.log1p(value))
    return rows, values


def fit(rows: list[dict], targets: list[float]) -> Model:
    counts: dict[str, int] = {}
    for row in rows:
        for name in row:
            counts[name] = counts.get(name, 0) + 1
    # Categories need a few posts before their own weight means anything.
    names = sorted(name for name, count in counts.items()
                   if "=" not in name or count >= MIN_CATEGORY_POSTS)
    data = np.array([[row.get(name, 0.0 if "=" in name else None) for name in names] for row in rows],
                    dtype=float)
    medians = np.array([np.nanmedian(column) if not np.all(np.isnan(column)) else 0.0 for column in data.T])
    missing = np.isnan(data)
    data[missing] = np.take(medians, np.nonzero(missing)[1])
    means, scales = data.mean(axis=0), data.std(axis=0)
    scales[scales == 0] = 1.0
    x = (data - means) / scales
    y = np.array(targets)
    bias = float(y.mean())
    weights = np.linalg.solve(x.T @ x + RIDGE * np.eye(len(names)), x.T @ (y - bias))
    residual = y - (x @ weights + bias)
    total = float(((y - bias) ** 2).sum())
    r2 = 1 - float((residual ** 2).sum()) / total if total else 0.0
    return Model(names, medians, means, scales, weights, bias, len(rows), r2)


def train(config: Config, now: float | None = None) -> Model | None:
    """Fit on every qualifying post, save a readable summary, or return None if there are too few."""
    now = time.time() if now is None else now
    with connect(config.paths.database) as db:
        rows, targets = training_rows(db, now)
    path = config.paths.data_dir / SUMMARY
    if len(rows) < MIN_POSTS:
        path.write_text(json.dumps({"posts": len(rows), "needed": MIN_POSTS,
                                    "note": "Clips are picked at random until enough posts have views."},
                                   indent=2), encoding="utf-8")
        return None
    model = fit(rows, targets)
    path.write_text(json.dumps(model.summary(), indent=2), encoding="utf-8")
    return model


def clip_features(db, clip_ids: list[str]) -> dict[str, dict]:
    if not clip_ids:
        return {}
    marks = ",".join("?" * len(clip_ids))
    return {row["id"]: features(row) for row in db.execute(f"{FEATURE_SQL} WHERE c.id IN ({marks})", clip_ids)}
