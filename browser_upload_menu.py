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


class MenuQuit(Exception):
    pass


def choose(label, options):
    print(f"\n{label}")
    for i, item in enumerate(options, 1):
        print(f"  {i}. {item}")
    while True:
        answer = input("Number (or q to quit): ").strip()
        if answer.lower() == "q":
            raise MenuQuit()
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return options[int(answer) - 1]


def run_menu_action(settings):
    if not settings.is_file():
        raise SystemExit('Copy browser-upload.example.json to browser-upload.json and configure your accounts first.')
    config = json.loads(settings.read_text(encoding='utf-8-sig'))
    preferred = ['gta6-1', 'gta6-2', 'gta6-3']
    accounts = [name for name in preferred if name in config['accounts']]
    accounts += [name for name in config['accounts'] if name not in accounts]
    selected = choose('Account folder', accounts + ['ALL ACCOUNTS STATUS'])
    if selected == 'ALL ACCOUNTS STATUS':
        from gta_clips.browser_status import display_status
        display_status(settings)
        return
    account = selected
    platform = choose('Platform', list(config['accounts'][account]))
    action = choose('Action', ['preview', 'login', 'status', 'publish one', 'verify-login', 'enable hourly posting', 'disable hourly posting', 'set channel URL or handle', 'all accounts status'])
    if action == 'all accounts status':
        from gta_clips.browser_status import display_status
        display_status(settings)
        return
    if action == 'set channel URL or handle':
        prompt = 'Paste YouTube Studio channel URL or channel ID: ' if platform == 'youtube' else 'Enter account @handle: '
        while True:
            value = input(prompt).strip()
            if value.lower() == 'q':
                return
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
        return
    if action in ('enable hourly posting', 'disable hourly posting'):
        enabled = action == 'enable hourly posting'
        config['accounts'][account][platform]['auto_post'] = enabled
        settings.write_text(json.dumps(config, indent=2) + '\n', encoding='utf-8')
        if enabled:
            from gta_clips.browser_upload import ledger
            from gta_clips.browser_schedule import readiness
            import time
            state = (settings.parent / config.get('state_dir', 'data/browser-upload')).resolve()
            db = ledger(state / 'posts.sqlite3')
            try:
                status = readiness(db, account, platform, config['accounts'][account][platform], time.time())
            finally:
                db.close()
            print(f'Hourly posting enabled for {account}/{platform}. Status: {status}.')
            print('Confirmed submissions under review count as successful upload tests.')
        else:
            print('Hourly posting disabled.')
        return
    argv = ['--settings', str(settings), '--account', account, '--platform', platform]
    if action == 'publish one':
        print('This publishes the next eligible ready-folder video using the saved title and caption.')
        if input('Type POST to publish: ').strip() != 'POST':
            print('Cancelled.')
            return
        argv += ['run', '--publish', '--max', '1']
    else:
        argv += [action]
    main(argv)


def launch():
    settings = Path(__file__).resolve().parent / 'browser-upload.json'
    while True:
        try:
            run_menu_action(settings)
        except MenuQuit:
            return
        except SystemExit as exc:
            if exc.code not in (None, 0, 1):
                print(exc.code)
        except (OSError, ValueError, KeyError, RuntimeError) as exc:
            print(f'Stopped: {exc}')
        try:
            input('\nPress Enter to return to the menu (Ctrl+C to quit)...')
        except (KeyboardInterrupt, EOFError):
            return


if __name__ == '__main__':
    try:
        launch()
    except (KeyboardInterrupt, EOFError):
        print('\nClosed launcher.')
