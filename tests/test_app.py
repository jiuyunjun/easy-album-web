import os
import io
import shutil
import unittest
from EasyAlbumWeb import app, UPLOAD_ROOT

class AlbumTest(unittest.TestCase):
    album = 'test'

    def setUp(self):
        self.client = app.test_client()
        self.path = os.path.join(UPLOAD_ROOT, self.album)
        if os.path.isdir(self.path):
            shutil.rmtree(self.path)
        os.makedirs(self.path, exist_ok=True)

    def tearDown(self):
        if os.path.isdir(self.path):
            shutil.rmtree(self.path)

    def test_upload_utf8_filename(self):
        data = {
            'file': (io.BytesIO(b'123'), '测试.jpg')
        }
        resp = self.client.post(f'/{self.album}', data=data,
                                content_type='multipart/form-data')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(os.path.isfile(os.path.join(self.path, '测试.jpg')))

    def test_delete_and_clear(self):
        # upload two files
        files = [
            ('a.jpg', b'a'),
            ('b.jpg', b'b')
        ]
        for name, content in files:
            data = {'file': (io.BytesIO(content), name)}
            self.client.post(f'/{self.album}', data=data,
                             content_type='multipart/form-data')
        self.assertTrue(os.path.isfile(os.path.join(self.path, 'a.jpg')))
        # delete one
        self.client.post(f'/{self.album}/delete', json={'file': 'a.jpg'})
        self.assertFalse(os.path.isfile(os.path.join(self.path, 'a.jpg')))
        # clear all
        self.client.post(f'/{self.album}/delete_all')
        self.assertEqual(os.listdir(self.path), [])

    def test_pack_zip(self):
        data = {
            'file': (io.BytesIO(b'xyz'), 'c.jpg')
        }
        self.client.post(f'/{self.album}', data=data,
                         content_type='multipart/form-data')
        resp = self.client.get(f'/{self.album}/pack')
        zpath = os.path.join(self.path, f"{self.album}.zip")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(os.path.isfile(zpath))

if __name__ == '__main__':
    unittest.main()

