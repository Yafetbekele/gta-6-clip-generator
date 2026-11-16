# GTA 6 Clip Generator

This project will turn Twitch VODs into vertical short-form videos, then queue them for local review and approved posting. The configuration module is available first.

## Run the configuration check

Use Python 3.11 or later. The configuration module uses the Python standard library.

```powershell
Copy-Item config.example.toml config.toml
$env:PYTHONPATH = "src"
python -m gta_clips check-config
```

You can also install it with `python -m pip install -e .` and run `gta-clips check-config`. To use a different settings file, run `gta-clips --config path/to/settings.toml check-config`.

Add Twitch channel logins to `twitch.streamers` in `config.toml`. For example, `streamers = ["your_channel", "another_channel"]`. An empty list is valid and selects no channels.

- `highlights` sets the clip count, duration, lead-in time, and relative weights for chat, audio, and Twitch clips.
- `editing` selects the layout and Whisper model. For split layouts, the optional per-streamer rectangles in the example specify where to crop facecam and gameplay.
- `schedule` sets each platform's daily limit and minimum spacing. The example disables posting with a daily limit of zero.
- `paths.data_dir` is relative to the selected config file. It will hold the SQLite database and media.

The configuration check reads settings and prints a summary. It makes no API requests and needs no credentials. The Twitch discovery module will read `TWITCH_CLIENT_ID` and `TWITCH_CLIENT_SECRET` from the process environment. The `.env.example` file lists those names for reference. Keep actual credentials outside Git.
