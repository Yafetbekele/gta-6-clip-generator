# Normal Chrome YouTube uploader

This replacement runs in the Chrome profile where you install the extension.
It opens its own Studio tab, verifies the target channel ID, posts one clip and
closes only that tab after confirmation. Existing tabs remain untouched.
The computer must remain awake with Chrome running. There are no AI calls.

## Install and review

1. In the intended Chrome profile, open `chrome://extensions`.
2. Enable Developer mode, choose **Load unpacked**, and select
   `chrome-extension-uploader` inside this project.
3. Review Chrome's permissions. `debugger` lets the extension choose the local
   video file and keep its upload worker alive; `scripting` operates the Studio
   form; alarms check the queue; storage retains its pairing key. Website access
   is limited to YouTube/Studio and the local queue. Chrome may show a debugging banner.
   This uploader does not hide automation, read cookies, export sessions or
   disable Chrome's sandbox.
4. From this project, run:

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File .\install-normal-chrome-uploader.ps1
   ```

   This starts a local queue without enabling posting or changing transports.
5. Open `data\browser-upload\chrome-bridge-token.txt`, copy the pairing key,
   and paste it into the extension popup. Do not post the key in chat or Git.
6. Click **Pair and check connection**. The popup must say Connected.
7. After reviewing the extension and confirming the intended Chrome profile,
   run the installer again with `-Activate`. Activation waits for the current
   scheduled upload batch to finish by refusing if it still owns its lock.
8. Click **Start automatic posting** in the extension popup. This is the action
   that allows public posting. Installing or pairing alone never posts.

## Multiple accounts

Only channels accessible from the signed-in Chrome profile can work. Accounts
that were signed into separate uploader profiles are not copied into this one.
Sign into the required accounts yourself, using Google's normal account flow.
In each channel's Studio tab, open the popup, enter the exact public @handle,
and click **Save handle for this Studio channel**. The popup reads Studio's
channel ID and binds that ID to the supplied handle. The uploader can then
select that exact handle in YouTube's ordinary channel picker. A mistaken handle
still cannot pass the subsequent channel ID check.
The extension opens the configured Studio channel URL and checks Studio's own
home link before sending a file and again before Publish. If Studio opens a
different channel or asks for login, that account pauses for manual attention.
It does not guess account names, enter credentials or publish to a substitute.
Inaccessible channels may require selecting the proper channel yourself and
then using the popup's account-specific Resume button.

## Limits, recovery and history

- Same ready folders, titles, captions, credits and duplicate ledger.
- Maximum three attempts per rolling 24 hours per account. Any stricter saved
  limit and the existing spacing/jitter still apply.
- One active upload across every extension instance connected to this queue.
- Confirmed posts enter the old ledger, so switching transports does not reset
  history. Expired/uncertain uploads remain held, never automatically retried.
- Failed tabs remain open; only the affected account pauses. Inspect Studio
  before resuming. Resume does not erase held duplicate records.
- Closing Chrome, cancelling its debugger banner, or shutting down the computer
  can interrupt an upload. Inspect any draft/public post before taking action.
- Logs: `data/browser-upload/chrome-bridge.log`. Pairing key and state are local.
- The existing hourly task is preserved. It skips YouTube posting when the new
  transport is active. Its existing metrics/cleanup behavior is unchanged.

## Rollback

Stop posting in the extension and wait for any active upload to finish. Then run
the installer with `-Rollback`. The original uploader, profiles, quotas and
posting history remain available. Do not delete a held record without checking
Studio for an existing post first.

## Validation status

Queue tests cover authentication, website-origin rejection, channel mismatches,
changed files, pacing, duplicate history, uncertain outcomes and restart leases.
Full Studio compatibility still requires a live test in the chosen Chrome
profile. A successfully paired extension is not proof that every channel is
signed in or that an upload has completed.

Chrome APIs: [debugger permission and tab attachment](https://developer.chrome.com/docs/extensions/reference/api/debugger),
[service worker lifetime](https://developer.chrome.com/docs/extensions/develop/concepts/service-workers/lifecycle/).
