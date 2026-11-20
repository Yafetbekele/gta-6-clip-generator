"""Local account overview; login status is the last check, not a live probe."""
import json
import time
from datetime import datetime

from .browser_upload import ledger
from .browser_schedule import readiness


def account_status(path):
    config = json.loads(path.read_text(encoding='utf-8-sig'))
    state = (path.resolve().parent / config.get('state_dir', 'data/browser-upload')).resolve()
    db = ledger(state / 'posts.sqlite3')
    rows = []
    try:
        names = sorted(config['accounts'], key=lambda n: (0, n) if n.startswith('gta6-') else (1, n))
        for account in names:
            for platform, target in config['accounts'][account].items():
                identity = target.get('channel_id' if platform == 'youtube' else 'handle', '').lstrip('@').lower()
                check = db.execute('SELECT * FROM login_checks WHERE account=? AND platform=?', (account, platform)).fetchone()
                login = 'Not checked'
                if check and check['identity'] == identity:
                    when = datetime.fromtimestamp(check['checked']).strftime('%m/%d %H:%M')
                    login = ('Verified ' if check['verified'] else 'Check failed ') + when
                elif check:
                    login = 'Recheck: identity changed'
                counts = dict(db.execute('SELECT state,COUNT(*) FROM posts WHERE account=? AND platform=? GROUP BY state', (account, platform)).fetchall())
                posted = counts.get('posted', 0)
                review = counts.get('under_review', 0)
                rows.append({
                    'account': account, 'platform': platform,
                    'enabled': target.get('auto_post') is True, 'login': login,
                    'posted': posted, 'under_review': review,
                    'status': readiness(db, account, platform, target, time.time()),
                })
    finally:
        db.close()
    return rows


def display_status(path):
    print('\nALL ACCOUNTS STATUS')
    print(f'{"Account":<13} {"Platform":<10} {"Auto":<5} {"Login (last check)":<31} {"Posts / review":<16} Scheduler')
    print('-' * 115)
    for row in account_status(path):
        print(f'{row["account"]:<13} {row["platform"]:<10} {"ON" if row["enabled"] else "OFF":<5} '
              f'{row["login"]:<31} {str(row["posted"]) + " / " + str(row["under_review"]):<16} {row["status"]}')
    print('\nLogin reflects the last identity verification, not a fresh online check.')
    print('Use verify-login to refresh it. Posting also updates the login check.')
    print('Posts / review counts confirmed posts and accepted submissions under review separately.')
