import unittest
import json
import threading
from unittest.mock import patch
from test_browser_upload import BrowserUploadTests
from gta_clips.browser_upload import scan, claim, finish
from gta_clips.browser_schedule import readiness, tick


class ScheduleTests(BrowserUploadTests):
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
        self.assertEqual(readiness(self.db,'test','youtube',target,10000), 'needs review: uncertain upload')
        finish(self.db,'youtube','test',clip,'posted')
        self.db.execute('update posts set started=10000')
        self.db.commit()
        self.assertEqual(readiness(self.db,'test','youtube',target,13599), 'waiting for hourly interval')
        self.assertEqual(readiness(self.db,'test','youtube',target,13600), 'ready')


if __name__ == '__main__': unittest.main()
