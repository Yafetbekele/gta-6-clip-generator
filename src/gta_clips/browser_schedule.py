"""Local hourly browser posting: no AI calls and no catch-up bursts."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import subprocess
import sys
import time
from pathlib import Path

from .browser_upload import PLATFORMS, exists, ledger, scan, settings
from .db import worker_lock


def readiness(db, account, platform, target, now):
    if target.get('auto_post') is not True:
        return 'disabled'
    rows = db.execute('SELECT state,started FROM posts WHERE account=? AND platform=?',
                      (account, platform)).fetchall()
    # Held clips remain excluded by the upload ledger, but do not stop this account.
    if not any(r['state'] in ('posted', 'under_review') for r in rows):
        return 'needs successful test upload'
    if rows and now - max(r['started'] for r in rows) < 3600:
        return 'waiting for hourly interval'
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
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--settings', type=Path, default=Path('browser-upload.json'))
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    print(json.dumps(tick(args.settings, args.dry_run), indent=2))


if __name__ == '__main__':
    main()
