"""Whether a clip was made the way the running tests say clips are made now.

Each test (hook styles, filler, dead-time trim, caption styles, burst cuts) records its choice on
the clip when it is rendered. A clip rendered before a test was turned on, or while a setting had
not reached the running processes, does not carry the test, and posting it would weaken the
test and post a clip the settings no longer ask for. The ready step offers only clips that fit.

Every test switch in the config must have a rule in EXPERIMENTS (tests/test_recipe.py checks
this), so a new test can't be turned on without its clips being checked before they are posted.
"""

from typing import Callable

from . import cuts
from . import filler as fillers
from . import trim as trims
from .config import Config

Rule = Callable[[Config, dict], bool]


def _mode(value: str | None, mode: str, none: str) -> bool:
    """For "off"/"test"/"on" tests: off means no arm recorded or NONE, on means a real arm, test either."""
    if mode == "off":
        return value in (None, none)
    if mode == "on":
        return value not in (None, none)
    return value is not None


def _switch(value: str | None, testing: bool) -> bool:
    """For true/false tests: a style recorded exactly when the test is on."""
    return (value is not None) == testing


def _cut(config: Config, clip: dict) -> bool:
    cut = (clip.get("cut_before"), clip.get("cut_after"))
    if cut == (None, None):
        # VOD clips and clips from before cuts were recorded: the standard window.
        return not (config.live.test_cuts and clip.get("kind") == "burst")
    if config.live.test_cuts and clip.get("kind") == "burst":
        return cut == cuts.STANDARD or (cut[0] in cuts.BEFORE and cut[1] in cuts.AFTER)
    return cut == cuts.STANDARD


# Config switch (section.field) -> whether a clip fits that switch's current setting.
EXPERIMENTS: dict[str, Rule] = {
    "editing.test_hooks": lambda config, clip: _switch(clip.get("hook_style"), config.editing.test_hooks),
    "editing.filler": lambda config, clip: _mode(clip.get("filler"), config.editing.filler, fillers.NONE),
    "editing.trim_dead_time": lambda config, clip: _mode(clip.get("trim"), config.editing.trim_dead_time,
                                                         trims.NONE),
    "editing.test_captions": lambda config, clip: _switch(clip.get("caption_style"), config.editing.test_captions),
    "live.test_cuts": _cut,
}

# Ready-step columns the rules read (plus the burst's trigger kind as "kind").
COLUMNS = ("hook_style", "filler", "trim", "caption_style", "cut_before", "cut_after")


def misfits(config: Config, clip: dict) -> list[str]:
    """The test switches whose current setting this clip was not made with (empty when it fits)."""
    return [name for name, fits in EXPERIMENTS.items() if not fits(config, clip)]


def _cut_arm(config: Config, clip: dict) -> str | None:
    if not config.live.test_cuts or clip.get("kind") != "burst":
        return None
    return f"{clip.get('cut_before')}+{clip.get('cut_after')}"


# Config switch -> the arm of that test a clip is in, or None when the test is off or the clip isn't
# part of it. Picking uses these to post first whatever the tests have the least of (see need).
ARMS: dict[str, Callable[[Config, dict], str | None]] = {
    "editing.test_hooks": lambda config, clip: clip.get("hook_style") if config.editing.test_hooks else None,
    "editing.filler": lambda config, clip: clip.get("filler") if config.editing.filler != "off" else None,
    "editing.trim_dead_time": lambda config, clip: (clip.get("trim") if config.editing.trim_dead_time == "test"
                                                    else None),
    "editing.test_captions": lambda config, clip: clip.get("caption_style") if config.editing.test_captions else None,
    "live.test_cuts": _cut_arm,
}


def arms(config: Config, clip: dict) -> dict[str, str]:
    """The running tests this clip is part of, and its arm in each."""
    found = {name: arm(config, clip) for name, arm in ARMS.items()}
    return {name: arm for name, arm in found.items() if arm is not None}


Counts = dict[str, dict[str, int]]


def count_arms(config: Config, clips) -> Counts:
    """How many of ``clips`` (already picked for posting) each arm of each running test has."""
    counts: Counts = {}
    for clip in clips:
        add(counts, arms(config, dict(clip)))
    return counts


def add(counts: Counts, clip_arms: dict[str, str]):
    for name, arm in clip_arms.items():
        counts.setdefault(name, {})
        counts[name][arm] = counts[name].get(arm, 0) + 1


def need(counts: Counts, clip_arms: dict[str, str], known: dict[str, set[str]]) -> float:
    """How far behind this clip's arms are, summed over the running tests (higher: post it sooner).

    For each test, an arm's shortfall from the average of the arms that can be picked now (those
    among the candidates), relative to that average, so every test counts about as much. Arms no
    longer made (from an earlier run of a test) don't count.
    """
    total = 0.0
    for name, arm in clip_arms.items():
        seen = counts.get(name, {})
        names = known.get(name, set()) | {arm}
        mean = sum(seen.get(other, 0) for other in names) / len(names)
        total += (mean - seen.get(arm, 0)) / (mean + 1)
    return total


# Features of the views model that name a test's arm directly, or a clip's length (the cut test).
# Ranking ignores them, so the model can't keep choosing one arm and the tests stay fair; it still
# learns them. Which arm gets posted is decided by need() instead.
ARM_FEATURES = ("hook=", "filler=", "trim=", "captions=")
LENGTH_FEATURES = ("clip_seconds", "cut_before", "cut_after")


def blind(features: dict) -> dict:
    """``features`` with the arm a clip was given left out."""
    return {name: (None if name in LENGTH_FEATURES else value) for name, value in features.items()
            if not name.startswith(ARM_FEATURES)}
