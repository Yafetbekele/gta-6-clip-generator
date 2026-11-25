"""Export view-tracking records to a local, readable folder without network calls."""
import argparse
from datetime import datetime
from html import escape
import json
from pathlib import Path
import sqlite3
import tomllib

from .db import worker_lock


def read_table(path, table):
    if not path.is_file():
        return []
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=20)
    db.row_factory = sqlite3.Row
    try:
        if not db.execute('SELECT 1 FROM sqlite_master WHERE type=? AND name=?', ('table', table)).fetchone():
            return []
        # Table names are constants supplied by this module, never user input.
        return [dict(row) for row in db.execute(f'SELECT * FROM "{table}"')]
    finally:
        db.close()


def write_file(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(value, encoding='utf-8')
    temporary.replace(path)


PLATFORM_NAMES = {'youtube': 'YouTube', 'instagram': 'Instagram', 'tiktok': 'TikTok'}


def banned_platforms(options):
    """Entries marked `banned` in config.toml: (account, platform) for a whole account, or
    (account, platform, handle) for one earlier handle. An unreadable file marks none."""
    try:
        accounts = tomllib.loads(options.read_text(encoding='utf-8-sig')).get('accounts', [])
    except (OSError, tomllib.TOMLDecodeError):
        return set()
    banned = set()
    for account in accounts:
        if not isinstance(account, dict):
            continue
        for entry in account.get('banned', []):
            if isinstance(entry, str):
                platform, _, handle = entry.strip().partition(':')
                key = (account.get('name'), platform) + ((handle.lstrip('@').lower(),) if handle else ())
                banned.add(key)
    return banned


def post_handle(url):
    """The account handle in an Instagram or TikTok post link, or None."""
    import re
    match = re.search(r'(?:instagram\.com|tiktok\.com)/@?([\w.]+)/(?:reel|reels|p|video)/', url or '')
    return match.group(1).lower() if match else None


def row_is_banned(row, banned):
    account, platform = row.get('account'), row.get('platform')
    return (account, platform) in banned or (account, platform, row.get('handle')) in banned


def reel_code(url):
    """The shortcode in an Instagram post link, or None."""
    import re
    match = re.search(r'instagram\.com/(?:[\w.]+/)?(?:reel|reels|p)/([\w-]+)', url or '')
    return match.group(1) if match else None


def merge_same_reel(rows):
    """One row per Instagram Reel when the publisher and the browser collector both recorded it.

    The row with a clip (the publisher's) is kept; it takes the newer numbers of the two.
    """
    groups = {}
    for row in rows:
        row = {**row, 'handle': row.get('handle') or post_handle(row.get('url'))}
        code = reel_code(row.get('url')) if row.get('platform') == 'instagram' else None
        groups.setdefault(code or id(row), []).append(row)
    merged = []
    for group in groups.values():
        if len(group) == 1:
            merged.append(group[0])
            continue
        keep = dict(max(group, key=lambda r: (r.get('clip_id') is not None, r.get('likes') is not None)))
        newest = max(group, key=lambda r: r.get('fetched') or 0)
        for key in ('views', 'likes', 'comments', 'shares'):
            if newest.get(key) is not None and (newest.get('fetched') or 0) >= (keep.get('fetched') or 0):
                keep[key] = newest[key]
            elif keep.get(key) is None:
                keep[key] = next((r[key] for r in group if r.get(key) is not None), None)
        keep['fetched'] = newest.get('fetched')
        # The publisher's link has no handle; the collector's says which account the Reel was on.
        keep['handle'] = next((r['handle'] for r in group if r.get('handle')), None)
        merged.append(keep)
    return merged


def cell(value):
    return escape(str(value)) if value is not None else '—'


def number(value):
    return f'{value:,}' if isinstance(value, int) else '—'


def sort_value(value):
    return value if isinstance(value, (int, float)) else -1


def link(url):
    from urllib.parse import urlsplit
    host = urlsplit(url or '').hostname or ''
    allowed = host in ('youtube.com', 'www.youtube.com', 'youtu.be', 'tiktok.com', 'www.tiktok.com',
                       'instagram.com', 'www.instagram.com')
    return (f'<a href="{escape(url, quote=True)}" target="_blank" rel="noopener">Open</a>'
            if allowed and url.startswith('https://') else '<span class="muted">No link</span>')


def when(timestamp):
    if not isinstance(timestamp, (int, float)):
        return '—'
    try:
        return datetime.fromtimestamp(timestamp).astimezone().strftime('%b %d, %H:%M')
    except (OSError, OverflowError, ValueError):
        return '—'


STYLE = """
:root{--bg:#0f1420;--panel:#171e2e;--line:#2a3448;--text:#e6e9f0;--muted:#8d97ab;--link:#8fb8ff;--accent:#6ee7b7;--bad:#f87171;--warn:#fbbf24}
@media (prefers-color-scheme: light){:root{--bg:#f6f7fb;--panel:#fff;--line:#e1e5ee;--text:#151a26;--muted:#5d6678;--link:#2457c5;--accent:#0f8a5f;--bad:#c62828;--warn:#a86400}}
*{box-sizing:border-box}body{font:15px/1.45 system-ui,"Segoe UI",sans-serif;margin:0;background:var(--bg);color:var(--text)}
main{max-width:1180px;margin:0 auto;padding:24px 16px 48px}h1{font-size:26px;margin:0 0 4px}h2{font-size:18px;margin:32px 0 10px}
a{color:var(--link)}.muted{color:var(--muted)}.stale{color:var(--warn);font-weight:600}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-top:18px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.card b{display:block;font-size:26px;font-variant-numeric:tabular-nums}.card span{color:var(--muted);font-size:13px}
.table{overflow-x:auto;background:var(--panel);border:1px solid var(--line);border-radius:10px}
table{border-collapse:collapse;width:100%}td,th{padding:9px 12px;text-align:left;border-bottom:1px solid var(--line);white-space:nowrap}
tbody tr:last-child td{border-bottom:0}th{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);font-weight:600}
th[data-sort]{cursor:pointer;user-select:none}th[data-sort]:hover{color:var(--text)}
th[data-dir=desc]::after{content:" \\25BE"}th[data-dir=asc]::after{content:" \\25B4"}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}td.title{white-space:normal;min-width:240px}
td.title small{display:block;color:var(--muted);font-size:12px;overflow-wrap:anywhere}
tr.banned td{color:var(--muted)}.badge{display:inline-block;font-size:11px;font-weight:700;padding:1px 7px;border-radius:99px;border:1px solid currentColor;margin-left:6px}
.badge.bad{color:var(--bad)}.filters{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 10px}
.filters button{font:inherit;font-size:13px;padding:5px 12px;border-radius:99px;border:1px solid var(--line);background:var(--panel);color:var(--text);cursor:pointer}
.filters button[aria-pressed=true]{border-color:var(--accent);color:var(--accent)}
details{margin-top:28px;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 16px}
summary{cursor:pointer;font-weight:600}details ul{margin:10px 0 0;padding-left:20px}details .table{margin-top:10px;border:0}
"""

SCRIPT = """
document.querySelectorAll('[data-updated]').forEach(el => {
  const t = +el.dataset.updated * 1000;
  const tick = () => {
    const m = Math.round((Date.now() - t) / 60000);
    el.textContent = m < 1 ? 'just now' : m < 60 ? m + ' min ago' : Math.round(m / 6) / 10 + ' hours ago';
    el.classList.toggle('stale', m >= 180);
  };
  tick(); setInterval(tick, 30000);
});
document.querySelectorAll('table[data-sortable]').forEach(table => {
  table.querySelectorAll('th[data-sort]').forEach(th => th.addEventListener('click', () => {
    const col = [...th.parentNode.children].indexOf(th);
    const down = th.dataset.dir !== 'desc';
    th.parentNode.querySelectorAll('th').forEach(h => delete h.dataset.dir);
    th.dataset.dir = down ? 'desc' : 'asc';
    const value = row => row.cells[col].dataset.v ?? row.cells[col].textContent.trim();
    const rows = [...table.tBodies[0].rows].sort((a, b) => {
      const x = value(a), y = value(b), nx = parseFloat(x), ny = parseFloat(y);
      const r = (isNaN(nx) || isNaN(ny)) ? String(x).localeCompare(String(y)) : nx - ny;
      return down ? -r : r;
    });
    rows.forEach(row => table.tBodies[0].appendChild(row));
  }));
});
document.querySelectorAll('.filters').forEach(bar => bar.addEventListener('click', event => {
  const button = event.target.closest('button');
  if (!button) return;
  bar.querySelectorAll('button').forEach(b => b.setAttribute('aria-pressed', b === button));
  const want = button.dataset.platform;
  document.querySelectorAll('#posts tbody tr[data-platform]').forEach(row => {
    row.hidden = want !== 'all' && row.dataset.platform !== want;
  });
}));
"""


def render_page(merged, attempts, checks, banned, titles, counts):
    """The overview page: totals, one row per channel, then every post with the most viewed first.

    Posts on banned accounts are left off the page entirely; the JSON downloads still have them.
    """
    hidden = sum(1 for row in merged if row_is_banned(row, banned))
    merged = live = [row for row in merged if not row_is_banned(row, banned)]
    channels = {}
    for row in merged:
        channels.setdefault((row.get('account') or '', row.get('platform') or ''), []).append(row)

    cards = ''
    for platform, name in PLATFORM_NAMES.items():
        rows = [row for row in live if row.get('platform') == platform]
        if rows:
            views = sum(row['views'] for row in rows if isinstance(row.get('views'), int))
            cards += f'<div class="card"><span>{name} views</span><b>{views:,}</b><span>{len(rows)} posts</span></div>'
    best = max((row for row in live if isinstance(row.get('views'), int)), key=lambda row: row['views'], default=None)
    if best:
        cards += (f'<div class="card"><span>Best post</span><b>{best["views"]:,}</b>'
                  f'<span>{cell(best.get("account"))} · {PLATFORM_NAMES.get(best.get("platform"), cell(best.get("platform")))}</span></div>')

    def channel_views(item):
        return sum(r['views'] for r in item[1] if isinstance(r.get('views'), int))

    channel_rows = ''
    for (account, platform), rows in sorted(channels.items(), key=lambda item: (item[0] in banned, -channel_views(item))):
        measured = [r['views'] for r in rows if isinstance(r.get('views'), int)]
        last = max((r['fetched'] for r in rows if isinstance(r.get('fetched'), (int, float))), default=None)
        is_banned = (account, platform) in banned
        badge = '<span class="badge bad">Banned</span>' if is_banned else ''
        channel_rows += (f'<tr{" class=banned" if is_banned else ""}><td>{cell(account)}{badge}</td>'
                         f'<td>{PLATFORM_NAMES.get(platform, cell(platform))}</td><td class="num">{len(rows)}</td>'
                         f'<td class="num" data-v="{sum(measured)}">{number(sum(measured)) if measured else "—"}</td>'
                         f'<td class="num" data-v="{max(measured, default=-1)}">{number(max(measured)) if measured else "—"}</td>'
                         f'<td data-v="{last or 0}">{when(last)}</td></tr>')

    post_rows = ''
    for row in sorted(merged, key=lambda r: sort_value(r.get('views')), reverse=True):
        account, platform = row.get('account'), row.get('platform')
        is_banned = (account, platform) in banned
        badge = '<span class="badge bad">Banned</span>' if is_banned else ''
        title = titles.get(row.get('clip_id')) or ''
        numbers = ''.join(f'<td class="num" data-v="{sort_value(row.get(key))}">{number(row.get(key))}</td>'
                          for key in ('views', 'likes', 'comments'))
        post_rows += (f'<tr data-platform="{escape(str(platform), quote=True)}"{" class=banned" if is_banned else ""}>'
                      f'<td>{cell(account)}{badge}</td><td>{PLATFORM_NAMES.get(platform, cell(platform))}</td>'
                      f'<td class="title">{escape(title) if title else "<span class=muted>Clip not matched</span>"}'
                      f'<small>{cell(row.get("clip_id"))}</small></td>'
                      f'<td data-v="{sort_value(row.get("published"))}">{when(row.get("published"))}</td>'
                      f'{numbers}<td>{cell(row.get("privacy"))}</td><td>{link(row.get("url"))}</td></tr>')

    upload_rows = ''.join(
        f'<tr><td>{cell(row.get("account"))}</td><td>{PLATFORM_NAMES.get(row.get("platform"), cell(row.get("platform")))}</td>'
        f'<td>{cell(row.get("clip_id"))}</td><td>{cell(row.get("state"))}</td><td>{link(row.get("url"))}</td></tr>'
        for row in attempts
        if not row_is_banned({**row, 'handle': post_handle(row.get('url'))}, banned))
    platforms = [p for p in PLATFORM_NAMES if any(row.get('platform') == p for row in merged)]
    filters = '<button data-platform="all" aria-pressed="true">All</button>' + ''.join(
        f'<button data-platform="{p}" aria-pressed="false">{PLATFORM_NAMES[p]}</button>' for p in platforms)
    downloads = ' · '.join(f'<a href="{name}.json">{label}</a>' for name, label in [
        ('posts', 'Latest post metrics'), ('view-history', 'All view snapshots'), ('clip-features', 'Clip features'),
        ('upload-history', 'Upload records'), ('model', 'Ranking model'), ('collection-status', 'Collector status'),
        ('post-observations', 'Privacy and review history')])
    now = datetime.now().astimezone()
    posts, snapshots, measured_clips, uploads = counts
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1"><meta http-equiv="refresh" content="300">'
            f'<title>Clip views</title><style>{STYLE}</style></head><body><main>'
            '<h1>Clip views</h1>'
            f'<p class="muted">Updated {now.strftime("%b %d, %H:%M")} (<span data-updated="{int(now.timestamp())}">just now</span>). '
            f'{posts} posts · {snapshots} snapshots · {measured_clips} measured clips. A dash means not measured yet, not zero.'
            + (f' {hidden} posts on banned accounts are hidden.' if hidden else '') + '</p>'
            f'<div class="cards">{cards}</div>'
            '<h2>By channel</h2><div class="table"><table data-sortable><thead><tr>'
            '<th data-sort>Set</th><th data-sort>Platform</th><th data-sort class="num">Posts</th>'
            '<th data-sort class="num">Views</th><th data-sort class="num">Best</th><th data-sort>Last measured</th>'
            f'</tr></thead><tbody>{channel_rows}</tbody></table></div>'
            f'<h2>Posts</h2><div class="filters">{filters}</div>'
            '<div class="table"><table id="posts" data-sortable><thead><tr>'
            '<th data-sort>Set</th><th data-sort>Platform</th><th data-sort>Clip</th><th data-sort>Posted</th>'
            '<th data-sort class="num">Views</th><th data-sort class="num">Likes</th><th data-sort class="num">Comments</th>'
            '<th data-sort>Privacy</th><th>Link</th></tr></thead><tbody>'
            + (post_rows or '<tr><td colspan="9">No linked posts imported by the stats worker yet.</td></tr>')
            + '</tbody></table></div>'
            '<details><summary>Collector status</summary><ul>'
            + ''.join(f'<li>{cell(row.get("account"))} / {cell(row.get("platform"))}: {cell(row.get("detail"))}</li>'
                      for row in checks)
            + f'</ul><p>Downloads: {downloads}</p></details>'
            f'<details><summary>All upload records ({uploads})</summary><div class="table"><table><thead><tr>'
            '<th>Set</th><th>Platform</th><th>Clip</th><th>Status</th><th>Link</th></tr></thead><tbody>'
            f'{upload_rows}</tbody></table></div></details>'
            f'</main><script>{SCRIPT}</script></body></html>')


