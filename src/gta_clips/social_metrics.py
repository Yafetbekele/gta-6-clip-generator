"""Visible TikTok Studio and Instagram Reels metrics; no private endpoints."""
from urllib.parse import urlsplit
import re

from .browser_metrics import exact_count


def remote_id(platform, href):
    path = urlsplit(href or '').path
    pattern = r'/@[^/]+/video/(\d+)/?$' if platform == 'tiktok' else r'/(?:[^/]+/)?(?:reel|p)/([\w-]+)/?$'
    match = re.search(pattern, path)
    return match.group(1) if match else None


def normalized(text):
    return ' '.join((text or '').split())


def match_clip(caption, candidates):
    """Require the complete title AND exact source-VOD timestamp, uniquely."""
    text = normalized(caption)
    matches = []
    for clip in candidates:
        source_urls = re.findall(r'https://(?:www\.)?twitch\.tv/videos/\d+\?t=\d+s', clip.get('description') or '')
        if source_urls and normalized(clip['title']) in text and all(url in text for url in source_urls):
            matches.append(clip)
    return matches[0] if len(matches) == 1 else None


def tiktok_rows(page):
    page.get_by_text('Posts', exact=True).click()
    selector = '[data-tt="components_PostTable_Absolute"]'
    page.locator(selector).first.wait_for(state='visible')
    # Confirm metric column order before reading the numbered cells.
    header = page.locator('[data-tt="components_PostTable_FlexColumn"]').first.inner_text()
    if not all(name in header for name in ('Views', 'Likes', 'Comments')):
        raise RuntimeError('TikTok metric headers were not recognized')
    found = {}
    unchanged = 0
    for _ in range(40):
        rows = page.locator(selector).evaluate_all('''els=>els.map(e=>({
            href:e.querySelector('[data-tt="components_PostInfoCell_a"]')?.getAttribute('href'),
            caption:e.querySelector('[data-tt="components_PostInfoCell_a"]')?.innerText,
            views:e.querySelector('[data-tt="components_RowLayout_FlexRow_6"]')?.innerText,
            likes:e.querySelector('[data-tt="components_RowLayout_FlexRow_7"]')?.innerText,
            comments:e.querySelector('[data-tt="components_RowLayout_FlexRow_8"]')?.innerText,
            privacy:e.querySelector('[data-tt="components_PrivacyCell_TUXButton"]')?.innerText,
            status:e.querySelector('[data-tt="components_PublishStageLabel_TUXText"]')?.innerText || 'Listed in Posts'
        }))''')
        old = len(found)
        for row in rows:
            if remote_id('tiktok', row.get('href')):
                found[row['href']] = row
        unchanged = unchanged + 1 if len(found) == old else 0
        if unchanged >= 2 or not rows:
            break
        page.locator(selector).last.scroll_into_view_if_needed()
        page.wait_for_timeout(800)
    return list(found.values())


def instagram_rows(page, handle):
    home = page.locator('svg[aria-label="Home"]')
    sidebar = home.locator('xpath=ancestor::*[.//a//img[contains(@alt,"profile picture")]][1]')
    sidebar.locator('a').filter(has=page.locator('img[alt$="profile picture"]')).first.click()
    cards = page.locator('a[href*="/reel/"]')
    cards.first.wait_for(state='visible')
    captions = {}
    for _ in range(20):
        rows = cards.evaluate_all("els=>els.map(e=>({href:e.getAttribute('href'),caption:e.querySelector('img')?.alt || ''}))")
        old = len(captions)
        for row in rows:
            key = remote_id('instagram', row['href'])
            if key:
                captions[key] = row['caption']
        page.mouse.move(900, 600)
        page.mouse.wheel(0, 800)
        page.wait_for_timeout(600)
        if len(captions) == old:
            break
    page.locator(f'a[href="/{handle.lstrip("@").lower()}/reels/"]').click()
    found = {}
    unchanged = 0
    for _ in range(40):
        rows = cards.evaluate_all('''els=>els.map(e=>({href:e.getAttribute('href'),
            views:e.querySelector('svg[aria-label="View Count Icon"]') ? e.innerText : null,
            status:'Listed on Reels profile',privacy:null}))''')
        old = len(found)
        for row in rows:
            key = remote_id('instagram', row['href'])
            if key and exact_count(row['views']) is not None:
                row['caption'] = captions.get(key, '')
                found[key] = row
        unchanged = unchanged + 1 if len(found) == old else 0
        if unchanged >= 3:
            break
        if cards.count():
            page.mouse.move(900, 600)
            page.mouse.wheel(0, 800)
        page.wait_for_timeout(1000)
    # Reels grids sometimes omit caption alt text. Read the visible post page
    # for those rows; retain metrics even if the caption cannot be recovered.
    for row in list(found.values())[:25]:
        if 'twitch.tv/videos/' in row.get('caption', ''):
            continue
        try:
            page.goto('https://www.instagram.com' + urlsplit(row['href']).path, wait_until='domcontentloaded')
            caption = page.get_by_text(re.compile(r'Source VOD: https://'))
            caption.first.wait_for(state='visible', timeout=10000)
            row['caption'] = page.locator('body').inner_text()
        except Exception:
            pass
    return list(found.values())


def store_social(db, upload_db, account, platform, rows, candidates, fetched):
    db.execute('''CREATE TABLE IF NOT EXISTS post_observations (
        platform TEXT,video_id TEXT,fetched INTEGER,privacy TEXT,status TEXT,caption TEXT,
        PRIMARY KEY(platform,video_id,fetched))''')
    stored = 0
    for row in rows:
        remote = remote_id(platform, row.get('href'))
        views = exact_count(row.get('views'))
        if not remote or views is None:
            continue
        existing = db.execute('SELECT clip_id,account FROM posts WHERE platform=? AND video_id=?', (platform, remote)).fetchone()
        clip = match_clip(row.get('caption'), candidates)
        clip_id = existing['clip_id'] if existing and existing['clip_id'] else clip['id'] if clip else None
        post_account = existing['account'] if existing and existing['account'] else account
        base = 'https://www.tiktok.com' if platform == 'tiktok' else 'https://www.instagram.com'
        url = base + urlsplit(row['href']).path
        db.execute('''INSERT INTO posts(platform,video_id,clip_id,account,url,source) VALUES(?,?,?,?,?,'browser-studio')
            ON CONFLICT(platform,video_id) DO UPDATE SET clip_id=COALESCE(posts.clip_id,excluded.clip_id),url=excluded.url''',
                   (platform, remote, clip_id, post_account, url))
        published = clip.get('finished') if clip else None
        inserted = db.execute('''INSERT OR IGNORE INTO post_stats(platform,video_id,fetched,published,views,likes,comments)
            VALUES(?,?,?,?,?,?,?)''', (platform, remote, int(fetched), published, views,
                                     exact_count(row.get('likes')), exact_count(row.get('comments'))))
        db.execute('INSERT OR IGNORE INTO post_observations VALUES(?,?,?,?,?,?)',
                   (platform, remote, int(fetched), row.get('privacy'), row.get('status'), row.get('caption')))
        stored += inserted.rowcount
        if clip:
            with upload_db:
                upload_db.execute("UPDATE posts SET url=? WHERE platform=? AND account=? AND clip_id=? AND state IN ('posted','under_review')",
                                  (url, platform, account, clip['id']))
    return stored
