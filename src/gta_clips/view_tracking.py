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
        for name, rows in [('posts', merged), ('view-history', history), ('clip-features', features), ('upload-history', attempts)]:
            write_file(folder / (name + '.json'), json.dumps(rows, indent=2, ensure_ascii=False))
        write_file(folder / 'collection-status.json', json.dumps(checks, indent=2))
        write_file(folder / 'post-observations.json', json.dumps(observations, indent=2, ensure_ascii=False))
        model = data / 'view-model.json'
        write_file(folder / 'model.json', model.read_text(encoding='utf-8') if model.is_file() else json.dumps({'status': 'No saved model summary found'}))
        def cell(value):
            return escape(str(value)) if value is not None else '—'
        def link(url):
            from urllib.parse import urlsplit
            host = urlsplit(url or '').hostname or ''
            allowed = host in ('youtube.com', 'www.youtube.com', 'youtu.be', 'tiktok.com', 'www.tiktok.com', 'instagram.com', 'www.instagram.com')
            return f'<a href="{escape(url, quote=True)}">Open post</a>' if allowed and url.startswith('https://') else 'No link captured'
        body = ''.join('<tr>' + ''.join(f'<td>{cell(row.get(key))}</td>' for key in ('account', 'platform', 'clip_id', 'views', 'likes', 'comments', 'privacy', 'post_status'))
                       + f'<td>{link(row.get("url"))}</td></tr>' for row in merged)
        uploads = ''.join('<tr>' + ''.join(f'<td>{cell(row.get(key))}</td>' for key in ('account', 'platform', 'clip_id', 'state'))
                          + f'<td>{link(row.get("url"))}</td></tr>' for row in attempts)
        html = '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><meta http-equiv="refresh" content="60">
<title>Clip view tracking</title><style>body{font:16px system-ui;margin:32px;background:#111827;color:#e5e7eb}a{color:#93c5fd}table{border-collapse:collapse;width:100%;margin:20px 0}td,th{padding:10px;text-align:left;border-bottom:1px solid #374151}td{overflow-wrap:anywhere}h1,h2{color:white}.table{overflow:auto}</style>
<h1>Clip view tracking</h1>'''
        html += f'<p>Report updated: {datetime.now().astimezone().isoformat(timespec="seconds")}</p>'
        html += f'<p>{len(posts)} linked posts · {len(history)} metric snapshots · {len(features)} measured clips · {len(attempts)} upload records</p>'
        html += '<p>Reports refresh after each scheduler cycle. View numbers come from the browser collector or stats worker, not from this page. A dash means no measurement; it does not mean zero views. TikTok/Instagram uploads without links appear below but cannot be matched to view metrics yet.</p>'
        html += '<p>YouTube, TikTok Studio, and Instagram Reels counts are read using saved logins about every six hours. Failed checks retry after 30 minutes; busy profiles wait for the next scheduler cycle. Unavailable metrics remain unknown. Instagram privacy is not inferred from a visible Reel.</p>'
        html += '<p><a href="collection-status.json">Collector status</a> · <a href="post-observations.json">Privacy and review history</a></p><ul>' + ''.join(f'<li>{cell(row["account"])} / {cell(row["platform"])}: {cell(row["detail"])}</li>' for row in checks) + '</ul>'
        html += '<p>Downloads: ' + ' · '.join(f'<a href="{name}.json">{label}</a>' for name, label in [('posts','Latest post metrics'),('view-history','All view snapshots'),('clip-features','Clip features'),('upload-history','Upload records'),('model','Ranking model')]) + '</p>'
        html += '<h2>Latest measured views</h2><div class="table"><table><tr><th>Set</th><th>Platform</th><th>Clip</th><th>Views</th><th>Likes</th><th>Comments</th><th>Privacy</th><th>Post status</th><th>Link</th></tr>' + (body or '<tr><td colspan="9">No linked posts imported by the stats worker yet.</td></tr>') + '</table></div>'
        html += '<h2>All upload records</h2><div class="table"><table><tr><th>Set</th><th>Platform</th><th>Clip</th><th>Status</th><th>Link</th></tr>' + uploads + '</table></div></html>'
        write_file(folder / 'index.html', html)
    return folder


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--settings', type=Path, default=Path('browser-upload.json'))
    print(export_tracking(parser.parse_args().settings))