def export_tracking(settings):
    settings = settings.resolve()
    config = json.loads(settings.read_text(encoding='utf-8-sig'))
    options = settings.parent / 'config.toml'
    data = 'data'
    if options.is_file():
        data = tomllib.loads(options.read_text(encoding='utf-8-sig')).get('paths', {}).get('data_dir', data)
    data = (settings.parent / Path(data).expanduser()).resolve()
    state = (settings.parent / config.get('state_dir', 'data/browser-upload')).resolve()
    folder = data / 'view-tracking'
    with worker_lock(folder, 'export'):
        posts = read_table(data / 'state.sqlite3', 'posts')
        history = read_table(data / 'state.sqlite3', 'post_stats')
        features = read_table(data / 'state.sqlite3', 'clip_stats')
        attempts = read_table(state / 'posts.sqlite3', 'posts')
        checks = read_table(state / 'posts.sqlite3', 'metric_collection_checks')
        keys = {(r['platform'], r['account']) for r in checks}
        checks += [{**r, 'platform': 'youtube'} for r in read_table(state / 'posts.sqlite3', 'browser_metric_checks')
                   if ('youtube', r['account']) not in keys]
        observations = read_table(data / 'state.sqlite3', 'post_observations')
        latest = {}
        for row in history:
            key = (row['platform'], row['video_id'])
            if key not in latest or row['fetched'] > latest[key]['fetched']:
                latest[key] = row
        observed = {}
        for row in observations:
            key = (row['platform'], row['video_id'])
            if key not in observed or row['fetched'] > observed[key]['fetched']:
                observed[key] = row
        merged = [{**post, **latest.get((post['platform'], post['video_id']), {}),
                   'privacy': observed.get((post['platform'], post['video_id']), {}).get('privacy'),
                   'post_status': observed.get((post['platform'], post['video_id']), {}).get('status')} for post in posts]
        merged = merge_same_reel(merged)
        for name, rows in [('posts', merged), ('view-history', history), ('clip-features', features), ('upload-history', attempts)]:
            write_file(folder / (name + '.json'), json.dumps(rows, indent=2, ensure_ascii=False))
        write_file(folder / 'collection-status.json', json.dumps(checks, indent=2))
        write_file(folder / 'post-observations.json', json.dumps(observations, indent=2, ensure_ascii=False))
        model = data / 'view-model.json'
        write_file(folder / 'model.json', model.read_text(encoding='utf-8') if model.is_file() else json.dumps({'status': 'No saved model summary found'}))
        titles = {row['id']: row.get('title') for row in read_table(data / 'state.sqlite3', 'clips')}
        html = render_page(merged, attempts, checks, banned_platforms(options), titles,
                           (len(posts), len(history), len(features), len(attempts)))
        write_file(folder / 'index.html', html)
    return folder


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--settings', type=Path, default=Path('browser-upload.json'))
    print(export_tracking(parser.parse_args().settings))
