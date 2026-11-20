import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import browser_upload_menu
from gta_clips.browser_upload import ledger, verify_saved_account
from gta_clips.browser_status import account_status


class StatusTests(unittest.TestCase):
    def test_status_tracks_identity_verification_and_confirmed_submissions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'settings.json'
            config = {'state_dir': 'state', 'accounts': {'gta6-1': {'instagram': {
                'handle': 'example', 'auto_post': True}}}}
            path.write_text(json.dumps(config))
            db = ledger(root / 'state' / 'posts.sqlite3')
            try:
                self.assertEqual(account_status(path)[0]['login'], 'Not checked')
                with patch('gta_clips.browser_upload.verify_account'):
                    verify_saved_account(None, 'instagram', 'gta6-1', {'handle': 'example'}, db)
                with db:
                    for index, state in enumerate(('posted', 'under_review', 'held')):
                        db.execute('INSERT INTO posts VALUES (?,?,?,?,?,?,NULL,?,?)',
                                   ('instagram', 'gta6-1', str(index), str(index), state, 1, '', ''))
                row = account_status(path)[0]
                self.assertTrue(row['enabled'])
                self.assertTrue(row['login'].startswith('Verified '))
                self.assertEqual((row['posted'], row['under_review']), (1, 1))
                with patch('gta_clips.browser_upload.verify_account', side_effect=RuntimeError('wrong account')):
                    with self.assertRaises(RuntimeError):
                        verify_saved_account(None, 'instagram', 'gta6-1', {'handle': 'example'}, db)
                self.assertTrue(account_status(path)[0]['login'].startswith('Check failed '))
                config['accounts']['gta6-1']['instagram']['handle'] = 'new_identity'
                path.write_text(json.dumps(config))
                self.assertEqual(account_status(path)[0]['login'], 'Recheck: identity changed')
            finally:
                db.close()

    def test_launcher_returns_after_failure_and_success_until_quit(self):
        with patch('browser_upload_menu.run_menu_action', side_effect=[SystemExit(1), None, browser_upload_menu.MenuQuit()]) as action, \
                patch('builtins.input', return_value='') as prompt:
            browser_upload_menu.launch()
        self.assertEqual(action.call_count, 3)
        self.assertEqual(prompt.call_count, 2)
