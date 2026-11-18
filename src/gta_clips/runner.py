"""Run the workers on the schedule in [runtime], one process each, until asked to stop."""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import logging
import os
import subprocess
import sys
import time

from .config import Config, ConfigError, load_config
from .db import worker_lock
from .diagnostics import _kill_tree, describe
from .live import STOP_FILE as WATCH_STOP_FILE, write_json

STATUS_FILE = "run-status.json"
# Creating this file asks `gta-clips run` to stop its watcher and exit.
STOP_FILE = "run.stop"
TICK_SECONDS = 15
# A watcher that is still finishing a render after this long is stopped forcefully.
WATCH_STOP_GRACE = 15 * 60
JOBS = (("ingest", "ingest_every_minutes"), ("render-live", "render_every_minutes"),
        ("publish", "publish_every_minutes"))


def _command(config_path: Path, name: str) -> list[str]:
    return [sys.executable, "-m", "gta_clips", "--config", str(config_path), name]


def _spawn(config_path: Path, name: str) -> subprocess.Popen:
    flags = 0x4000 | 0x08000000 if sys.platform == "win32" else 0  # below normal priority, no window
    # Each worker writes its own log file, so console output is not needed.
    return subprocess.Popen(_command(config_path, name), cwd=config_path.parent, creationflags=flags,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@dataclass
class Child:
    name: str
    process: subprocess.Popen | None = None
    started: float | None = None
    last_exit: int | None = None
    last_finished: float | None = None
    next_run: float = 0.0
    restarts: int = 0
    failures: int = 0
    stopping_since: float | None = None

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def reap(self):
        if self.process is not None and self.process.poll() is not None:
            self.last_exit = self.process.returncode
            self.last_finished = time.time()
            self.process = None
            self.stopping_since = None

    def as_dict(self) -> dict:
        return {"running": self.running, "pid": self.process.pid if self.running else None,
                "started": self.started if self.running else None, "last_exit": self.last_exit,
                "last_finished": self.last_finished, "next_run": self.next_run or None,
                "restarts": self.restarts, "stopping": self.stopping_since is not None}


class Runner:
    def __init__(self, config_path: Path, config: Config):
        self.config_path, self.config = config_path, config
        self.data = config.paths.data_dir
        self.watcher = Child("watch-live")
        self.jobs = {name: Child(name) for name, _ in JOBS}
        self.started = time.time()
        self.config_error: str | None = None

    def reload(self):
        try:
            self.config = load_config(self.config_path)
            self.config_error = None
        except ConfigError as error:
            # Keep running on the last good settings; the dashboard shows the problem.
            if self.config_error != str(error):
                logging.error("Keeping previous settings: %s", error)
            self.config_error = str(error)

    def tick(self, now: float | None = None):
        now = time.time() if now is None else now
        self.reload()
        self._watch(now)
        runtime = self.config.runtime
        for name, setting in JOBS:
            job = self.jobs[name]
            job.reap()
            minutes = getattr(runtime, setting)
            if not minutes:
                job.next_run = 0
                continue
            if not job.next_run:
                job.next_run = now + 60  # Let the watcher start first.
            if now >= job.next_run and not job.running:
                logging.info("Starting %s", name)
                job.process, job.started = _spawn(self.config_path, name), now
                job.next_run = now + minutes * 60
        self.write_status("running", now)

    def _watch(self, now: float):
        watcher = self.watcher
        exited = watcher.process is not None and watcher.process.poll() is not None
        stopping = watcher.stopping_since is not None
        watcher.reap()
        if exited and not stopping:
            ran = now - (watcher.started or now)
            if watcher.last_exit == 0 and ran < 60:
                # Usually another watcher is already running (started from a terminal).
                watcher.next_run = now + 300
                logging.info("watch-live exited at once; checking again in 5 min")
            elif watcher.last_exit:
                # Back off when the watcher keeps failing, for example without chat authorization.
                watcher.failures = watcher.failures + 1 if ran < 600 else 1
                delay = min(1800, 60 * 2 ** (watcher.failures - 1))
                watcher.next_run = now + delay
                logging.warning("watch-live exited with %s; next start in %s min",
                                watcher.last_exit, delay // 60)
        wanted = self.config.runtime.watching_at(datetime.fromtimestamp(now))
        if wanted and not watcher.running and now >= watcher.next_run:
            (self.data / WATCH_STOP_FILE).unlink(missing_ok=True)
            logging.info("Starting watch-live (%s)", self.config.runtime.describe())
            watcher.process, watcher.started = _spawn(self.config_path, "watch-live"), now
            watcher.restarts += 1
            watcher.next_run = 0
        elif watcher.running and not wanted:
            self.stop_watcher(now)
        if watcher.running and watcher.stopping_since and now - watcher.stopping_since > WATCH_STOP_GRACE:
            logging.warning("watch-live did not stop in time; ending it")
            _kill_tree(watcher.process)

    def stop_watcher(self, now: float):
        if self.watcher.running and self.watcher.stopping_since is None:
            logging.info("Asking watch-live to stop")
            self.watcher.stopping_since = now
            (self.data / WATCH_STOP_FILE).write_text("stop", encoding="utf-8")

    def write_status(self, state: str, now: float):
        runtime = self.config.runtime
        try:
            write_json(self.data / STATUS_FILE, {
                "state": state, "pid": os.getpid(), "started": self.started, "updated": now,
                "config": str(self.config_path), "config_error": self.config_error,
                "window": runtime.describe(),
                "in_window": runtime.watching_at(datetime.fromtimestamp(now)),
                "watcher": self.watcher.as_dict(),
                "jobs": {name: {**self.jobs[name].as_dict(), "every_minutes": getattr(runtime, setting)}
                         for name, setting in JOBS},
            })
        except OSError as error:
            logging.debug("Could not write run status: %s", error)


def run(config_path: Path, stop_check=None):
    """Supervise the workers until data/run.stop appears (or ``stop_check()`` is true)."""
    config_path = config_path.expanduser().resolve()
    config = load_config(config_path)
    data = config.paths.data_dir
    with worker_lock(data, "run"):
        stop_file = data / STOP_FILE
        stop_file.unlink(missing_ok=True)
        runner = Runner(config_path, config)
        logging.info("Running on schedule: %s", config.runtime.describe())
        try:
            while not stop_file.exists() and not (stop_check and stop_check()):
                try:
                    runner.tick()
                except Exception as error:  # One bad tick must not end the schedule.
                    logging.exception("Scheduler tick failed: %s", describe(error))
                time.sleep(TICK_SECONDS)
        finally:
            logging.info("Stopping the schedule")
            runner.stop_watcher(time.time())
            deadline = time.time() + WATCH_STOP_GRACE
            while runner.watcher.running and time.time() < deadline:
                runner.write_status("stopping", time.time())
                time.sleep(2)
            if runner.watcher.running:
                _kill_tree(runner.watcher.process)
            runner.watcher.reap()
            runner.write_status("stopped", time.time())
            stop_file.unlink(missing_ok=True)


def start_detached(config_path: Path) -> int:
    """Start `gta-clips run` in the background, without a console window."""
    executable = Path(sys.executable)
    windowless = executable.with_name("pythonw.exe")
    if sys.platform == "win32" and windowless.exists():
        executable = windowless
    command = [str(executable), "-m", "gta_clips", "--config", str(config_path), "run"]
    flags = 0x00000008 | 0x00000200 | 0x08000000 if sys.platform == "win32" else 0
    process = subprocess.Popen(command, cwd=config_path.parent, creationflags=flags,
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, start_new_session=sys.platform != "win32")
    return process.pid
