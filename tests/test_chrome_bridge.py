import json
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from pathlib import Path
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from gta_clips.chrome_bridge import Queue, MODE, handler_for, token_for
from gta_clips.browser_upload import scan

CHANNEL = 'UC' + 'a' * 22
NOTE = '''Channel: Test

Title:
A good moment | @streamer

Description:
Credit: https://www.twitch.tv/streamer

Hashtags:
#Shorts

Clip ID: clip-1
'''


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = self.root / 'settings.json'
        self.cfg = {'youtube_transport': MODE, 'state_dir': 'state', 'ready_folder': 'ready',
                    'accounts': {'first': {'youtube': {'channel_id': CHANNEL, 'auto_post': True,
                                                       'daily_limit': 3, 'spacing_minutes': 480}}}}
        self.write_config()
        folder = self.root / 'ready/2026-11-27/first'
        folder.mkdir(parents=True)
        self.video = folder / '1200 clip.mp4'
        self.video.write_bytes(b'video bytes')
        self.video.with_suffix('.txt').write_text(NOTE)
        self.queue = Queue(self.settings)

    def write_config(self):
        self.settings.write_text(json.dumps(self.cfg))

    def job(self, now=100000):
        return self.queue.next(now)['job']

    def event(self, job, event, now=100001, **extra):
        return self.queue.event({'job_id':job['id'], 'event':event, 'channel_id':CHANNEL, **extra}, now)

    def test_default_transport_cannot_issue_jobs(self):
        self.cfg.pop('youtube_transport'); self.write_config()
        self.assertIsNone(self.job())

    def test_test_run_stops_after_one_post_even_after_spacing_elapses(self):
        self.queue.begin_test()
        job=self.job();self.event(job,'start');self.event(job,'posted',url='https://youtube.com/shorts/abc123')
        other=self.video.with_name('1300 second.mp4');other.write_bytes(b'other video')
        other.with_suffix('.txt').write_text(NOTE.replace('clip-1','clip-2'))
        self.assertIsNone(self.job(now=200000))
        self.assertEqual(self.queue.status()['test_accounts'][0]['state'],'posted')

    def test_test_run_reports_missing_clips_without_later_surprise_posts(self):
        self.queue.begin_test();self.video.unlink()
        self.assertIsNone(self.job())
        self.assertEqual(self.queue.status()['test_accounts'][0]['state'],'skipped')

    def test_channel_handle_binding_only_accepts_configured_channel(self):
        self.queue.bind({'channel_id':CHANNEL,'handle':'@mychannel'})
        self.assertEqual(self.job()['channel_handle'],'@mychannel')
        with self.assertRaises(ValueError):self.queue.bind({'channel_id':'UC'+'b'*22,'handle':'@wrong'})

    def test_only_one_job_and_ready_folder_ownership(self):
        job = self.job()
        self.assertEqual(job['account'], 'first')
        self.assertEqual(Path(job['file']), self.video)
        self.assertIsNone(self.job())
        self.assertNotIn('Sign-in', job['description'])

    def test_wrong_channel_never_claims_or_uploads(self):
        job = self.job()
        with self.assertRaisesRegex(ValueError, 'identity'):
            self.event(job,'start',channel_id='UC'+'b'*22)
        with self.queue.db() as db:
            self.assertEqual(db.execute('select count(*) from posts').fetchone()[0],0)

    def test_file_changed_after_reservation_stops_before_upload(self):
        job = self.job(); self.video.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'changed'):
            self.event(job,'start')

    def test_success_is_idempotent_and_prevents_duplicate(self):
        job = self.job(); self.event(job,'start')
        self.event(job,'posted',url='https://youtube.com/shorts/abc123')
        self.event(job,'posted',url='https://youtube.com/shorts/abc123')
        self.assertIsNone(self.job(now=200000))
        with self.queue.db() as db:
            row=db.execute('select state,url from posts').fetchone()
            self.assertEqual(tuple(row),('posted','https://youtube.com/shorts/abc123'))

    def test_failure_keeps_uncertain_post_held_and_pauses_only_account(self):
        job=self.job();self.event(job,'start');self.event(job,'failed',detail='Inspect Studio')
        with self.queue.db() as db:
            self.assertEqual(db.execute('select state from posts').fetchone()[0],'held')
            self.assertEqual(db.execute('select account from chrome_attention').fetchone()[0],'first')
        self.assertIsNone(self.job(now=200000))

    def test_expired_upload_not_retried(self):
        job=self.job();self.event(job,'start')
        self.assertIsNone(self.job(now=200000))
        with self.assertRaisesRegex(ValueError,'expired'):
            self.event(job,'posted',now=200000,url='https://youtube.com/shorts/abc123')

    def test_disabled_account_and_daily_limits_and_spacing(self):
        self.cfg['accounts']['first']['youtube']['auto_post']=False;self.write_config()
        self.assertIsNone(self.job())
        self.cfg['accounts']['first']['youtube']['auto_post']=True;self.write_config()
        with self.queue.db() as db:
            for i in range(3):
                db.execute("insert into posts values('youtube','first',?,?, 'posted',?,NULL,'','')",(f'old-{i}',f'digest-{i}',99000-i))
        self.assertIsNone(self.job())
        with self.queue.db() as db:
            db.execute("delete from posts where clip_id!='old-0'")
        self.assertIsNone(self.job())  # Eight-hour interval still applies.

    def test_restart_keeps_inflight_lease(self):
        self.job()
        self.assertIsNone(Queue(self.settings).next(100005)['job'])

    def test_publication_requires_start_and_recognizable_url(self):
        job=self.job()
        with self.assertRaises(ValueError):self.event(job,'posted',url='https://youtube.com/shorts/abc123')
        self.event(job,'start')
        with self.assertRaises(ValueError):self.event(job,'posted',url='https://evil.example/abc123')

    def test_authentication_origin_and_host(self):
        token=token_for(self.queue.state)
        self.assertEqual(token_for(self.queue.state),token)
        server=ThreadingHTTPServer(('127.0.0.1',0),handler_for(self.queue,token,0))
        # Bind the actual assigned port into the handler's host validation.
        server.RequestHandlerClass=handler_for(self.queue,token,server.server_port)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            url=f'http://127.0.0.1:{server.server_port}/status'
            for headers in ({}, {'Authorization':'Bearer '+token,'Origin':'https://evil.example'},
                            {'Authorization':'Bearer '+token,'Host':'evil.example'}):
                with self.assertRaises(urllib.error.HTTPError) as e:
                    urllib.request.urlopen(urllib.request.Request(url,headers=headers))
                self.assertEqual(e.exception.code,403)
            req=urllib.request.Request(url,headers={'Authorization':'Bearer '+token,'Origin':'chrome-extension://'+'a'*32})
            with urllib.request.urlopen(req) as response:
                self.assertEqual(json.load(response)['mode'],MODE)
        finally:
            server.shutdown();server.server_close();thread.join()

    def test_three_per_day_cap_applies_even_if_old_setting_is_higher(self):
        self.cfg['accounts']['first']['youtube']['daily_limit']=99;self.write_config()
        with self.queue.db() as db:
            for i,started in enumerate([200000-23*3600,200000-18*3600,200000-9*3600]):
                db.execute("insert into posts values('youtube','first',?,?, 'posted',?,NULL,'','')",(f'old-{i}',f'digest-{i}',started))
        self.assertIsNone(self.job(now=200000))

    def test_second_start_does_not_send_file_twice(self):
        job=self.job();self.event(job,'start')
        with self.assertRaisesRegex(ValueError,'already started'):self.event(job,'start')

    def test_configuration_change_after_reservation_stops_upload(self):
        job=self.job()
        self.cfg['accounts']['first']['youtube']['channel_id']='UC'+'b'*22;self.write_config()
        with self.assertRaisesRegex(ValueError,'configuration changed'):self.event(job,'start')

    def test_legacy_scheduler_skips_extension_owned_accounts(self):
        from gta_clips.browser_schedule import tick
        with patch('gta_clips.browser_schedule.scan',side_effect=AssertionError('Legacy scanned extension queue')):
            results=tick(self.settings,dry_run=True)
        self.assertEqual(results[0]['status'],'handled by normal Chrome extension')

    def test_legacy_publish_command_cannot_bypass_transport(self):
        from gta_clips.browser_upload import main
        with self.assertRaises(SystemExit) as e:
            main(['run','--settings',str(self.settings),'--account','first','--platform','youtube','--publish'])
        self.assertEqual(e.exception.code,1)


if __name__=='__main__':unittest.main()
