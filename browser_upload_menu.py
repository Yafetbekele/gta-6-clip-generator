"""Small local launcher; credentials are entered only on the platform's own website."""
import json
import re
from pathlib import Path
from urllib.parse import urlsplit
from gta_clips.browser_upload import main


def parse_identity(platform, value):
    value = value.strip()
    if platform == 'youtube':
        if value.startswith('https://'):
            url = urlsplit(value)
            if url.hostname not in ('studio.youtube.com', 'www.youtube.com', 'youtube.com'):
                raise ValueError('Use a YouTube Studio channel URL or a channel ID.')
            value = url.path.removeprefix('/channel/').rstrip('/')
        if not re.fullmatch(r'UC[A-Za-z0-9_-]{22}', value):
            raise ValueError('Paste the Studio URL ending in /channel/UC… or the exact channel ID.')
        return 'channel_id', value
    value = value.lstrip('@')
    if not re.fullmatch(r'[A-Za-z0-9_.]+', value):
        raise ValueError('Enter the handle, such as @mychannel, without a URL.')
    return 'handle', value


def choose(label, options):
    print(f"\n{label}")
    for i, item in enumerate(options, 1):
        print(f"  {i}. {item}")
    while True:
        answer = input("Number (or q to quit): ").strip()
        if answer.lower() == "q":
            raise SystemExit(0)
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return options[int(answer) - 1]


if __name__ == '__main__':
    settings = Path(__file__).resolve().parent / 'browser-upload.json'
    if not settings.is_file():
        raise SystemExit('Copy browser-upload.example.json to browser-upload.json and configure your accounts first.')
    config = json.loads(settings.read_text(encoding='utf-8-sig'))
    preferred = ['gta6-1', 'gta6-2', 'gta6-3']
    accounts = [name for name in preferred if name in config['accounts']]
    accounts += [name for name in config['accounts'] if name not in accounts]
    account = choose('Account folder', accounts)
    platform = choose('Platform', list(config['accounts'][account]))
    action = choose('Action', ['preview', 'login', 'status', 'publish one', 'verify-login', 'enable hourly posting', 'disable hourly posting', 'set channel URL or handle'])
    if action == 'set channel URL or handle':
        prompt = 'Paste YouTube Studio channel URL or channel ID: ' if platform == 'youtube' else 'Enter account @handle: '
        while True:
            value = input(prompt).strip()
            if value.lower() == 'q':
                raise SystemExit(0)
            try:
                key, identity = parse_identity(platform, value)
                break
            except ValueError as exc:
                print(f'{exc} (or q to cancel)')
        target = config['accounts'][account][platform]
        if target.get(key) != identity:
            target['auto_post'] = False
        target[key] = identity
        settings.write_text(json.dumps(config, indent=2) + '\n', encoding='utf-8')
        print(f'Saved {platform} identity for {account}: {identity}')
        print('Close the dedicated Chrome window, then choose verify-login. Changed identities disable hourly posting.')
        raise SystemExit(0)
    if action in ('enable hourly posting', 'disable hourly posting'):
        enabled = action == 'enable hourly posting'
        config['accounts'][account][platform]['auto_post'] = enabled
        settings.write_text(json.dumps(config, indent=2) + '\n', encoding='utf-8')
        print('Hourly posting enabled; a successful test upload is required before it runs.' if enabled else 'Hourly posting disabled.')
        raise SystemExit(0)
    argv = ['--settings', str(settings), '--account', account, '--platform', platform]
    if action == 'publish one':
        print('This publishes the next eligible ready-folder video using the saved title and caption.')
        if input('Type POST to publish: ').strip() != 'POST':
            raise SystemExit('Cancelled.')
        argv += ['run', '--publish', '--max', '1']
    else:
        argv += [action]
    raise SystemExit(main(argv))
