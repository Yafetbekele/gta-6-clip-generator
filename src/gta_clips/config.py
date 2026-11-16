"""Load TOML settings and validate them before starting a job."""

from collections.abc import Iterable
from dataclasses import dataclass, fields
from pathlib import Path
import re
from sys import float_info
import tomllib


class ConfigError(ValueError):
    """The selected configuration is missing or invalid."""


@dataclass(frozen=True)
class PathsConfig:
    data_dir: Path

    @property
    def database(self) -> Path:
        return self.data_dir / "state.sqlite3"


@dataclass(frozen=True)
class TwitchConfig:
    game_name: str
    streamers: tuple[str, ...]
    lookback_days: int = 7
    clips_per_vod: int = 10

    @property
    def all_streamers(self) -> bool:
        return self.streamers == ("*",)


@dataclass(frozen=True)
class HighlightsConfig:
    clips_per_vod: int = 3
    duration_seconds: int = 35
    lead_in_seconds: int = 4
    chat_weight: float = 1.0
    audio_weight: float = 1.0
    twitch_clips_weight: float = 1.0


@dataclass(frozen=True)
class Crop:
    x: float
    y: float
    width: float
    height: float


@dataclass(frozen=True)
class SplitLayout:
    facecam: Crop
    gameplay: Crop


@dataclass(frozen=True)
class EditingConfig:
    layout: str
    whisper_model: str
    streamers: dict[str, SplitLayout]


@dataclass(frozen=True)
class ScheduleConfig:
    daily_limit: int = 0
    min_spacing_minutes: int = 120


@dataclass(frozen=True)
class LiveConfig:
    max_channels: int = 15
    messages_per_10_seconds: int = 15
    spike_multiplier: float = 3.0
    cooldown_seconds: int = 120


@dataclass(frozen=True)
class Config:
    paths: PathsConfig
    twitch: TwitchConfig
    highlights: HighlightsConfig
    editing: EditingConfig
    schedule: dict[str, ScheduleConfig]
    live: LiveConfig = LiveConfig()


PLATFORMS = ("youtube", "instagram", "tiktok")


def _table(value: object, path: str, allowed: Iterable[str]) -> dict:
    if not isinstance(value, dict):
        raise ConfigError(f"{path} must be a TOML table")
    unknown = sorted(value.keys() - set(allowed))
    if unknown:
        raise ConfigError(f"Unknown setting in {path}: {', '.join(unknown)}")
    return value


def _integer(value: object, path: str, minimum: int, maximum: int | None = None) -> int:
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        bounds = f"between {minimum} and {maximum}" if maximum is not None else f"at least {minimum}"
        raise ConfigError(f"{path} must be an integer {bounds}")
    return value


