"""Integration coverage for local rating persistence and video seeking."""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import json
import re
import unittest
import urllib.request
import urllib.error

from gta_clips.config import load_config, PathsConfig
from gta_clips.db import connect
from gta_clips.review_web import make_server

class ReviewWebTest(unittest.TestCase):
    def test_video_ranges_and_authenticated_rating_persistence(self):
        with TemporaryDirectory() as directory:
            config=replace(load_config('config.example.toml'), paths=PathsConfig(Path(directory)))
            (Path(directory)/'test.mp4').write_bytes(b'0123456789')
            with connect(config.paths.database) as db:
                db.execute("INSERT INTO vods(id,streamer,status) VALUES ('1','test','done')")
                db.execute("""INSERT INTO clips(id,vod_id,streamer,path,start,duration,transcript,title,description,hashtags)
                    VALUES ('clip','1','test','test.mp4',0,60,'','Test','','')""")
            server=make_server(config,0)
            thread=Thread(target=server.serve_forever,daemon=True);thread.start()
            url=f'http://127.0.0.1:{server.server_port}'
            try:
                with urllib.request.urlopen(url) as response: page=response.read().decode()
                token=re.search("const token='([^']+)'",page)[1]
                with urllib.request.urlopen(urllib.request.Request(url+'/video/clip',headers={'Range':'bytes=2-5'})) as response:
                    self.assertEqual(response.status,206)
                    self.assertEqual(response.read(),b'2345')
                body=json.dumps({'clip_id':'clip','score':4,'notes':'Great payoff'}).encode()
                with self.assertRaises(urllib.error.HTTPError) as blocked:
                    urllib.request.urlopen(urllib.request.Request(url+'/api/rate',data=body))
                self.assertEqual(blocked.exception.code,403)
                request=urllib.request.Request(url+'/api/rate',data=body,headers={'X-Review-Token':token,'Content-Type':'application/json'})
                with urllib.request.urlopen(request) as response:self.assertTrue(json.load(response)['saved'])
                with urllib.request.urlopen(url+'/api/clips') as response: clip=json.load(response)[0]
                self.assertEqual((clip['score'],clip['notes']),(4,'Great payoff'))
                with connect(config.paths.database) as db:
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM uploads').fetchone()[0],0)
                with self.assertRaises(urllib.error.HTTPError) as blocked:
                    urllib.request.urlopen(urllib.request.Request(url+'/api/clips',headers={'Host':'untrusted.example'}))
                self.assertEqual(blocked.exception.code,403)
            finally:
                server.shutdown();server.server_close();thread.join()
