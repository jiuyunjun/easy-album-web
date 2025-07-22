import os
import io
import shutil
import pytest
import sys

# Ensure the project root is on sys.path when running via the pytest binary
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from EasyAlbumWeb import app, UPLOAD_ROOT

ALBUM = "test"


@pytest.fixture
def album_path():
    path = os.path.join(UPLOAD_ROOT, ALBUM)
    if os.path.isdir(path):
        shutil.rmtree(path)
    os.makedirs(path, exist_ok=True)
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def client():
    return app.test_client()


def test_upload_utf8_filename(client, album_path):
    data = {
        "file": (io.BytesIO(b"123"), "测试.jpg")
    }
    resp = client.post(f"/{ALBUM}", data=data, content_type="multipart/form-data")
    assert resp.status_code == 200
    assert os.path.isfile(os.path.join(album_path, "测试.jpg"))


def test_delete_and_clear(client, album_path):
    files = [
        ("a.jpg", b"a"),
        ("b.jpg", b"b"),
    ]
    for name, content in files:
        data = {"file": (io.BytesIO(content), name)}
        client.post(f"/{ALBUM}", data=data, content_type="multipart/form-data")
    assert os.path.isfile(os.path.join(album_path, "a.jpg"))
    client.post(f"/{ALBUM}/delete", json={"file": "a.jpg"})
    assert not os.path.isfile(os.path.join(album_path, "a.jpg"))
    client.post(f"/{ALBUM}/delete_all")
    assert os.listdir(album_path) == []


def test_pack_zip(client, album_path):
    data = {"file": (io.BytesIO(b"xyz"), "c.jpg")}
    client.post(f"/{ALBUM}", data=data, content_type="multipart/form-data")
    resp = client.get(f"/{ALBUM}/pack")
    zpath = os.path.join(album_path, f"{ALBUM}.zip")
    assert resp.status_code == 200
    assert os.path.isfile(zpath)

