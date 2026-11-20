"""Read visible YouTube Studio counts using existing browser logins; no API keys."""
import argparse
import json
from pathlib import Path
import re
import sqlite3
import time
import tomllib

from .browser_upload import chrome_profile, ledger, upload_slot, verify_saved_account
from .db import worker_lock, WorkerBusy


def exact_count(text):
    """Store exact displayed counts; rounded K/M values and dashes remain unknown."""
    text = (text or '').strip().replace('\u00a0', '').replace('\u202f', '')
    if re.fullmatch(r'\d{1,3}(?:,\d{3})+|\d+', text):
        return int(text.replace(',', ''))
    return None


def video_id(url):
    match = re.search(r'(?:/video/|/shorts/|youtu\.be/|[?&]v=)([A-Za-z0-9_-]{11})(?:[/?&]|$)', url or '')
    return match.group(1) if match else None


def collect_rows(page):
    # Some Studio layouts use a navigation button instead of an anchor.
    page.get_by_text('Content', exact=True).click()
    shorts = page.get_by_role('tab', name='Shorts', exact=True)
    view_all = page.get_by_text('View all', exact=True).first
    shorts.or_(view_all).first.wait_for(state='visible')
    if shorts.is_visible():
        shorts.click()
    else:
        view_all.click()
    page.locator('ytcp-video-row').first.wait_for(state='visible')
    seen = set()
    results = []
    for _ in range(20):
        rows = page.locator('ytcp-video-row').evaluate_all('''els => els.map(e => ({
            href:e.querySelector('a#video-title')?.getAttribute('href'),
            title:e.querySelector('a#video-title')?.getAttribute('aria-label'),
            views:e.querySelector('.tablecell-views')?.innerText,
            comments:e.querySelector('.tablecell-comments')?.innerText,
            visibility:e.querySelector('.tablecell-visibility')?.innerText
        }))''')
        signature = tuple(row.get('href') for row in rows)
        if not rows or signature in seen:
            break
        seen.add(signature)
        results.extend(rows)
        next_page = page.get_by_role('button', name='Next page', exact=True)
        if next_page.count() != 1 or not next_page.is_enabled():
            break
        first_href = rows[0]['href']
        next_page.click()
        page.wait_for_function("old => document.querySelector('ytcp-video-row a#video-title')?.getAttribute('href') !== old", arg=first_href)
    return results


def store_rows(db, account, known, rows, fetched):
    saved = 0
    for row in rows:
        remote = video_id(row.get('href'))
        post = known.get(remote)
        views = exact_count(row.get('views'))
        # Exact remote IDs only: no guesses from similar or truncated titles.
        if not post or views is None:
            continue
        db.execute('''INSERT INTO posts(platform,video_id,clip_id,account,url,source)
            VALUES('youtube',?,?,?,?, 'browser-studio') ON CONFLICT(platform,video_id) DO NOTHING''',
                   (remote, post.get('clip_id'), account, 'https://youtube.com/shorts/' + remote))
        inserted = db.execute('''INSERT OR IGNORE INTO post_stats(platform,video_id,fetched,published,views,comments)
            VALUES('youtube',?,?,?,?,?)''',
                   (remote, int(fetched), post.get('published'), views, exact_count(row.get('comments'))))
        saved += inserted.rowcount
    return saved


