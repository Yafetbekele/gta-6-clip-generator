"""Small local launcher; credentials are entered only on the platform's own website."""
import json
from pathlib import Path
from gta_clips.browser_upload import main


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
    account = choose('Account folder', list(config['accounts']))
    platform = choose('Platform', list(config['accounts'][account]))
    action = choose('Action', ['preview', 'login', 'status', 'publish one', 'verify-login', 'enable hourly posting', 'disable hourly posting'])
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
