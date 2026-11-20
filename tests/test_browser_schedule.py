import unittest
import json
import threading
from unittest.mock import patch
from test_browser_upload import BrowserUploadTests
from gta_clips.browser_upload import scan, claim, finish, Clip, exists
from gta_clips.browser_schedule import readiness, tick


class ScheduleTests(BrowserUploadTests):
    def test_failed_clip_does_not_block_next_hour_or_get_retried(self):
        clip, = scan(self.root, 'test')
        claim(self.db, 'youtube', 'test', clip)
        finish(self.db, 'youtube', 'test', clip, 'posted')
        failed = Clip(self.video, 'failed-clip', 'other-digest', 'Failed clip', '')
        claim(self.db, 'youtube', 'test', failed)
        self.db.execute("update posts set started=10000 where state='posted'")
        self.db.execute("update posts set started=13600 where state='held'")
        self.db.commit()
        target = {'auto_post': True}
        self.assertEqual(readiness(self.db, 'test', 'youtube', target, 17199), 'waiting for hourly interval')
        self.assertEqual(readiness(self.db, 'test', 'youtube', target, 17200), 'ready')
        self.assertTrue(exists(self.db, 'youtube', 'test', failed))
        self.assertEqual(self.db.execute("select state from posts where clip_id='failed-clip'").fetchone()[0], 'held')

    def test_review_submission_counts_as_test_and_is_not_retried(self):
        clip, = scan(self.root, 'test')
        claim(self.db, 'tiktok', 'test', clip)
        finish(self.db, 'tiktok', 'test', clip, 'under_review')
        self.db.execute('update posts set started=10000')
        self.db.commit()
        self.assertEqual(readiness(self.db, 'test', 'tiktok', {'auto_post': True}, 13600), 'ready')

    def test_tick_runs_two_accounts_concurrently(self):
        path = self.root / 'settings.json'
        path.write_text(json.dumps({
            'ready_folder': str(self.root), 'state_dir': str(self.root),
            'accounts': {name: {'youtube': {'auto_post': True}} for name in ('first', 'second')},
        }), encoding='utf-8')
        barrier = threading.Barrier(2)
        def upload(*args):
            barrier.wait(timeout=5)
            return 'run completed'
        with patch('gta_clips.browser_schedule.readiness', return_value='ready'), \
                patch('gta_clips.browser_schedule.settings'), \
                patch('gta_clips.browser_schedule.scan', return_value=[object()]), \
                patch('gta_clips.browser_schedule.exists', return_value=False), \
                patch('gta_clips.browser_schedule.run_upload', side_effect=upload) as run:
            results = tick(path)
        self.assertEqual(run.call_count, 2)
        self.assertEqual([r['status'] for r in results], ['run completed'] * 2)

    def test_scheduler_requires_opt_in_and_proven_upload(self):
        self.assertEqual(readiness(self.db,'test','youtube',{},10000), 'disabled')
        target = {'auto_post': True}
        self.assertEqual(readiness(self.db,'test','youtube',target,10000), 'needs successful test upload')
        clip, = scan(self.root,'test')
        claim(self.db,'youtube','test',clip)
        self.assertEqual(readiness(self.db,'test','youtube',target,10000), 'needs successful test upload')
        finish(self.db,'youtube','test',clip,'posted')
        self.db.execute('update posts set started=10000')
        self.db.commit()
        self.assertEqual(readiness(self.db,'test','youtube',target,13599), 'waiting for hourly interval')
        self.assertEqual(readiness(self.db,'test','youtube',target,13600), 'ready')


if __name__ == '__main__': unittest.main()
