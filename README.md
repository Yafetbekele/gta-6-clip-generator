# GTA 6 Clip Generator

A Python CLI that turns Twitch archives into reviewed, captioned vertical clips. YouTube Shorts and Instagram Reels can publish on a schedule after approval. The TikTok adapter sends approved drafts to your inbox for you to finish in TikTok, subject to app access and TikTok’s content rules.

## Install on Windows

1. Install Python 3.11 or later. The Python dependencies provide FFmpeg on supported platforms, including Windows x64. Set `FFMPEG` in `.env` if you need to use your own [FFmpeg build](https://ffmpeg.org/download.html) with `libx264` and `libass`.
2. In PowerShell from the repository directory, run:

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -e .
   Copy-Item config.example.toml config.toml
   Copy-Item .env.example .env
   .\.venv\Scripts\gta-clips.exe check-config
   ```

3. Put specific Twitch logins in `twitch.streamers` or set `streamers = ["*"]` to discover archives from all channels in the selected category. Verify reposting permission when you review clips. Set the destination limits in `config.toml` when you’re ready. A limit of zero disables that destination. The schedule counts days in the machine’s local time zone.
4. Create an app in the [Twitch Developer Console](https://dev.twitch.tv/console/apps), generate its client secret, and set `TWITCH_CLIENT_ID` and `TWITCH_CLIENT_SECRET` in `.env`. The program exchanges them for a Twitch app token.

On macOS or Linux, use `.venv/bin/python` and `.venv/bin/gta-clips`, and use `cp` for the example files. The commands below assume the virtual environment is active or its script directory is on your PATH.

### Media and highlight settings

- `twitch.game_name` is resolved by name. Twitch limits category archive searches to 500 videos, so a busy category can push older allowlisted archives outside that window.
- `highlights.clips_per_vod`, `duration_seconds`, and `lead_in_seconds` control clip selection. Adjust the chat, audio, and Twitch clip weights independently. Candidates also need sustained movement across multiple areas of the picture, including near the scored moment. That filter screens out mostly static intro and countdown screens before rendering. Check the actual content during review.
- The app downloads a temporary full VOD for audio and visual analysis and cleans its normal work files when processing completes or fails. Plan disk space for the largest archive you expect to process. The first transcription downloads the configured Whisper model unless `editing.whisper_model` points to a local model.
- The default `editing.layout = "fit"` keeps the entire stream visible, centered without stretching inside a 1080×1920 vertical video. Black space above and below a landscape stream holds the credit and captions. The stream appears smaller because none of its edges are cropped. Use `"gameplay"` for a center crop that fills the vertical frame.
- Set `editing.layout = "split"` and add a streamer’s `facecam` and `gameplay` crop rectangles using the example. Values describe fractions of the source frame in `x, y, width, height` order. Channels without split coordinates use the gameplay layout.
- Helix provides clip timestamps and view counts. For archived chat, install [TwitchDownloaderCLI](https://github.com/lay295/TwitchDownloader), set `TWITCH_DOWNLOADER` to the executable path, or put an existing TwitchDownloader JSON export at `data/chat-imports/<VOD_ID>.json`. The program uses chat timestamps for scoring and continues with audio and Twitch clips if chat is unavailable.

## Create and review clips

```text
gta-clips ingest
gta-clips queue list
gta-clips queue preview 123456789-1
gta-clips queue edit 123456789-1 --title "The getaway almost worked"
gta-clips queue approve 123456789-1 --platform youtube --youtube-visibility private --no-youtube-made-for-kids
gta-clips queue show 123456789-1
gta-clips publish
```

Use an ID from `queue list`. Preview opens the MP4 in your default video player. Use `--description` or `--hashtags "#GTA6 #Gaming"` with `queue edit` to change the other metadata. The app preserves the Twitch credit and channel link in the description. Use `queue reject ID` to reject a pending clip and repeat `--platform` on approval to select multiple destinations. For YouTube, choose the desired visibility and the appropriate audience setting.

`queue export ID DIRECTORY` copies the MP4 and a separate caption text file for manual publishing. For TikTok drafts, use the exported caption text in TikTok, since the draft API accepts video bytes and you enter the caption in the app.

## Catch live chat bursts

```text
gta-clips auth twitch
gta-clips watch-live
```

Run the authorization once and approve **read-only chat access** in Twitch. Use the same Twitch app credentials in `.env` and register `http://localhost:3000` in that app's redirect URLs. The watcher uses Twitch EventSub, sends no chat messages, and stores burst counts and timestamps locally.

- With `streamers = ["*"]`, the watcher selects the most-viewed live channels in the configured category, up to `live.max_channels` (15 by default, configurable up to 20). A named allowlist restricts which live channels are eligible. The selection refreshes every 30 seconds.
- Each newly watched broadcast collects five minutes of chat before it can trigger a clip (`live.warmup_seconds = 300`). A new connection after a disconnect warms up again. A burst requires at least 15 messages in 10 seconds and at least three times the preceding five-minute rolling baseline, excluding the current 10-second window. Set `messages_per_10_seconds`, `spike_multiplier`, and `cooldown_seconds` under `[live]` to tune it. The default cooldown is two minutes per broadcast.
- Trigger features are saved with each new burst, including baseline messages per 10 seconds, observed seconds, and detector settings. Older clips have missing features, not estimated values.

Rate clip value from 1 (poor) to 5 (excellent), with optional notes. Ratings do not approve or publish clips. You can update a rating by repeating the command. Export the ratings joined with transcripts and trigger features for later analysis:

```powershell
gta-clips queue rate CLIP_ID 4 --notes "Strong reaction and clear payoff"
gta-clips queue ratings-export data/ratings.json
```

For a browser-based review screen, run `gta-clips review-web --open`. It opens a local page at `http://127.0.0.1:8765` with video playback, 1–5 scores, notes, filters, and a Save & next unrated button. Ratings persist in the same SQLite database used by the CLI and ratings export. Reopening the launcher reuses the existing server. This page only saves ratings and has no publishing controls. On Windows, a shortcut can target `.venv\Scripts\pythonw.exe` with arguments `-m gta_clips --config "FULL_PATH_TO_CONFIG" review-web --open` and the project folder as its working directory.

The export includes unrated clips with null scores. Compare scores against spike ratio, normal chat volume, and streamer to look for patterns. These are selected chat-spike samples, so they cannot establish how good ordinary non-triggered moments are. No automatic threshold tuning occurs until ratings are collected and analyzed.
- Each burst requests exactly 30 seconds before and 30 seconds after its recorded timestamp. The watcher waits for the public Twitch VOD to cover that full window, downloads just that section, captions it, and adds it to the normal local review queue. If there is no in-progress archive, rendering can happen after the stream ends. Unavailable windows expire after 48 hours.
- Run `gta-clips live-status` from another terminal to see recent bursts and whether their clips are ready. Use **Ctrl+C** to stop watching. Restart the watcher or run `gta-clips render-live` to process saved windows once their VODs become available.

For continuous monitoring, run `watch-live` in a terminal or configure one Windows Task Scheduler task to start it at logon and restart on failure. Use the same executable, config, and user account as the other workers. The watcher logs to `data/watch-live.log`.

## Authorize platform access

### YouTube Shorts

1. In [Google Cloud Console](https://console.cloud.google.com/), enable YouTube Data API v3. Set up the OAuth consent screen and create a **Desktop app** OAuth client. If the OAuth app is in testing, add your account as a test user.
2. Set `YOUTUBE_CLIENT_ID` and `YOUTUBE_CLIENT_SECRET` in `.env`, then run `gta-clips auth youtube`. Sign into the channel you intend to use. The OAuth browser callback goes to a temporary listener on localhost.
3. Follow [Google’s OAuth verification process](https://support.google.com/cloud/answer/9110914) if required for your app’s audience and requested scopes. If you need more than the project’s default YouTube quota, follow [YouTube’s audit and quota extension process](https://developers.google.com/youtube/v3/guides/quota_and_compliance_audits).
4. Approve a clip with `--platform youtube`, choose its visibility and audience, and run `gta-clips publish`. YouTube classifies eligible vertical uploads as Shorts.

### Instagram Reels

1. In [Meta for Developers](https://developers.facebook.com/), set up **Instagram API with Instagram Login** for an Instagram professional account. Register an HTTPS redirect URL you control, and request `instagram_business_basic` and `instagram_business_content_publishing`. Standard Access is for accounts you own or manage and add to the app. For other accounts, follow Meta’s App Review and Advanced Access requirements.
2. Set `INSTAGRAM_CLIENT_ID`, `INSTAGRAM_CLIENT_SECRET`, `INSTAGRAM_REDIRECT_URI`, and `INSTAGRAM_GRAPH_VERSION` to the values from your app. Run `gta-clips auth instagram`. Open the printed URL and paste the HTTPS callback address into the terminal when prompted. The app verifies the callback and stores a long-lived token locally.
3. Install the S3 extra with `python -m pip install -e ".[instagram]"` inside the virtual environment. Configure `INSTAGRAM_S3_BUCKET`, `AWS_REGION`, and AWS credentials via the [SDK credential chain](https://boto3.amazonaws.com/v1/documentation/api/latest/guide/credentials.html). Use a private bucket. Grant object upload, read, and delete permissions for the `gta-clips/` prefix and set a lifecycle rule for abandoned objects. Meta retrieves the MP4 using a temporary signed URL. The adapter deletes the staged object when publishing completes.
4. Approve a clip with `--platform instagram`. The scheduler creates the Reel container, polls its status, then publishes when it is ready. [Meta content publishing](https://developers.facebook.com/documentation/instagram-platform/content-publishing)

### TikTok drafts

1. In [TikTok for Developers](https://developers.tiktok.com/), create a web app, add Login Kit and Content Posting API, enable Upload, register an HTTPS redirect URL you control, and request `video.upload`. Use the sandbox to demonstrate the integration, verify the required URLs, and submit the app for review with the end-to-end demo TikTok requests.
2. Set `TIKTOK_CLIENT_KEY`, `TIKTOK_CLIENT_SECRET`, and `TIKTOK_REDIRECT_URI`. Run `gta-clips auth tiktok` and paste the callback address into the terminal after authorizing. Approval for a particular app or clip is up to TikTok.
3. Approve an eligible clip with `--platform tiktok`. Once the status is `draft`, open the notification in the TikTok inbox, add the caption and clip credit, and finish posting.

TikTok’s Direct Post guidelines exclude private upload utilities and apps that copy arbitrary content from other platforms. This project uses the official draft route for eligible content. Use local export for clips you intend to publish manually. [TikTok draft upload](https://developers.tiktok.com/docs/en/content-posting-api-get-started-upload-content) · [TikTok app review](https://developers.tiktok.com/docs/en/app-review-guidelines) · [Content sharing guidelines](https://developers.tiktok.com/docs/en/content-sharing-guidelines)

Keep OAuth callback URLs, credentials, tokens, media, and the SQLite database private. By default, the project ignores `.env`, `config.toml`, and `data/` in Git. If you customize the storage directory, keep it outside tracked files.

## Schedule the workers

- Create two tasks in **Windows Task Scheduler**. Run `ingest` hourly and `publish` every few minutes. Set **Program/script** to the absolute path of `.venv\Scripts\gta-clips.exe`. Set **Add arguments** to `--config "C:\path\to\config.toml" ingest` or the same with `publish`. Set **Start in** to the repository directory and choose **Do not start a new instance** for each task. Use the same Windows account that can read your credentials and run the media tools.
- On Linux or macOS, schedule the same commands with your scheduler using absolute paths to the CLI and config. Activate or install any media tools for that task account.
- Check `data/ingest.log` and `data/publish.log`. `queue show ID` reports the state and provider ID for each destination. Expected transient failures back off and are retried. For a definitive failure, fix the issue and run `gta-clips retry PLATFORM ID`. A held result needs inspection on the platform before any manual upload because the previous request may have succeeded. TikTok’s daily limit counts drafts delivered to the inbox.

## Module map

| Module | Role |
| --- | --- |
| `config.py` and `__main__.py` | Validate settings and dispatch CLI commands. |
| `db.py` and `pipeline.py` | Store VOD IDs and clip states, prevent overlapping workers, and commit completed VODs and clips together. |
| `discovery.py` and `download.py` | Find category archives and popular clips via Helix, then download temporary video and chat data. |
| `highlights.py` | Rank moments by chat, audio, and Twitch clips, then keep non-overlapping windows with visual activity. |
| `captions.py` and `editing.py` | Generate Whisper word timing and render vertical MP4s with captions and credit. |
| `metadata.py` and `review.py` | Generate transcript-based suggestions and handle preview, editing, approval, rejection, and export. |
| `oauth.py`, `upload/`, and `scheduler.py` | Authorize accounts, checkpoint official API uploads, and enforce the selected schedule. |
| `live.py` and `live_render.py` | Count live chat bursts and render the matching 60-second VOD sections for review. |