def collect(settings, force=False, only_account=None):
    settings = settings.resolve()
    config = json.loads(settings.read_text(encoding='utf-8-sig'))
    options = settings.parent / 'config.toml'
    data = settings.parent / 'data'
    if options.is_file():
        data = (settings.parent / tomllib.loads(options.read_text(encoding='utf-8-sig')).get('paths', {}).get('data_dir', 'data')).resolve()
    state = (settings.parent / config.get('state_dir', 'data/browser-upload')).resolve()
    db = sqlite3.connect(data / 'state.sqlite3', timeout=30)
    db.row_factory = sqlite3.Row
    upload_db = ledger(state / 'posts.sqlite3')
    results = []
    try:
        db.executescript('''CREATE TABLE IF NOT EXISTS posts (
            platform TEXT, video_id TEXT, clip_id TEXT, account TEXT, url TEXT, source TEXT,
            added INTEGER DEFAULT (unixepoch()), PRIMARY KEY(platform,video_id));
            CREATE TABLE IF NOT EXISTS post_stats (
            platform TEXT,video_id TEXT,fetched INTEGER,published REAL,views INTEGER,likes INTEGER,
            comments INTEGER,shares INTEGER,avg_view_seconds REAL,avg_view_percent REAL,subscribers_gained INTEGER,
            PRIMARY KEY(platform,video_id,fetched));''')
        upload_db.execute('''CREATE TABLE IF NOT EXISTS browser_metric_checks (
            account TEXT PRIMARY KEY, checked REAL, successful INTEGER, detail TEXT)''')
        upload_db.commit()
        from playwright.sync_api import sync_playwright
        with worker_lock(state, 'browser-metrics'), sync_playwright() as pw:
            for account, targets in config['accounts'].items():
                if only_account and account != only_account:
                    continue
                target = targets.get('youtube', {})
                if not target.get('channel_id') or not chrome_profile(state, 'youtube', account).is_dir():
                    continue
                previous = upload_db.execute('SELECT checked,successful FROM browser_metric_checks WHERE account=?', (account,)).fetchone()
                if not force and previous and time.time() - previous['checked'] < (21600 if previous['successful'] else 1800):
                    continue
                known = {row['video_id']: {'clip_id': row['clip_id'], 'published': None} for row in
                         db.execute("SELECT * FROM posts WHERE platform='youtube' AND account=?", (account,))}
                for row in upload_db.execute("SELECT * FROM posts WHERE platform='youtube' AND account=? AND state='posted'", (account,)):
                    remote = video_id(row['url'])
                    if remote:
                        # Confirmed submission time is a conservative age estimate for training.
                        known[remote] = {'clip_id': row['clip_id'], 'published': row['finished']}
                if not known:
                    continue
                success = False
                stage = 'open browser'
                try:
                    with worker_lock(state, f'browser-upload-youtube-{account}'), upload_slot(state):
                        context = pw.chromium.launch_persistent_context(str(chrome_profile(state, 'youtube', account)),
                            channel='chrome', headless=False, chromium_sandbox=True, locale='en-US',
                            viewport={'width': 1440, 'height': 1000})
                        try:
                            page = context.pages[0] if context.pages else context.new_page()
                            page.set_default_timeout(30000)
                            stage = 'verify login'
                            verify_saved_account(page, 'youtube', account, target, upload_db)
                            stage = 'read Shorts table'
                            try:
                                rows = collect_rows(page)
                            except Exception:
                                try:
                                    page.screenshot(path=str(state / f'metrics-error-{account}.png'))
                                except Exception:
                                    pass
                                raise
                            with db:
                                count = store_rows(db, account, known, rows, time.time())
                            success = count > 0
                            detail = f'Saved {count} YouTube view snapshots'
                        finally:
                            context.close()
                except WorkerBusy:
                    results.append({'account': account, 'status': 'busy; will check next cycle'})
                    continue
                except Exception as exc:
                    detail = f'Metrics check failed at {stage} ({type(exc).__name__}); login or layout needs attention'
                with upload_db:
                    upload_db.execute('INSERT OR REPLACE INTO browser_metric_checks VALUES(?,?,?,?)',
                                      (account, time.time(), int(success), detail))
                results.append({'account': account, 'status': detail})
    finally:
        upload_db.close()
        db.close()
    from .view_tracking import export_tracking
    export_tracking(settings)
    return results


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--settings', type=Path, default=Path('browser-upload.json'))
    parser.add_argument('--account')
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args()
    try:
        print(json.dumps(collect(args.settings, args.force, args.account), indent=2))
    except WorkerBusy:
        print('A metrics collection is already running; it will refresh the reports when finished.')
