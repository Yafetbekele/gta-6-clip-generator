# Browser uploader: YouTube, TikTok and Instagram

## Hourly posting across account groups

`browser-upload.json` can contain ten account groups with three platform destinations each. `account_groups` stores organization labels and email notes only; never store passwords there. Missing channel IDs and handles must be filled before login. The live local configuration has 30 slots, but only the tested GTA 6 Clips 2 YouTube destination is enabled.

Set `auto_post: true` on each platform destination after login, verification, and a successful test upload. The scheduler requires a `posted` or `under_review` ledger record. Failed or canceled uploads stay `held` and are skipped individually; the account continues with another eligible clip at its next hourly interval. Other accounts continue independently. Files in the ready folder are the publication queue. An empty queue waits; missed hours do not cause catch-up batches.

Run `install-hourly-uploads.ps1` in PowerShell to register **Twitch Clips Hourly Browser Uploads**. It checks every five minutes, uploads at most one per destination per pass and enforces at least 60 minutes since the last upload attempt, including manual uploads. There is no additional daily cap. Chrome windows may appear during uploads. Keep Windows signed in, the computer awake, and dedicated uploader profiles closed. Nothing runs while the computer is off. Disable the named task in Windows Task Scheduler to stop automatic posting.

Check readiness without posting:

```powershell
.\.venv\Scripts\python.exe -m gta_clips.browser_schedule --dry-run
```

Local results are in `data/browser-upload/hourly-status.json` and `hourly-uploads.log`. Google Docs is an organizational tracker only; edits there do not automatically change uploader settings. Manual `publish one` remains available without the hourly restriction unless you explicitly configure one.

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

The launcher stays open after actions and handled errors; press Enter to return to the menu or enter `q` at a numbered menu to quit. Choose **ALL ACCOUNTS STATUS** from the account menu, or action **9 — all accounts status**, to view all destinations. It shows hourly posting ON/OFF, the last login verification with its timestamp, confirmed post and under-review counts, and scheduler eligibility. Login checks are saved by verify-login and posting, keyed to the configured identity. Existing sessions initially show Not checked until their next verification; changing the identity requires a fresh check. The overview does not open browsers or test current sign-ins automatically.

View-tracking reports are exported to `data/view-tracking/index.html` after each scheduler cycle. The Desktop `Twitch Clips/View Tracking` folder links to those reports. They include latest linked-post metrics, all metric snapshots, measured clip features, upload records, and the saved model summary as JSON downloads. Refresh manually with `.venv\Scripts\python.exe -m gta_clips.view_tracking --settings browser-upload.json`. Exporting reads existing local data only; collecting new view metrics remains the stats worker's responsibility. Blank metrics mean unknown, not zero. Original databases stay in place.

The hourly browser scheduler also checks YouTube Studio, TikTok Studio Posts, and Instagram Reels metrics when due, using existing saved Chrome profiles without API credentials or AI calls. Exact displayed counts are stored as timestamped history in `data/state.sqlite3` (`post_stats`). TikTok includes views/likes/comments plus privacy and review/date labels in `post_observations`; Instagram reads the Reel grid's view counter and leaves unavailable engagement metrics unknown. Displayed zero is stored as zero. Rounded K/M numbers, missing metrics, and unrecognized layouts are never guessed. Post links are saved automatically; clips are matched by known remote ID or a unique complete title plus exact source-VOD timestamp in the caption. Unmatched posts retain their counts and links with a blank clip ID. Existing clip associations are preserved.

Both YouTube legacy tabs and the newer Content → View all layout are supported. Successful destinations refresh about every six hours; failed checks retry after 30 minutes, and busy profiles wait until the next cycle. Upload/profile locks are shared with posting. Confirmed upload completion time is used as a conservative age estimate for the learning model; it is not an exact platform publication timestamp. The learning model currently trains on YouTube views only. The tracking overview exposes privacy/review labels and collector status separately from view counts.

