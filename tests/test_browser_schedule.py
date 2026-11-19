import unittest
from test_browser_upload import BrowserUploadTests
from gta_clips.browser_upload import scan, claim, finish
from gta_clips.browser_schedule import readiness


class ScheduleTests(BrowserUploadTests):
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
