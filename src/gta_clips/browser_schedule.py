"""Local hourly browser posting: no AI calls and no catch-up bursts."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import hashlib
import json
import logging
import math
import subprocess
import sys
import time
from pathlib import Path

from .browser_upload import PLATFORMS, exists, ledger, scan, settings, account_block
from .db import worker_lock


def posting_spacing(account, platform, target, last_started):
    """Stable per-attempt delay: polling/restarts never redraw the due time."""
    base = max(3600, float(target.get('spacing_minutes', 0)) * 60)
    variation = float(target.get('schedule_jitter_minutes', 0))
    if not math.isfinite(variation) or not 0 <= variation <= 120:
        raise ValueError('schedule_jitter_minutes must be between 0 and 120')
    if not variation:
        return base
    seed = f'{account}/{platform}/{float(last_started).hex()}'.encode()
    offset = int.from_bytes(hashlib.sha256(seed).digest()[:8], 'big') % (int(variation * 60) + 1)
    return base + offset


def readiness(db, account, platform, target, now):
    if target.get('auto_post') is not True:
        return 'disabled'
    blocked = account_block(db, platform, account, target)
    if blocked:
        return 'paused: account verification / appeal required'
    rows = db.execute('SELECT state,started FROM posts WHERE account=? AND platform=?',
                      (account, platform)).fetchall()
    # Held clips remain excluded by the upload ledger, but do not stop this account.
    if not any(r['state'] in ('posted', 'under_review') for r in rows):
        return 'needs successful test upload'
    latest = max(r['started'] for r in rows)
    spacing = posting_spacing(account, platform, target, latest)
    if now - latest < spacing:
        if float(target.get('schedule_jitter_minutes', 0)) > 0:
            due = datetime.fromtimestamp(latest + spacing).strftime('%H:%M')
            return f'waiting for varied interval (next eligible {due})'
        return 'waiting for hourly interval' if spacing == 3600 else f'waiting for {spacing / 60:g}-minute interval'
    return 'ready'


def run_upload(path, state, account, platform):
    # Child rechecks hourly eligibility inside its account/profile lock.
    command = [sys.executable, '-m', 'gta_clips.browser_upload', 'run',
               '--settings', str(path), '--platform', platform,
               '--account', account, '--publish', '--max', '1', '--scheduled']
    try:
        log_path = state / f'hourly-{platform}-{account}.log'
        with log_path.open('a', encoding='utf-8') as log:
            log.write(f'\n{time.time()} {account}/{platform}\n')
            log.flush()
            result = subprocess.run(command, cwd=path.parent, stdout=log, stderr=log)
        return 'run completed' if result.returncode == 0 else f'needs attention: check {log_path.name}'
    except Exception as exc:
        return f'needs attention: {type(exc).__name__}'


def tick(path, dry_run=False):
    path = path.resolve()
    config = json.loads(path.read_text(encoding='utf-8-sig'))
    state = (path.parent / config.get('state_dir', 'data/browser-upload')).resolve()
    root = (path.parent / config['ready_folder']).resolve()
    results = []
    jobs = []
    with worker_lock(state, 'browser-schedule'):
        db = ledger(state / 'posts.sqlite3')
        try:
            for account, targets in config['accounts'].items():
                for platform in PLATFORMS:
                    if platform not in targets:
                        continue
                    target = targets[platform]
                    status = readiness(db, account, platform, target, time.time())
                    if status == 'ready':
                        try:
                            settings(path, account, platform)
                            clips = scan(root, account)
                            if not any(not exists(db, platform, account, c) for c in clips):
                                status = 'waiting for clips'
                            elif not dry_run:
                                jobs.append((account, platform, len(results)))
                                status = 'queued'
                        except Exception as exc:
                            status = f'needs attention: {type(exc).__name__}'
                    results.append({'account': account, 'platform': platform, 'status': status})
        finally:
            db.close()
        # SQLite reads stay on this thread; children own their DB connections.
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [(index, pool.submit(run_upload, path, state, account, platform))
                       for account, platform, index in jobs]
            for index, future in futures:
                results[index]['status'] = future.result()
        if not dry_run:
            temp = state / 'hourly-status.tmp'
            temp.write_text(json.dumps({'updated': time.time(), 'accounts': results}, indent=2), encoding='utf-8')
            temp.replace(state / 'hourly-status.json')
    if not dry_run:
        try:
            from .browser_metrics import collect
            metrics = collect(path)
            for result in metrics:
                logging.info('View collection %s: %s', result['account'], result['status'])
        except Exception as exc:
            logging.warning('Browser view collection deferred (%s)', type(exc).__name__)
        try:
            from .ready_cleanup import cleanup_used
            cleanup_used(config, root, state)
        except Exception as exc:
            logging.warning('Used ready clips not cleaned (%s)', type(exc).__name__)
        try:
            from .view_tracking import export_tracking
            export_tracking(path)
        except Exception as exc:
            logging.warning('View tracking report not refreshed (%s)', type(exc).__name__)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--settings', type=Path, default=Path('browser-upload.json'))
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    print(json.dumps(tick(args.settings, args.dry_run), indent=2))


if __name__ == '__main__':
    main()
