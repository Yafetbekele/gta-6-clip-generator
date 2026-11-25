"""Load TOML settings and validate them before starting a job."""

from collections.abc import Iterable
from dataclasses import dataclass, fields
from datetime import datetime
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
    # Seconds between something happening on stream and chat reacting to it. 0 keeps chat as-is.
    chat_reaction_lag_seconds: int = 0
    # Skip moments without on-screen motion. Turn off for categories like Just Chatting.
    require_motion: bool = True


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
    renderer: str = "ffmpeg"
    # Give each clip a random kind of hook box (see hook_styles.py) instead of the usual one.
    test_hooks: bool = False
    # Background gameplay under the stream (see filler.py): "off", "test" (random kind or none) or "on".
    filler: str = "off"
    # Cut dead time out of clips (see trim.py): "off", "test" (half the clips, at random) or "on".
    trim_dead_time: str = "off"
    # Give each clip a random caption style (see caption_styles.py) instead of the usual one.
    test_captions: bool = False
    # Where Whisper runs: "auto" (the GPU when it works, else the CPU), "cuda" or "cpu".
    whisper_device: str = "auto"
    # Skip moments where the streamer mostly talks to chat (see chat_talk.py).
    skip_chat_talk: bool = True


@dataclass(frozen=True)
class ScheduleConfig:
    daily_limit: int = 0
    min_spacing_minutes: int = 120
    # Up to this many extra minutes, drawn per post, so posts don't land at fixed times.
    jitter_minutes: int = 0


DEFAULT_IGNORED_CHATTERS = ("nightbot", "streamelements", "moobot", "fossabot", "streamlabs",
                            "wizebot", "sery_bot", "soundalerts", "botrixoficial")


@dataclass(frozen=True)
class LiveConfig:
    warmup_seconds: int = 300
    max_channels: int = 15
    # Live channels with fewer viewers than this are not watched. 0 watches any size.
    min_viewers: int = 0
    # 0 means no absolute floor: a burst is judged only against the channel's normal rate.
    messages_per_10_seconds: int = 0
    spike_multiplier: float = 3.0
    cooldown_seconds: int = 120
    # A burst also needs this many different chatters, so one spammer can't trigger it.
    min_unique_chatters: int = 5
    ignored_chatters: tuple[str, ...] = DEFAULT_IGNORED_CHATTERS
    # Comparison windows rendered like bursts, so ratings can show what the trigger adds.
    control_interval_seconds: int = 3600
    near_miss_multiplier: float = 2.0
    near_miss_interval_seconds: int = 3600
    # Live clips rendered at the same time. Each one downloads, transcribes and encodes on its own.
    render_workers: int = 2
    # Give each burst clip a random lead-in and follow-through (see cuts.py) instead of 30 s + 30 s.
    test_cuts: bool = False
    # Also cut the day's most-viewed Twitch viewer clips (viewer_clips.py), as a test against bursts.
    test_viewer_clips: bool = False
    # Render a window only when the ready step needs clips for that account set (see on_demand.py),
    # keeping render_buffer clips ready per set, instead of rendering every window.
    render_on_demand: bool = False
    render_buffer: int = 3


DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@dataclass(frozen=True)
class RuntimeConfig:
    """When `gta-clips run` starts each worker. Times are local, as HH:MM."""
    watch_days: tuple[str, ...] = DAYS
    watch_start: str = "00:00"
    watch_end: str = "24:00"
    ingest_every_minutes: int = 0
    render_every_minutes: int = 15
    publish_every_minutes: int = 0
    instagram_browser_accounts: tuple[str, ...] = ()
    ready_every_minutes: int = 30
    ready_per_day: int = 6
    # Days a ready-to-post day folder is kept.
    ready_keep_days: int = 7
    # Newest clip videos kept per account set; older ones are deleted. 0 keeps everything.
    max_clips_per_account: int = 0
    # Measures clips, reads post views and refits the views model.
    stats_every_minutes: int = 60
    # Collects viewer clips when live.test_viewer_clips is on.
    viewer_clips_every_minutes: int = 60

    def watching_at(self, moment: datetime) -> bool:
        """True if ``moment`` falls in a watch window. A window past midnight belongs to its start day."""
        start, end = _minutes(self.watch_start), _minutes(self.watch_end)
        now = moment.hour * 60 + moment.minute
        today = DAYS[moment.weekday()]
        yesterday = DAYS[(moment.weekday() - 1) % 7]
        if start == end or (start == 0 and end == 1440):
            return today in self.watch_days
        if start < end:
            return today in self.watch_days and start <= now < end
        return (now >= start and today in self.watch_days) or (now < end and yesterday in self.watch_days)

    def describe(self) -> str:
        days = "every day" if set(self.watch_days) == set(DAYS) else ", ".join(
            day.title() for day in DAYS if day in self.watch_days) or "no days"
        if _minutes(self.watch_start) == _minutes(self.watch_end) or (
                self.watch_start == "00:00" and self.watch_end == "24:00"):
            return f"All day, {days}"
        return f"{self.watch_start}–{self.watch_end}, {days}"