Run a read-only collection manually with `.venv\Scripts\python.exe -m gta_clips.browser_metrics --settings browser-upload.json` (optional `--account gta6-1 --platform instagram --force`). Collector status is shown in View Tracking and `collection-status.json`; privacy/review history is in `post-observations.json`. Existing API-based stats collection can still report missing API authorization; that does not prevent browser collection.

Used ready copies and original MP4s are cleaned after successful uploads and scheduled cycles. The ready MP4 and matching TXT, plus the original video referenced by the clip database, are deleted only once every destination with a configured channel ID/handle for that set has a confirmed `posted` or `under_review` ledger record. Disabled destinations with saved identities still count, so their pending clips are retained. Failed, canceled, private, or uncertain attempts do not qualify. Original cleanup is restricted to the resolved `data/clips` directory; shared source files require every referencing clip to qualify. Tracking, metadata, ratings, and duplicate-prevention database records remain intact. Deleted videos will no longer be playable in review.

```powershell
.\.venv\Scripts\python.exe -m gta_clips.browser_upload login --platform youtube --account gta6-2
.\.venv\Scripts\python.exe -m gta_clips.browser_upload login --platform tiktok --account gta6-2
.\.venv\Scripts\python.exe -m gta_clips.browser_upload login --platform instagram --account gta6-2
```

Login now opens **installed Google Chrome normally**, using a dedicated profile under `data/browser-upload/chrome-profiles/PLATFORM/ACCOUNT`. Sign in manually and select the intended channel, then close that dedicated Chrome window completely. Choose **5 — verify-login** in the launcher next (or run the command below). Verification reopens the same profile with Playwright using installed Chrome and checks the account without uploading. Google may still block or reverify that session; this is a test, not a guaranteed fix. No security settings or automation-detection flags are changed. Existing personal Chrome logins are not copied or extracted. Old bundled Chromium profiles are left untouched.

```powershell
.\.venv\Scripts\python.exe -m gta_clips.browser_upload verify-login --platform youtube --account gta6-2
```

Use English site UI. The program checks the Studio channel ID or the site's authenticated Profile link on every upload. If the site doesn't expose that link, it stops; the adapter needs an update. Keep the dedicated profile closed whenever running verification or uploads; do not close your unrelated personal Chrome windows.

## Preview and publish

```powershell
.\.venv\Scripts\python.exe -m gta_clips.browser_upload preview --platform youtube --account gta6-2
.\.venv\Scripts\python.exe -m gta_clips.browser_upload run --platform youtube --account gta6-2 --publish --max 1
```

Substitute `tiktok` or `instagram` to post there. Preview sends nothing. YouTube without `--publish` requests a private upload; TikTok and Instagram require `--publish`. Captions contain only the Title, Description and Hashtags sections, never the sign-in email or account notes. Streamer credit links are required; oversized captions are rejected rather than cutting credits off. Videos are not re-encoded. Instagram selects Original crop to preserve the full frame.

Defaults: no daily cap and no hourly spacing, at most one per command. Missing or null daily_limit means unlimited; spacing_minutes defaults to 0. Optional numeric limits can still be configured. `daily_limit: 0` disables uploads. `--max` does not override spacing or daily limits. Run again when due. No background process is installed automatically. In Windows Task Scheduler, run the command hourly **only when you are logged on**, with the project as the Start in folder. The hourly scheduler runs up to two uploads concurrently. Separate account/platform profile locks prevent overlapping use of the same Chrome profile; two shared upload slots also enforce the limit for manual runs. A third manual upload stops with a busy message; retry when a slot is free. Each scheduled destination writes to data/browser-upload/hourly-PLATFORM-ACCOUNT.log. No AI service needs to be running.

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

Launcher option **8 — set channel URL or handle** saves a YouTube Studio channel URL/channel ID or a TikTok/Instagram handle locally. Changing identity disables hourly posting. Login works before identity setup; verification and uploads still require an exact identity.