def _text(value: object, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{path} must be a non-empty string")
    return value.strip()


def _login(value: object, path: str) -> str:
    login = _text(value, path).lower()
    if not re.fullmatch(r"[a-z0-9_]{1,25}", login):
        raise ConfigError(f"{path} must contain Twitch logins, such as streamer_name")
    return login


def _nonnegative_number(value: object, path: str) -> float:
    if type(value) in (float, int) and 0 <= value <= float_info.max:
        return float(value)
    raise ConfigError(f"{path} must be a finite, nonnegative number")


def _crop(value: object, path: str) -> Crop:
    if not isinstance(value, list) or len(value) != 4:
        raise ConfigError(f"{path} must be [x, y, width, height]")
    crop = Crop(*(_nonnegative_number(number, path) for number in value))
    if not (crop.width > 0 and crop.height > 0
            and crop.x + crop.width <= 1 and crop.y + crop.height <= 1):
        raise ConfigError(f"{path} must fit within the frame and have positive width and height")
    return crop


def _twitch(raw: object) -> TwitchConfig:
    options = _table(raw, "twitch", (field.name for field in fields(TwitchConfig)))
    logins = options.get("streamers", [])
    if not isinstance(logins, list):
        raise ConfigError("twitch.streamers must be an array of Twitch logins")
    if "*" in logins and logins != ["*"]:
        raise ConfigError('Use twitch.streamers = ["*"] by itself to discover all channels')
    streamers = ("*",) if logins == ["*"] else tuple(
        dict.fromkeys(_login(login, "twitch.streamers") for login in logins)
    )
    return TwitchConfig(
        game_name=_text(options.get("game_name", "Grand Theft Auto VI"), "twitch.game_name"),
        streamers=streamers,
        lookback_days=_integer(options.get("lookback_days", 7), "twitch.lookback_days", 1),
        clips_per_vod=_integer(options.get("clips_per_vod", 10), "twitch.clips_per_vod", 1),
    )


def _highlights(raw: object) -> HighlightsConfig:
    options = _table(raw, "highlights", (field.name for field in fields(HighlightsConfig)))
    duration = _integer(options.get("duration_seconds", 35), "highlights.duration_seconds", 15, 60)
    weights = {
        name: _nonnegative_number(options.get(name, 1.0), f"highlights.{name}")
        for name in ("chat_weight", "audio_weight", "twitch_clips_weight")
    }
    if not any(weights.values()):
        raise ConfigError("At least one highlight weight must be greater than zero")
    return HighlightsConfig(
        clips_per_vod=_integer(options.get("clips_per_vod", 3), "highlights.clips_per_vod", 1),
        duration_seconds=duration,
        lead_in_seconds=_integer(
            options.get("lead_in_seconds", 4), "highlights.lead_in_seconds", 0, duration - 1
        ),
        **weights,
    )


def _editing(raw: object) -> EditingConfig:
    options = _table(raw, "editing", (field.name for field in fields(EditingConfig)))
    layout = options.get("layout", "gameplay")
    if layout not in ("gameplay", "split"):
        raise ConfigError("editing.layout must be 'gameplay' or 'split'")
    streamers = options.get("streamers", {})
    if not isinstance(streamers, dict):
        raise ConfigError("editing.streamers must be a TOML table")
    layouts = {}
    for name, value in streamers.items():
        path = f"editing.streamers.{name}"
        login = _login(name, path)
        if login in layouts:
            raise ConfigError(f"Duplicate layout for {login}")
        rectangles = _table(value, path, ("facecam", "gameplay"))
        layouts[login] = SplitLayout(
            facecam=_crop(rectangles.get("facecam"), f"{path}.facecam"),
            gameplay=_crop(rectangles.get("gameplay"), f"{path}.gameplay"),
        )
    return EditingConfig(layout, _text(options.get("whisper_model", "base"), "editing.whisper_model"), layouts)


def _schedule(raw: object) -> dict[str, ScheduleConfig]:
    platforms = _table(raw, "schedule", PLATFORMS)
    schedule = {}
    for platform in PLATFORMS:
        path = f"schedule.{platform}"
        options = _table(platforms.get(platform, {}), path, ("daily_limit", "min_spacing_minutes"))
        schedule[platform] = ScheduleConfig(
            daily_limit=_integer(options.get("daily_limit", 0), f"{path}.daily_limit", 0),
            min_spacing_minutes=_integer(options.get("min_spacing_minutes", 120),
                                         f"{path}.min_spacing_minutes", 0),
        )
    return schedule


def _live(raw: object) -> LiveConfig:
    options = _table(raw, "live", (field.name for field in fields(LiveConfig)))
    defaults = LiveConfig()
    multiplier = _nonnegative_number(options.get("spike_multiplier", defaults.spike_multiplier),
                                     "live.spike_multiplier")
    if multiplier < 1:
        raise ConfigError("live.spike_multiplier must be at least 1")
    return LiveConfig(
        max_channels=_integer(options.get("max_channels", defaults.max_channels), "live.max_channels", 1, 20),
        messages_per_10_seconds=_integer(options.get("messages_per_10_seconds", defaults.messages_per_10_seconds),
                                         "live.messages_per_10_seconds", 1),
        spike_multiplier=multiplier,
        cooldown_seconds=_integer(options.get("cooldown_seconds", defaults.cooldown_seconds),
                                  "live.cooldown_seconds", 60),
    )


def load_config(filename: str | Path = "config.toml") -> Config:
    """Read a config file. Relative storage paths start at the file's directory."""
    try:
        path = Path(filename).expanduser().resolve()
        with path.open("rb") as file:
            raw = tomllib.load(file)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ConfigError(f"Cannot read {filename}: {exc}") from exc
    options = _table(raw, "config", ("paths", "twitch", "highlights", "editing", "schedule", "live"))
    paths = _table(options.get("paths", {}), "paths", ("data_dir",))
    directory = _text(paths.get("data_dir", "data"), "paths.data_dir")
    try:
        data_dir = (path.parent / Path(directory).expanduser()).resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise ConfigError(f"Invalid paths.data_dir: {exc}") from exc
    return Config(
        paths=PathsConfig(data_dir),
        twitch=_twitch(options.get("twitch", {})),
        highlights=_highlights(options.get("highlights", {})),
        editing=_editing(options.get("editing", {})),
        schedule=_schedule(options.get("schedule", {})),
        live=_live(options.get("live", {})),
    )
