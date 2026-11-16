"""Command-line entry point."""

import argparse
from collections.abc import Sequence
from pathlib import Path

from .config import ConfigError, load_config


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gta-clips")
    parser.add_argument("--config", type=Path, default=Path("config.toml"), help="TOML settings file")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check-config", help="Validate settings and show the configured targets")
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        parser.exit(2, f"Configuration error: {exc}\n")

    destinations = [platform for platform, settings in config.schedule.items() if settings.daily_limit]
    print(f"Configuration is valid for {config.twitch.game_name}.")
    print(f"Allowlisted streamers: {', '.join(config.twitch.streamers) or 'none configured'}")
    print(f"Storage: {config.paths.data_dir}")
    print(f"Enabled posting destinations: {', '.join(destinations) or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
