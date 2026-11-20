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
- `highlights.clips_per_vod`, `duration_seconds`, and `lead_in_seconds` control clip selection. Adjust the chat, audio, and Twitch clip weights independently. Candidates also need sustained movement across multiple areas of the picture, including near the scored moment. That filter screens out mostly static intro and countdown screens before rendering. Set `highlights.require_motion = false` for categories such as Just Chatting, where a good moment can be a still camera. Check the actual content during review.
- `highlights.chat_reaction_lag_seconds` shifts chat earlier before scoring, because viewers type after they see something. It defaults to 0. Compare ratings before changing it.
- A VOD that fails is retried every 15 minutes, up to 5 attempts, after which its status becomes `abandoned`. Subscriber-only or removed VODs are marked `unavailable` and are not retried. Clips that rendered before a failure are kept.
- The app downloads a temporary full VOD for audio and visual analysis and cleans its normal work files when processing completes or fails. Plan disk space for the largest archive you expect to process. The first transcription downloads the configured Whisper model unless `editing.whisper_model` points to a local model.
- The default `editing.layout = "fit"` keeps the entire stream visible, centered without stretching inside a 1080×1920 vertical video. Black space above and below a landscape stream holds the credit and captions. The stream appears smaller because none of its edges are cropped. Use `"gameplay"` for a center crop that fills the vertical frame.
- Set `editing.layout = "split"` and add a streamer’s `facecam` and `gameplay` crop rectangles using the example. Values describe fractions of the source frame in `x, y, width, height` order. Channels without split coordinates use the gameplay layout.
- Helix provides clip timestamps and view counts. For archived chat, install [TwitchDownloaderCLI](https://github.com/lay295/TwitchDownloader), set `TWITCH_DOWNLOADER` to the executable path, or put an existing TwitchDownloader JSON export at `data/chat-imports/<VOD_ID>.json`. The program uses chat timestamps for scoring and continues with audio and Twitch clips if chat is unavailable.

## Create and review clips

### Optional Remotion polish

New Remotion clips automatically select a short opening quote from the whole
transcript, rather than always taking its first sentence. The local extractive
selector skips common greetings and subscription messages, preserves wording
(including negation), and omits the overlay when it cannot find a suitable short
quote. It is a heuristic, not visual understanding or an AI-written summary;
Whisper errors and context-dependent quotes still need review. The overlay lasts
up to seven seconds and stays above the video, separate from speech captions.
Its text, source quote, and caption timings are saved beside the MP4 in `.edit.json`.
The review page displays the actual opening text when this manifest is available.

Both renderers also add a debate headline: a short accent label such as "HOT TAKE
OR FACTS?" over the clip's own quoted line, or, when Whisper is not confident the
speech is English or no good line exists, an opinion question about the Twitch
category (for example "GTA 6 is overhyped. Change my mind."). These come from fixed
templates, not an AI model. They are opinions about the game or prompts over the
streamer's own words, never claims about the streamer, and like everything else
they are only published after you approve the clip. The FFmpeg renderer keeps the
headline on screen for the whole clip, just above the full-width video.

For an existing pending clip:

```text
gta-clips queue hook CLIP_ID --suggest-only
gta-clips queue hook CLIP_ID
gta-clips queue hook CLIP_ID --text "Your edited context, up to 72 characters"
gta-clips queue hook CLIP_ID --clear
```

Only `--suggest-only` avoids rendering. The other commands fetch the original VOD
window and render a new version with the configured renderer, so its VOD must still be available.
The original video and ratings remain saved, and the queue switches to the new
video only after success. Approved clips or clips with upload records cannot be
re-edited this way. Preview the replacement before approving it. Refresh the review
page to load the new video. Editing the publishing title does not change burned-in
text. Existing clips are never rebuilt automatically by this feature.

The `remotion/` project adds a full-frame 1080×1920 composition with an animated
Twitch credit, a dark gradient background, a progress line, and readable active-word
captions. It preserves the original audio and the exact selected window. It does
not add music, change the selected moment, or publish clips.

Install Node.js 22.13+ (or 24+) and run `npm ci` inside `remotion/`. Run
`npm run dev` there to preview and edit `PolishedClip` in Remotion Studio.
The default Studio view is a layout demonstration; production footage and Whisper
captions arrive through JSON props. Generated captions are one programmatic layer,
not individually source-editable Studio caption items.

To use it for new clips, set `renderer = "remotion"` and `layout = "fit"` under
`[editing]` in `config.toml`, then restart the watcher/worker. Existing clips and
ratings stay unchanged. Use `renderer = "ffmpeg"` to restore the original renderer.
The Remotion dependencies and bundled browser must be available to the worker's
Windows account. Its first render may download the browser. If installed outside
this source checkout, set `REMOTION_PROJECT` to the absolute `remotion/` directory.
An optional `REMOTION_LICENSE_KEY` environment variable is forwarded to renders.

Python trims an uncaptioned intermediate, reuses Whisper word timings, invokes the
Node renderer with a 30-minute timeout, validates the output duration, and only
then adds the video to the existing review workflow. Errors go through the existing
retry system; it never silently substitutes a different renderer. Remotion needs
more CPU and disk than FFmpeg alone. Temporary footage is cleaned by the normal
pipeline work-directory cleanup. This first version supports the full-frame `fit`
layout; `split` and `gameplay` remain available with FFmpeg.

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
- A channel already being watched keeps its place until it drops more than 5 places below the cutoff, and stays at least 10 minutes. Channels whose chat cannot be read, or whose current stream has no usable VOD, are replaced by the next channel. The log reports `Coverage: watched=N/desired=M` on each refresh.
- Each newly watched broadcast collects five minutes of watched chat before it can trigger a clip (`live.warmup_seconds = 300`). Time spent unwatched does not count toward warmup or the baseline. A channel unwatched for more than 60 seconds warms up again. A burst is judged against the channel's own chat rate, not a fixed message count: it needs `spike_multiplier` times the preceding five-minute rolling baseline (excluding the current 10-second window) and at least `min_unique_chatters` different chatters, which keeps a few people in a quiet chat from triggering it. Common bot accounts are ignored (`ignored_chatters`). Tune `spike_multiplier`, `min_unique_chatters`, and `cooldown_seconds` under `[live]` or on the dashboard's Settings tab. An optional `messages_per_10_seconds` floor is off (0) by default. Channels with fewer live viewers than `min_viewers` are not watched (0, the default, watches any size).
- To show what the trigger adds, the watcher also records occasional comparison windows: random **control** moments and **near misses** (rises above `near_miss_multiplier` that did not reach `spike_multiplier`). They render and appear in review like bursts, and `live-status` and the export label them by `source`. Set either interval to 0 to turn it off.
- Trigger features are saved with each new window, including the baseline, unique chatters, the peak over the following 30 seconds, viewer count and rank, stream age, and seconds Twitch muted. Per-second chat counts are kept in the `chat_seconds` table so other thresholds can be replayed later. Older clips have missing features, not estimated values.
- Network errors and Twitch disconnects are retried with backoff (10 seconds up to 5 minutes). Only a missing or revoked chat authorization stops the watcher. Each run is recorded in `watch_sessions` with its exit reason.

Rate clip value from 1 (poor) to 5 (excellent), with optional notes. Ratings do not approve or publish clips. You can update a rating by repeating the command; the latest rating is current, and every earlier one is kept in `rating_events`. Export the ratings joined with transcripts and features for later analysis, as JSON or CSV, or print a summary:

```powershell
gta-clips queue rate CLIP_ID 4 --notes "Strong reaction and clear payoff"
gta-clips queue ratings-export data/ratings.json
gta-clips queue ratings-export data/ratings.csv
gta-clips queue ratings-summary
```

`ratings-summary` shows the mean score by source and streamer, and the Spearman rank correlation between each recorded feature and the score. It describes your ratings; it does not train a model or change any setting. With fewer than 30 ratings it warns that the numbers are mostly noise.

For a browser-based review screen, run `gta-clips review-web --open`. It opens a local page at `http://127.0.0.1:8765` (`http://localhost:8765` also works) with video playback, 1–5 scores, notes, filters, and a Save & next unrated button. Ratings persist in the same SQLite database used by the CLI and ratings export, along with how far into the clip you watched. Reopening the launcher reuses the existing server. This page only saves ratings and has no publishing controls. On Windows, a shortcut can target `.venv\Scripts\pythonw.exe` with arguments `-m gta_clips --config "FULL_PATH_TO_CONFIG" review-web --open` and the project folder as its working directory.

The export includes unrated clips with null scores. Compare scores against spike ratio, normal chat volume, and streamer to look for patterns. Burst clips alone are selected chat-spike samples, so they cannot establish how good ordinary moments are; rated control and near-miss windows give that comparison. Nothing tunes thresholds automatically.

Before a database upgrade, the app copies the database to `data/backups/`. Upgrades only add tables and columns, so existing clips and ratings are not rewritten.
- Each burst requests exactly 30 seconds before and 30 seconds after its recorded timestamp. The watcher waits for the public Twitch VOD to cover that full window, downloads that section with 3 seconds of padding, cuts the exact 60 seconds while rendering, captions it, and adds it to the normal local review queue. A failed attempt is retried after 2, 4, 8 … up to 60 minutes, and `live-status` shows the last error. Subscriber-only, private, or missing archives are marked unavailable and are never worked around. If there is no in-progress archive, rendering can happen after the stream ends. Unavailable windows expire after 48 hours.
- Run `gta-clips live-status` from another terminal to see recent bursts and whether their clips are ready. Use **Ctrl+C** to stop watching. Restart the watcher or run `gta-clips render-live` to process saved windows once their VODs become available.

For continuous monitoring, run `watch-live` in a terminal or configure one Windows Task Scheduler task to start it at logon and restart on failure. Use the same executable, config, and user account as the other workers. The watcher logs to `data/watch-live.log`.

## Dashboard and run schedule

Run `gta-clips dashboard --open` to open a local page at `http://127.0.0.1:8766`. It shows:

- **Overview:** which channels the watcher is in right now, their viewer counts and current chat rate against each channel's normal rate, plus today's bursts, clips waiting for review, and ratings. Charts show viewers in watched channels over 24 hours, chat messages per minute over the last hour, and clip windows per hour, split into bursts, near misses, and random controls.
- **Streamers:** the last 7 days per streamer: approximate watch time, peak viewers, bursts, clips, and average rating.
- **Activity:** recent clip windows with their status and last error, watcher runs, streams that cannot be clipped, and scheduled jobs.
- **Settings:** watch days and hours, how often each worker runs, the category, the number of channels, and the burst and comparison-clip thresholds. Saving checks the values with the normal config loader, keeps your comments, and copies the old file to `data/backups/` first. Detection settings take effect when the watcher restarts; the page has a button for that.

**Start schedule** runs `gta-clips run` in the background. It starts `watch-live` during the watch hours in `[runtime]`, asks it to stop outside them (a clip that is rendering finishes first), restarts it with a growing delay if it keeps failing, and starts `render-live`, `ingest`, and `publish` at their intervals. `publish` is off by default and only ever sends clips you approved. **Stop schedule** stops it. You can also run `gta-clips run` in a terminal; it logs to `data/run.log`. If you used Windows Task Scheduler for these workers, turn those tasks off before starting the schedule so they do not run twice. Only one copy of each worker runs at a time.

Viewer history starts when the watcher first runs with this version; it saves one snapshot of the watched channels every 5 minutes. The dashboard only listens on this computer and reads the same SQLite database as the other commands.

## Account sets

To run several sets of YouTube, TikTok and Instagram accounts, add one `[[accounts]]` table per set to `config.toml` (`config.example.toml` has a commented example):

```toml
[[accounts]]
name = "irl-es"          # lowercase letters, digits and dashes; used for the folder and logins
label = "IRL Spanish"
category = "Just Chatting"
language = "es"          # Twitch stream language; leave empty for any language
channels = 5
email = ""               # optional: the set's sign-in email, shown in ready-to-post notes
youtube = ""             # optional: where its clips go, shown in ready-to-post notes
tiktok = ""
instagram = ""
```

- Sets with the same category and language share one group of channels. Three English GTA 6 sets with 5 channels each watch 15 GTA 6 channels together. Each new clip window goes to the set in that group with the fewest windows so far, so a clip belongs to exactly one set and is never posted twice.
- When account sets are configured, `[twitch].game_name` and `[live].max_channels` are ignored. The watcher covers each set's category and language, up to 100 channels in total.
- Clips are saved in `clips/<set name>/` inside the storage directory. Debate headlines use the set's language (English, Spanish or Portuguese). A quote from the stream is only used when the transcript is in the same language.
- Connect each set's logins with `gta-clips auth youtube --account NAME`, and the same for `tiktok` and `instagram`. A set only publishes with its own logins. `gta-clips auth youtube` without `--account` is the login for clips made before account sets. Twitch chat uses one login for everything.
- `gta-clips accounts` lists each set with its clips to review, approved clips, clips waiting for a VOD and connected logins. The dashboard's **Accounts** tab and the review page's account filter show the same.
- `gta-clips accounts sort` assigns earlier clip windows and clips to a set when their category and stream language match one, and moves their files into the set's folder. Clips that already have an upload record and all ratings stay as they are.

## Ready to post by hand

`gta-clips ready` (run by `gta-clips run` every `runtime.ready_every_minutes`, 30 by default) copies recent clips of each account set into `ready-to-post/<day>/<set name>/` inside the storage directory. Nothing is uploaded: you watch each clip and post it yourself.

- Each set gets up to `runtime.ready_per_day` clips a day (6 by default), spread through the day: one after midnight, then one more every 24 / `ready_per_day` hours.
- Rejected clips, clips you rated 1 or 2, and clips older than three days are left out.
- The order comes from real post numbers, not hand-picked weights (see below). Until enough posts have views, clips are picked at random so the first posts are a fair sample.
- Each video has a `.txt` beside it with the title, description and hashtags, and the set's `youtube`, `tiktok`, `instagram` and `email` names when they are filled in. Never put passwords in the config.
- The videos are copies; the originals stay in `clips/`. Day folders older than a week are removed.

## Clip stats and views

`gta-clips stats` (run every `runtime.stats_every_minutes`, 60 by default) records facts about each clip and how its posts did.

- **Per clip** (`clip_stats`): integrated loudness, loudness range, true peak, the loudest moment and when it happens, when sound starts, how much is silent, scene cuts, chat messages during, before and after the clip, the busiest chat second, emotes, cheers and bits, and the streamer's partner/affiliate status and channel age. Rendering also records when the first and last words are spoken. Stream title, tags and the mature flag are saved with each chat window.
- **Per post** (`posts`, `post_stats`): posts made by the publisher or the browser uploader are found automatically. For a clip posted by hand, record its link with `gta-clips posted <Clip ID> <link>` (the Clip ID is in the clip's `.txt`). YouTube views, likes and comments come from the YouTube Data API, and average watch time and average % watched from the YouTube Analytics API, as snapshots every 6 hours for 60 days.
- **Views model**: once 30 YouTube posts are at least two days old, a ridge regression is fitted from those clips' stats to the log of their views, and the ready-to-post folder offers clips in order of predicted views. One pick in five stays random so the model keeps getting tested on clips it would not choose. `data/view-model.json` shows how many posts it used and which stats weigh most. Its fit is measured on the same posts it learned from, so it overstates accuracy.
- Reading views needs the read-only YouTube scopes. Logins made before they were added can only upload: run `gta-clips auth youtube --account NAME` again for each set. TikTok and Instagram numbers are not read yet.

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
| `runner.py`, `dashboard.py`, and `settings_edit.py` | Run workers on the `[runtime]` schedule, serve the local dashboard, and save setting changes. |
# Optional browser uploads

For uploading ready-folder clips without API keys using a dedicated browser login, see [BROWSER-UPLOAD.md](BROWSER-UPLOAD.md). Includes YouTube, TikTok and Instagram adapters, a local duplicate ledger, and posting limits. Live validation of the new browser adapters is still required.
