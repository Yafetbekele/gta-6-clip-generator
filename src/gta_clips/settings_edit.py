"""Change individual settings in config.toml, keeping its comments and layout."""

from dataclasses import dataclass
from pathlib import Path
import json
import os
import re
import time

from .config import DAYS, Config, ConfigError, load_config


@dataclass(frozen=True)
class Setting:
    table: str
    key: str
    kind: str  # int, float, bool, text, time or days
    label: str
    help: str = ""
    minimum: float | None = None
    maximum: float | None = None
    group: str = ""


SETTINGS = (
    Setting("editing", "renderer", "text", "Clip renderer",
            "ffmpeg or remotion. Remotion adds animated styling and requires the fit layout. Restart the watcher after saving.",
            group="Video editing"),
    Setting("runtime", "watch_days", "days", "Days to watch", "Days when the live watcher runs.",
            group="Schedule"),
    Setting("runtime", "watch_start", "time", "Start watching at", "Local time. Use 00:00 to 24:00 for all day.",
            group="Schedule"),
    Setting("runtime", "watch_end", "time", "Stop watching at", "A time earlier than the start runs past midnight.",
            group="Schedule"),
    Setting("runtime", "render_every_minutes", "int", "Render saved windows every (minutes)",
            "Finishes clips whose VOD became available. 0 turns it off.", 0, 1440, "Schedule"),
    Setting("runtime", "ingest_every_minutes", "int", "Clip finished VODs every (minutes)",
            "Downloads whole VODs, which is slow. 0 turns it off.", 0, 1440, "Schedule"),
    Setting("runtime", "publish_every_minutes", "int", "Send approved clips every (minutes)",
            "Only clips you approved are ever sent. 0 turns it off.", 0, 1440, "Schedule"),
    Setting("twitch", "game_name", "text", "Twitch category", "Exact category name, such as Just Chatting.",
            group="What to watch"),
    Setting("live", "max_channels", "int", "Channels watched at once", "The most-viewed live channels, up to 20.",
            1, 20, "What to watch"),
    Setting("live", "spike_multiplier", "float", "Times the normal chat rate",
            "A burst is judged only against the channel's normal rate over the last 5 minutes.", 1, 100, "Burst detection"),
    Setting("live", "min_unique_chatters", "int", "Different chatters needed",
            "Stops a few people or one spammer from triggering a clip in a quiet chat.", 1, 1000, "Burst detection"),
    Setting("live", "cooldown_seconds", "int", "Seconds between clips per channel", "", 60, 86400,
            "Burst detection"),
    Setting("live", "warmup_seconds", "int", "Warmup before a channel can trigger (seconds)",
            "Time to learn a channel's normal chat rate.", 0, 86400, "Burst detection"),
    Setting("live", "control_interval_seconds", "int", "Random comparison clip every (seconds)",
            "Per channel. 0 turns it off.", 0, 864000, "Comparison clips"),
    Setting("live", "near_miss_interval_seconds", "int", "Near-miss clip at most every (seconds)",
            "Per channel. 0 turns it off.", 0, 864000, "Comparison clips"),
    Setting("live", "near_miss_multiplier", "float", "Near-miss multiple of the baseline",
            "Must be below the burst multiple.", 1, 100, "Comparison clips"),
    Setting("highlights", "require_motion", "bool", "Require on-screen motion (VOD clips)",
            "Turn off for Just Chatting.", group="VOD clips"),
    Setting("highlights", "clips_per_vod", "int", "Clips per VOD", "", 1, 50, "VOD clips"),
)
BY_NAME = {f"{setting.table}.{setting.key}": setting for setting in SETTINGS}


def current_values(config: Config) -> dict:
    sections = {"runtime": config.runtime, "twitch": config.twitch, "live": config.live,
                "highlights": config.highlights, "editing": config.editing}
    values = {}
    for name, setting in BY_NAME.items():
        value = getattr(sections[setting.table], setting.key)
        values[name] = list(value) if isinstance(value, tuple) else value
    return values


def _coerce(setting: Setting, value):
    """Check the type here; load_config checks the meaning before anything is saved."""
    if setting.kind == "bool":
        if type(value) is not bool:
            raise ConfigError(f"{setting.label} must be on or off")
        return value
    if setting.kind == "int":
        if type(value) is not int:
            raise ConfigError(f"{setting.label} must be a whole number")
    elif setting.kind == "float":
        if type(value) not in (int, float):
            raise ConfigError(f"{setting.label} must be a number")
        value = float(value)
    elif setting.kind == "days":
        if not isinstance(value, list) or any(day not in DAYS for day in value):
            raise ConfigError(f"{setting.label} must be a list of days")
        return [day for day in DAYS if day in value]
    elif not isinstance(value, str) or not value.strip() or "\n" in value:
        raise ConfigError(f"{setting.label} must be a single line of text")
    if setting.kind in ("int", "float") and (
            (setting.minimum is not None and value < setting.minimum)
            or (setting.maximum is not None and value > setting.maximum)):
        raise ConfigError(f"{setting.label} must be between {setting.minimum:g} and {setting.maximum:g}")
    return value.strip() if isinstance(value, str) else value


def _toml(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    # JSON strings and string arrays are valid TOML for these plain values.
    return json.dumps(value, ensure_ascii=False)


def set_value(text: str, table: str, key: str, value) -> str:
    """Replace ``key`` in ``[table]``, adding the key or the table if it is missing."""
    lines = text.splitlines()
    header = re.compile(rf"^\s*\[\s*{re.escape(table)}\s*\]\s*(#.*)?$")
    start = next((index for index, line in enumerate(lines) if header.match(line)), None)
    entry = f"{key} = {_toml(value)}"
    if start is None:
        while lines and not lines[-1].strip():
            lines.pop()
        lines += ["", f"[{table}]", entry]
        return "\n".join(lines) + "\n"
    end = next((index for index in range(start + 1, len(lines)) if lines[index].lstrip().startswith("[")),
               len(lines))
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    for index in range(start + 1, end):
        if pattern.match(lines[index]):
            if lines[index].rstrip().endswith("[") or (lines[index].count("[") > lines[index].count("]")):
                raise ConfigError(f"Edit {table}.{key} by hand; it spans several lines")
            lines[index] = entry
            return "\n".join(lines) + "\n"
    # Add it after the table's last setting, so comments introducing the next table stay with it.
    insert = start + 1
    for index in range(start + 1, end):
        stripped = lines[index].strip()
        if stripped and not stripped.startswith("#"):
            insert = index + 1
    lines.insert(insert, entry)
    return "\n".join(lines) + "\n"


def save(config_path: Path, changes: dict) -> Config:
    """Validate ``changes`` with the normal loader, back up config.toml, then replace it."""
    if not isinstance(changes, dict) or not changes:
        raise ConfigError("No settings to save")
    text = config_path.read_text(encoding="utf-8")
    for name, value in changes.items():
        setting = BY_NAME.get(name)
        if setting is None:
            raise ConfigError(f"{name} cannot be changed here")
        text = set_value(text, setting.table, setting.key, _coerce(setting, value))
    # Checking beside the real file keeps relative paths such as data_dir the same.
    candidate = config_path.with_name(config_path.name + ".candidate")
    candidate.write_text(text, encoding="utf-8")
    try:
        config = load_config(candidate)
    finally:
        candidate.unlink(missing_ok=True)
    backups = config.paths.data_dir / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    (backups / f"{config_path.stem}-{int(time.time())}.toml").write_text(
        config_path.read_text(encoding="utf-8"), encoding="utf-8")
    temporary = config_path.with_name(config_path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, config_path)
    return config