def _minutes(value: str) -> int:
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


@dataclass(frozen=True)
class AccountConfig:
    """One set of YouTube, TikTok and Instagram accounts fed by Twitch categories or by streamers.

    A streamer account (``streamers`` set, no category) gets clips only from those Twitch
    channels, whatever they are streaming, and those channels' clips go only to it.
    """
    name: str
    # The first category; ``more_categories`` holds any others watched for this set. Empty for a
    # streamer account.
    category: str
    # Twitch stream language, such as "en". Empty watches every language.
    language: str = ""
    channels: int = 5
    label: str = ""
    # Where this set's clips are posted, for people posting by hand. Names only, never logins.
    youtube: str = ""
    tiktok: str = ""
    instagram: str = ""
    # The sign-in email for these accounts, shown in notes. Never a password.
    email: str = ""
    # Platforms where this set's account is banned, such as ["instagram"]. Reports mark them.
    banned: tuple[str, ...] = ()
    more_categories: tuple[str, ...] = ()
    # Twitch logins this set follows instead of a category (a streamer account).
    streamers: tuple[str, ...] = ()

    @property
    def categories(self) -> tuple[str, ...]:
        return (self.category, *self.more_categories) if self.category else ()

    @property
    def watches(self) -> str:
        """What this set gets clips from, for people reading reports."""
        if self.streamers:
            return "streamer " + ", ".join(self.streamers)
        return ", ".join(self.categories)

    @property
    def title(self) -> str:
        return self.label or self.name

    @property
    def destinations(self) -> tuple[tuple[str, str], ...]:
        return tuple((platform, handle) for platform, handle in
                     (("YouTube", self.youtube), ("TikTok", self.tiktok), ("Instagram", self.instagram)) if handle)


@dataclass(frozen=True)
class Feed:
    """Channels watched for one category and language, shared by the accounts that use them.

    A streamer feed (``streamers`` set) watches those channels whenever they are live, in any
    category, for its one streamer account.
    """
    category: str
    language: str
    channels: int
    accounts: tuple[str, ...]
    streamers: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        if self.streamers:
            return f"streamers|{self.accounts[0]}"
        return f"{self.category}|{self.language}"

    @property
    def label(self) -> str:
        return ", ".join(self.streamers) if self.streamers else self.category


MAX_TOTAL_CHANNELS = 100


@dataclass(frozen=True)
class Config:
    paths: PathsConfig
    twitch: TwitchConfig
    highlights: HighlightsConfig
    editing: EditingConfig
    schedule: dict[str, ScheduleConfig]
    live: LiveConfig = LiveConfig()
    runtime: RuntimeConfig = RuntimeConfig()
    accounts: tuple[AccountConfig, ...] = ()

    def account(self, name: str | None) -> AccountConfig | None:
        return next((account for account in self.accounts if account.name == name), None)

    @property
    def feeds(self) -> tuple[Feed, ...]:
        """Accounts with the same category and language share one set of watched channels."""
        if not self.accounts:
            return (Feed(self.twitch.game_name, "", self.live.max_channels, ()),)
        # Streamer feeds come first, so they claim their channels before any category feed can.
        followed = tuple(Feed("", "", len(account.streamers), (account.name,), account.streamers)
                         for account in self.accounts if account.streamers)
        grouped: dict[tuple[str, str], list[AccountConfig]] = {}
        names: dict[str, str] = {}
        for account in self.accounts:
            # Each category gets the set's full channel count, so a quiet one never starves a busy one.
            for category in account.categories:
                names.setdefault(category.casefold(), category)
                grouped.setdefault((category.casefold(), account.language), []).append(account)
        return followed + tuple(Feed(names[category], language, sum(a.channels for a in members),
                                     tuple(a.name for a in members))
                                for (category, language), members in grouped.items())

    @property
    def total_channels(self) -> int:
        return sum(feed.channels for feed in self.feeds)


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


