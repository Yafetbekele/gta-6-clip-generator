"""Readable, credential-free error details and subprocesses that never outlive their timeout."""

from collections.abc import Sequence
import logging
import os
import re
import signal
import subprocess
import sys
import threading

STDERR_TAIL = 2000
_QUERY = re.compile(r"(https?://[^\s?#\"']+)\?[^\s\"']*")
_SECRET = re.compile(r"(?i)\b(access_token|refresh_token|client_secret|token|sig|signature|auth)=([^\s&\"']+)")


def redact(text: str) -> str:
    """Remove URL query strings and token-like parameters, which can carry signed credentials."""
    return _SECRET.sub(r"\1=…", _QUERY.sub(r"\1?…", text))


def describe(error: BaseException, limit: int = 600) -> str:
    """A short, safe summary of an exception for logs and database detail columns."""
    if isinstance(error, CommandFailed):
        return error.summary
    message = redact(str(error)).strip()
    text = f"{type(error).__name__}: {message}" if message else type(error).__name__
    return text[:limit]


class CommandFailed(subprocess.CalledProcessError):
    """A subprocess failed. ``summary`` holds its exit code and a redacted stderr tail."""

    def __init__(self, returncode, cmd, output=None, stderr=None, *, timed_out=False):
        super().__init__(returncode, cmd, output, stderr)
        self.timed_out = timed_out
        program = os.path.basename(str(cmd[0])) if cmd else "command"
        if len(cmd) > 2 and cmd[1] == "-m":
            program = str(cmd[2])
        tail = redact((stderr or "").strip())[-STDERR_TAIL:]
        reason = "timed out" if timed_out else f"exited with {returncode}"
        self.summary = f"{program} {reason}" + (f": {tail}" if tail else "")

    def __str__(self):
        return self.summary


def _kill_tree(process: subprocess.Popen):
    if process.poll() is not None:
        return
    if os.name == "nt":
        # A plain kill leaves grandchildren (yt-dlp's FFmpeg) running and holding files open.
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)],
                       capture_output=True, check=False)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            process.kill()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()


def run(command: Sequence[str], *, timeout: float, cwd=None, low_priority: bool = False,
        capture_stdout: bool = True) -> subprocess.CompletedProcess:
    """Run a command, kill its whole process tree on timeout, and raise CommandFailed on error."""
    options = {}
    if os.name == "nt":
        flags = 0x00004000 if low_priority else 0  # BELOW_NORMAL_PRIORITY_CLASS
        options["creationflags"] = flags | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        options["start_new_session"] = True
        if low_priority:
            options["preexec_fn"] = lambda: os.nice(5)
    process = subprocess.Popen(
        list(command), cwd=cwd, text=True, encoding="utf-8", errors="replace",
        stdout=subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
        stderr=subprocess.PIPE, **options,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(process)
        stdout, stderr = process.communicate()
        raise CommandFailed(-1, list(command), stdout, stderr, timed_out=True) from None
    except BaseException:
        _kill_tree(process)
        raise
    if process.returncode:
        raise CommandFailed(process.returncode, list(command), stdout, stderr)
    return subprocess.CompletedProcess(list(command), process.returncode, stdout, stderr)


def install_thread_logging():
    """Send uncaught thread exceptions (such as a dead render worker) to the log file."""
    def hook(args):
        if issubclass(args.exc_type, SystemExit):
            return
        logging.critical("Thread %s stopped: %s",
                         args.thread.name if args.thread else "?",
                         describe(args.exc_value) if args.exc_value else args.exc_type.__name__,
                         exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
    threading.excepthook = hook


def log_versions():
    versions = [f"python {sys.version.split()[0]}"]
    for module, attribute in (("yt_dlp.version", "__version__"), ("faster_whisper", "__version__"),
                              ("av", "__version__"), ("websockets", "__version__")):
        try:
            imported = __import__(module, fromlist=[attribute])
            versions.append(f"{module.split('.')[0]} {getattr(imported, attribute)}")
        except Exception:  # A missing optional module must not stop startup.
            versions.append(f"{module.split('.')[0]} unavailable")
    logging.info("Versions: %s", ", ".join(versions))
