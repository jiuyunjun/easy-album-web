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

if __name__ == '__main__':
    unittest.main()