def _boolean(value: object, path: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{path} must be true or false")
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
    require_motion = options.get("require_motion", True)
    if type(require_motion) is not bool:
        raise ConfigError("highlights.require_motion must be true or false")
    return HighlightsConfig(
        chat_reaction_lag_seconds=_integer(options.get("chat_reaction_lag_seconds", 0),
                                           "highlights.chat_reaction_lag_seconds", 0, 30),
        require_motion=require_motion,
        clips_per_vod=_integer(options.get("clips_per_vod", 3), "highlights.clips_per_vod", 1),
        duration_seconds=duration,
        lead_in_seconds=_integer(
            options.get("lead_in_seconds", 4), "highlights.lead_in_seconds", 0, duration - 1
        ),
        **weights,
    )


def _editing(raw: object) -> EditingConfig:
    options = _table(raw, "editing", (field.name for field in fields(EditingConfig)))
    layout = options.get("layout", "fit")
    if layout not in ("fit", "gameplay", "split"):
        raise ConfigError("editing.layout must be 'fit', 'gameplay' or 'split'")
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
    renderer = options.get("renderer", "ffmpeg")
    if renderer not in ("ffmpeg", "remotion"):
        raise ConfigError("editing.renderer must be 'ffmpeg' or 'remotion'")
    if renderer == "remotion" and layout != "fit":
        raise ConfigError("The Remotion renderer currently requires editing.layout = 'fit'")
    filler = options.get("filler", "off")
    if filler not in ("off", "test", "on"):
        raise ConfigError("editing.filler must be 'off', 'test' or 'on'")
    if filler != "off" and renderer == "remotion":
        raise ConfigError("editing.filler needs editing.renderer = 'ffmpeg'")
    trim_dead_time = options.get("trim_dead_time", "off")
    if trim_dead_time not in ("off", "test", "on"):
        raise ConfigError("editing.trim_dead_time must be 'off', 'test' or 'on'")
    if trim_dead_time != "off" and renderer == "remotion":
        raise ConfigError("editing.trim_dead_time needs editing.renderer = 'ffmpeg'")
    whisper_device = options.get("whisper_device", "auto")
    if whisper_device not in ("auto", "cuda", "cpu"):
        raise ConfigError("editing.whisper_device must be 'auto', 'cuda' or 'cpu'")
    return EditingConfig(layout, _text(options.get("whisper_model", "base"), "editing.whisper_model"), layouts, renderer,
                         whisper_device=whisper_device,
                         skip_chat_talk=_boolean(options.get("skip_chat_talk", True), "editing.skip_chat_talk"),
                         test_hooks=_boolean(options.get("test_hooks", False), "editing.test_hooks"),
                         filler=filler, trim_dead_time=trim_dead_time,
                         test_captions=_boolean(options.get("test_captions", False), "editing.test_captions"))


def _schedule(raw: object) -> dict[str, ScheduleConfig]:
    platforms = _table(raw, "schedule", PLATFORMS)
    schedule = {}
    for platform in PLATFORMS:
        path = f"schedule.{platform}"
        options = _table(platforms.get(platform, {}), path,
                         ("daily_limit", "min_spacing_minutes", "jitter_minutes"))
        schedule[platform] = ScheduleConfig(
            daily_limit=_integer(options.get("daily_limit", 0), f"{path}.daily_limit", 0),
            min_spacing_minutes=_integer(options.get("min_spacing_minutes", 120),
                                         f"{path}.min_spacing_minutes", 0),
            jitter_minutes=_integer(options.get("jitter_minutes", 0), f"{path}.jitter_minutes", 0, 240),
        )
    return schedule


def _live(raw: object) -> LiveConfig:
    options = _table(raw, "live", (field.name for field in fields(LiveConfig)))
    defaults = LiveConfig()
    multiplier = _nonnegative_number(options.get("spike_multiplier", defaults.spike_multiplier),
                                     "live.spike_multiplier")
    if multiplier < 1:
        raise ConfigError("live.spike_multiplier must be at least 1")
    near_miss = _nonnegative_number(options.get("near_miss_multiplier", defaults.near_miss_multiplier),
                                    "live.near_miss_multiplier")
    if not 1 <= near_miss < multiplier:
        raise ConfigError("live.near_miss_multiplier must be at least 1 and below live.spike_multiplier")
    ignored = options.get("ignored_chatters", list(defaults.ignored_chatters))
    if not isinstance(ignored, list):
        raise ConfigError("live.ignored_chatters must be an array of Twitch logins")
    return LiveConfig(
        min_unique_chatters=_integer(options.get("min_unique_chatters", defaults.min_unique_chatters),
                                     "live.min_unique_chatters", 1),
        ignored_chatters=tuple(dict.fromkeys(_login(name, "live.ignored_chatters") for name in ignored)),
        control_interval_seconds=_integer(
            options.get("control_interval_seconds", defaults.control_interval_seconds),
            "live.control_interval_seconds", 0),
        near_miss_multiplier=near_miss,
        near_miss_interval_seconds=_integer(
            options.get("near_miss_interval_seconds", defaults.near_miss_interval_seconds),
            "live.near_miss_interval_seconds", 0),
        warmup_seconds=_integer(options.get("warmup_seconds", 300), "live.warmup_seconds", 0),
        max_channels=_integer(options.get("max_channels", defaults.max_channels), "live.max_channels", 1, 20),
        min_viewers=_integer(options.get("min_viewers", defaults.min_viewers), "live.min_viewers", 0),
        messages_per_10_seconds=_integer(options.get("messages_per_10_seconds", defaults.messages_per_10_seconds),
                                         "live.messages_per_10_seconds", 0),
        spike_multiplier=multiplier,
        cooldown_seconds=_integer(options.get("cooldown_seconds", defaults.cooldown_seconds),
                                  "live.cooldown_seconds", 60),
        render_workers=_integer(options.get("render_workers", defaults.render_workers),
                                "live.render_workers", 1, 4),
        test_cuts=_boolean(options.get("test_cuts", defaults.test_cuts), "live.test_cuts"),
        test_viewer_clips=_boolean(options.get("test_viewer_clips", defaults.test_viewer_clips),
                                   "live.test_viewer_clips"),
        render_on_demand=_boolean(options.get("render_on_demand", defaults.render_on_demand),
                                  "live.render_on_demand"),
        render_buffer=_integer(options.get("render_buffer", defaults.render_buffer), "live.render_buffer", 1, 20),
    )


def _accounts(raw: object) -> tuple[AccountConfig, ...]:
    if not isinstance(raw, list):
        raise ConfigError("accounts must be written as [[accounts]] tables")
    accounts = []
    for index, value in enumerate(raw, start=1):
        path = f"accounts #{index}"
        options = _table(value, path, (field.name for field in fields(AccountConfig)))
        name = _text(options.get("name"), f"{path}.name").lower()
        # The name is also the clip folder, so keep it to safe characters.
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,31}", name):
            raise ConfigError(f"{path}.name must be lowercase letters, digits and dashes, such as gta6-1")
        if any(account.name == name for account in accounts):
            raise ConfigError(f"Two accounts are named {name}")
        language = options.get("language", "")
        if not isinstance(language, str) or not re.fullmatch(r"([a-z]{2})?", language.strip().lower()):
            raise ConfigError(f"{path}.language must be a two-letter code such as en, or empty for any")
        label = options.get("label", "")
        if not isinstance(label, str) or "\n" in label:
            raise ConfigError(f"{path}.label must be a single line of text")
        handles = {}  # Names and emails only; passwords never belong in the config.
        for platform in ("youtube", "tiktok", "instagram", "email"):
            handle = options.get(platform, "")
            if not isinstance(handle, str) or "\n" in handle or len(handle) > 100:
                raise ConfigError(f"{path}.{platform} must be a channel or account name on one line")
            handles[platform] = handle.strip()
        # A platform bans the set's whole account there; "platform:handle" bans one earlier handle only.
        banned = options.get("banned", [])
        if not isinstance(banned, list) or any(
                not isinstance(entry, str)
                or not re.fullmatch(rf"({'|'.join(PLATFORMS)})(:@?[A-Za-z0-9_.]{{1,30}})?", entry.strip())
                for entry in banned):
            raise ConfigError(f"{path}.banned must list platforms ({', '.join(PLATFORMS)}) "
                              'or earlier handles such as "instagram:old_handle"')
        banned = [entry.strip().replace(":@", ":") for entry in banned]
        streamers = options.get("streamers", [])
        if not isinstance(streamers, list) or len(streamers) > 20 or any(
                not isinstance(login, str) or not re.fullmatch(r"[a-z0-9_]{1,25}", login.strip().lower())
                for login in streamers):
            raise ConfigError(f"{path}.streamers must be a list of up to 20 Twitch logins, such as [\"kaicenat\"]")
        streamers = list(dict.fromkeys(login.strip().lower() for login in streamers))
        for login in streamers:
            if any(login in account.streamers for account in accounts):
                raise ConfigError(f"{path}.streamers: {login} already has its own account")
        categories = options.get("category")
        if streamers:
            if categories not in (None, "", []):
                raise ConfigError(f"{path} follows streamers, so it takes no category")
            categories = [""]
        elif isinstance(categories, list):
            if not categories:
                raise ConfigError(f"{path}.category must name at least one Twitch category")
            categories = [_text(category, f"{path}.category") for category in categories]
        else:
            categories = [_text(categories, f"{path}.category")]
        if len({category.casefold() for category in categories}) != len(categories):
            raise ConfigError(f"{path}.category lists the same category twice")
        accounts.append(AccountConfig(
            name=name, category=categories[0], more_categories=tuple(categories[1:]), banned=tuple(banned),
            streamers=tuple(streamers),
            language=language.strip().lower(),
            channels=_integer(options.get("channels", 5), f"{path}.channels", 1, 20),
            label=label.strip(), **handles,
        ))
    total = sum(account.channels * len(account.categories) + len(account.streamers) for account in accounts)
    if total > MAX_TOTAL_CHANNELS:
        raise ConfigError(f"Accounts watch {total} channels in total; the most is {MAX_TOTAL_CHANNELS}")
    return tuple(accounts)


