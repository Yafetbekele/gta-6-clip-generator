# Browser uploader: YouTube, TikTok and Instagram

This optional uploader uses Python + Playwright, with no AI calls or API keys. Each platform/account has its own local browser profile. It reads MP4s and their `.txt` metadata in `ready-to-post/YYYY-MM-DD/account/`. Putting a file there makes it eligible; `run --publish` explicitly publishes eligible files. It does not read the review queue's approval status.

**Status:** local parsing, duplicate protection, account mismatch and scheduling tests are covered. Browser flows are initial adapters, not yet validated end-to-end against your signed-in TikTok/Instagram accounts. Website controls can change. A missing control stops the batch; the script does not guess buttons, solve CAPTCHAs, or bypass account restrictions. TikTok requires an active account; a banned account will not work. Instagram posts use that account's existing audience/privacy settings.

## Install (PowerShell, in the project folder)

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[browser]"
.\.venv\Scripts\python.exe -m playwright install chromium
Copy-Item browser-upload.example.json browser-upload.json
```

Edit `browser-upload.json` with each account folder's exact YouTube channel ID or social handle. The example contains the verified GTA 6 Clips 2 YouTube ID and the configured social handles. Confirm those social handles are yours. Paths relative to the settings file resolve beside that file. The settings file and `data/` are gitignored. Never share `data/browser-upload/profiles`: it contains browser login sessions. Use the same Windows user for future runs.

## Connect each account once

For a menu instead of commands, double-click **Browser Uploader.cmd** in the project folder. Pick the account, platform and Preview/Login/Status/Publish one. Publishing requires typing POST. It uses exactly the same ledger and limits as the commands below.

```powershell
.\.venv\Scripts\python.exe -m gta_clips.browser_upload login --platform youtube --account gta6-2
.\.venv\Scripts\python.exe -m gta_clips.browser_upload login --platform tiktok --account gta6-2
.\.venv\Scripts\python.exe -m gta_clips.browser_upload login --platform instagram --account gta6-2
```

Sign in manually in the browser that opens, select the intended channel, then press Enter in the terminal. Normal Chrome logins are not copied or extracted. Use English site UI. The program checks the Studio channel ID or the site's authenticated Profile link on every upload. If the site doesn't expose that link, it stops; the adapter needs an update.

## Preview and publish

```powershell
.\.venv\Scripts\python.exe -m gta_clips.browser_upload preview --platform youtube --account gta6-2
.\.venv\Scripts\python.exe -m gta_clips.browser_upload run --platform youtube --account gta6-2 --publish --max 1
```

Substitute `tiktok` or `instagram` to post there. Preview sends nothing. YouTube without `--publish` requests a private upload; TikTok and Instagram require `--publish`. Captions contain only the Title, Description and Hashtags sections, never the sign-in email or account notes. Streamer credit links are required; oversized captions are rejected rather than cutting credits off. Videos are not re-encoded. Instagram selects Original crop to preserve the full frame.

Defaults: four attempts per rolling 24 hours per destination, at least 60 minutes apart, at most one per command. `daily_limit: 0` disables uploads. `--max` does not override spacing or daily limits. Run again when due. No background process is installed automatically. In Windows Task Scheduler, run the command hourly **only when you are logged on**, with the project as the Start in folder. Use a separate task per platform/account and avoid simultaneous tasks (a shared file lock protects the ledger). No AI service needs to be running.

## Duplicates and recovery

SQLite ledger: `data/browser-upload/posts.sqlite3`. It checks both clip ID and SHA-256, scoped to destination account/platform. Renaming a video does not cause another upload. This ledger is separate from the API scheduler: don't run both upload methods for the same clips without reconciling them.

The ledger records `held` **before** sending a file. A crash, a CAPTCHA, an unexpected dialog, or an unrecognized success screen leaves it held. That means it may already be a draft or live post. It is never retried automatically. Successful runs record `posted` (or `private`) only after a site confirmation. YouTube saves its video link; TikTok/Instagram may confirm success without exposing a link, so check the profile for it.

```powershell
.\.venv\Scripts\python.exe -m gta_clips.browser_upload status --platform youtube --account gta6-2
```

For an already published clip, record its receipt to prevent a duplicate:

```powershell
.\.venv\Scripts\python.exe -m gta_clips.browser_upload record-posted --platform youtube --account gta6-2 --clip-id live-316306402280-1795107360 --url https://youtube.com/shorts/hWMhn5jvMjw
```

For a held clip, inspect the platform first. If already posted, use `record-posted`. Only if it was **not posted**, and any partial draft has been removed manually, release it:

```powershell
.\.venv\Scripts\python.exe -m gta_clips.browser_upload release --platform youtube --account gta6-2 --clip-id CLIP_ID --confirm-not-posted
```

No automatic retries of posting, browser fingerprint changes, proxy rotation, or CAPTCHA workarounds are used. Failed checks require attention. The original files stay in place.
