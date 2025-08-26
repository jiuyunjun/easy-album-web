import io
import os
import shutil
import zipfile
import pytest
import sys

# Ensure the project root is on sys.path when running via the pytest binary
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from EasyReview import app, UPLOAD_ROOT

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
    data = {"file": (io.BytesIO(b"vid"), "v.mp4")}
    client.post(f"/{ALBUM}", data=data, content_type="multipart/form-data")
    sdir = os.path.join(album_path, ".review", "v.mp4")
    os.makedirs(sdir, exist_ok=True)
    with open(os.path.join(sdir, "1.jpg"), "wb") as f:
        f.write(b"i")
    resp = client.get(f"/{ALBUM}/pack")
    _ = resp.data
    zpath = os.path.join(album_path, f"{ALBUM}.zip")
    assert resp.status_code == 200
    assert os.path.isfile(zpath)
    with zipfile.ZipFile(zpath) as outer:
        assert "v.zip" in outer.namelist()
        with zipfile.ZipFile(io.BytesIO(outer.read("v.zip"))) as inner:
            assert "v.mp4" in inner.namelist()
            assert "1.jpg" in inner.namelist()


def test_rename_album(client, album_path):
    data = {"file": (io.BytesIO(b"x"), "a.jpg")}
    client.post(f"/{ALBUM}", data=data, content_type="multipart/form-data")
    resp = client.post(f"/{ALBUM}/rename", json={"name": "renamed"})
    assert resp.status_code == 200
    assert os.path.isdir(os.path.join(UPLOAD_ROOT, "renamed"))
    # cleanup renamed path
    shutil.rmtree(os.path.join(UPLOAD_ROOT, "renamed"), ignore_errors=True)


def test_rename_invalid(client, album_path):
    data = {"file": (io.BytesIO(b"x"), "a.jpg")}
    client.post(f"/{ALBUM}", data=data, content_type="multipart/form-data")
    resp = client.post(f"/{ALBUM}/rename", json={"name": "bad/"})
    assert resp.status_code == 400


def test_rename_file(client, album_path):
    data = {"file": (io.BytesIO(b"x"), "old.jpg")}
    client.post(f"/{ALBUM}", data=data, content_type="multipart/form-data")
    rev = os.path.join(album_path, ".review", "old.jpg")
    os.makedirs(rev, exist_ok=True)
    resp = client.post(f"/{ALBUM}/rename_file", json={"old": "old.jpg", "new": "new.jpg"})
    assert resp.status_code == 200
    assert os.path.isfile(os.path.join(album_path, "new.jpg"))
    assert os.path.isdir(os.path.join(album_path, ".review", "new.jpg"))


def test_upload_conflict(client, album_path):
    data = {"file": (io.BytesIO(b"a"), "dup.jpg")}
    client.post(f"/{ALBUM}", data=data, content_type="multipart/form-data")
    data_conflict = {"file": (io.BytesIO(b"a"), "dup.jpg")}
    resp = client.post(f"/{ALBUM}", data=data_conflict, content_type="multipart/form-data")
    assert resp.status_code == 409
    data2 = {"file": (io.BytesIO(b"b"), "dup.jpg")}
    resp = client.post(f"/{ALBUM}?overwrite=1", data=data2, content_type="multipart/form-data")
    assert resp.status_code == 200
    with open(os.path.join(album_path, "dup.jpg"), "rb") as f:
        assert f.read() == b"b"


def test_snapshot_rename_and_export(client, album_path):
    # upload a video
    data = {"file": (io.BytesIO(b"v"), "vid.mp4")}
    client.post(f"/{ALBUM}", data=data, content_type="multipart/form-data")
    snap_dir = os.path.join(album_path, ".review", "vid.mp4")
    os.makedirs(snap_dir, exist_ok=True)
    snap_path = os.path.join(snap_dir, "1.000.jpg")
    with open(snap_path, "wb") as f:
        f.write(b"img")
    # rename snapshot with prefix
    resp = client.post(f"/{ALBUM}/review/vid.mp4/snapshots/1.000.jpg/rename", json={"prefix": "aaa"})
    assert resp.status_code == 200
    assert os.path.isfile(os.path.join(snap_dir, "aaa_1.000.jpg"))
    # export
    resp = client.get(f"/{ALBUM}/export/vid.mp4")
    z = zipfile.ZipFile(io.BytesIO(resp.data))
    assert "vid.mp4" in z.namelist()
    assert "aaa_1.000.jpg" in z.namelist()


def test_zip_upload_restore(client, album_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr('vid.mp4', b'v')
        z.writestr('aaa_1.000.jpg', b'i')
    buf.seek(0)
    data = {"file": (buf, "pack.zip")}
    client.post(f"/{ALBUM}", data=data, content_type="multipart/form-data")
    assert os.path.isfile(os.path.join(album_path, "vid.mp4"))
    assert os.path.isfile(os.path.join(album_path, ".review", "vid.mp4", "aaa_1.000.jpg"))