def _clock(value: object, path: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d|24:00", value):
        raise ConfigError(f"{path} must be a time like 18:30 (00:00 to 24:00)")
    return value


def _interval(value: object, path: str) -> int:
    minutes = _integer(value, path, 0, 1440)
    if 0 < minutes < 5:
        raise ConfigError(f"{path} must be 0 (off) or at least 5 minutes")
    return minutes


def _runtime(raw: object) -> RuntimeConfig:
    options = _table(raw, "runtime", (field.name for field in fields(RuntimeConfig)))
    defaults = RuntimeConfig()
    days = options.get("watch_days", list(defaults.watch_days))
    if not isinstance(days, list) or any(day not in DAYS for day in days):
        raise ConfigError(f"runtime.watch_days must list days from {', '.join(DAYS)}")
    browser_accounts = options.get("instagram_browser_accounts", [])
    if not isinstance(browser_accounts, list) or any(
        not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+", name)
        for name in browser_accounts
    ):
        raise ConfigError("runtime.instagram_browser_accounts must list account folder names")
    return RuntimeConfig(
        instagram_browser_accounts=tuple(browser_accounts),
        watch_days=tuple(day for day in DAYS if day in days),
        watch_start=_clock(options.get("watch_start", defaults.watch_start), "runtime.watch_start"),
        watch_end=_clock(options.get("watch_end", defaults.watch_end), "runtime.watch_end"),
        ingest_every_minutes=_interval(options.get("ingest_every_minutes", defaults.ingest_every_minutes),
                                       "runtime.ingest_every_minutes"),
        render_every_minutes=_interval(options.get("render_every_minutes", defaults.render_every_minutes),
                                       "runtime.render_every_minutes"),
        publish_every_minutes=_interval(options.get("publish_every_minutes", defaults.publish_every_minutes),
                                        "runtime.publish_every_minutes"),
        ready_every_minutes=_interval(options.get("ready_every_minutes", defaults.ready_every_minutes),
                                      "runtime.ready_every_minutes"),
        ready_per_day=_integer(options.get("ready_per_day", defaults.ready_per_day), "runtime.ready_per_day", 0, 50),
        ready_keep_days=_integer(options.get("ready_keep_days", defaults.ready_keep_days),
                                 "runtime.ready_keep_days", 1, 365),
        max_clips_per_account=_integer(options.get("max_clips_per_account", defaults.max_clips_per_account),
                                       "runtime.max_clips_per_account", 0, 100000),
        stats_every_minutes=_interval(options.get("stats_every_minutes", defaults.stats_every_minutes),
                                      "runtime.stats_every_minutes"),
        viewer_clips_every_minutes=_interval(
            options.get("viewer_clips_every_minutes", defaults.viewer_clips_every_minutes),
            "runtime.viewer_clips_every_minutes"),
    )


def load_config(filename: str | Path = "config.toml") -> Config:
    """Read a config file. Relative storage paths start at the file's directory."""
    try:
        path = Path(filename).expanduser().resolve()
        with path.open("rb") as file:
            raw = tomllib.load(file)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ConfigError(f"Cannot read {filename}: {exc}") from exc
    options = _table(raw, "config", ("paths", "twitch", "highlights", "editing", "schedule", "live", "runtime",
                                     "accounts"))
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
        runtime=_runtime(options.get("runtime", {})),
        accounts=_accounts(options.get("accounts", [])),
    )
