#!/usr/bin/env python3
# coding: utf-8
"""
本地图片/视频分享站（单文件）— 功能增强版
新增：
1. 删除单个文件 & 清空相册（确认弹窗）。
2. 优化移动端布局（更自然的卡片宽度与间距）。
3. 视频 Range 流式播放（支持拖动 / 极速加载）。
"""

import os, shutil, hashlib, mimetypes, io, zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from flask import (
    Flask, request, render_template, abort, flash, send_file, Response, redirect, url_for, jsonify
)

# ------------------ 配置 ------------------
UPLOAD_ROOT = os.path.abspath("uploads")
ALLOWED_EXTS = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp",
    ".dng", ".raw", ".nef", ".cr2", ".arw", ".rw2",
    ".mp4", ".webm", ".ogg", ".avi", ".mov", ".mkv"
}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp"}
VIDEO_EXTS = {".mp4", ".webm", ".ogg", ".avi", ".mov", ".mkv"}
RAW_EXTS   = {".dng", ".raw", ".nef", ".cr2", ".arw", ".rw2"}
MAX_CONTENT_LENGTH = 5 * 1024 * 1024 * 1024  # 5 GB/请求

app = Flask(__name__, static_folder=UPLOAD_ROOT, static_url_path="/uploads")
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH
app.secret_key = "local-secret"
os.makedirs(UPLOAD_ROOT, exist_ok=True)

THUMB_DIR = ".thumbs"
THUMB_SIZE = (320, 320)
executor = ThreadPoolExecutor(max_workers=4)
PLACEHOLDER = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00"
    b"\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
)

# ------------------ 工具 ------------------

def allowed(fn: str) -> bool:
    return os.path.splitext(fn)[1].lower() in ALLOWED_EXTS

def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

def safe_album(name: str) -> str:
    return "".join(c for c in name if c.isalnum() or c == "_")

def sanitize_filename(name: str) -> str:
    """Allow UTF-8 filenames while stripping path separators and control chars."""
    name = os.path.basename(name)
    name = name.replace("\x00", "")
    import re
    return re.sub(r"[^\w\u4e00-\u9fff().\- ]+", "_", name)

def make_thumb(src: str, dest: str):
    """Create thumbnail for image/raw/video files."""
    try:
        from PIL import Image
        import rawpy
    except Exception:
        return
    ext = os.path.splitext(src)[1].lower()
    try:
        if ext in RAW_EXTS:
            with rawpy.imread(src) as r:
                img = Image.fromarray(r.postprocess())
        elif ext in VIDEO_EXTS:
            try:
                import cv2
                v = cv2.VideoCapture(src)
                ok, frame = v.read()
                v.release()
                if not ok:
                    return
                img = Image.fromarray(frame[..., ::-1])
            except Exception:
                return
        else:
            img = Image.open(src)
        img.thumbnail(THUMB_SIZE)
        img.save(dest, "JPEG")
    except Exception:
        pass

def thumb_path(album: str, filename: str) -> str:
    d = os.path.join(UPLOAD_ROOT, album, THUMB_DIR)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, filename + ".jpg")

# ---------- Range 流式播放 ----------

def partial_response(path: str, mime: str):
    """实现 HTTP Range 支持（仅用于视频）。"""
    file_size = os.path.getsize(path)
    range_header = request.headers.get("Range", None)
    if range_header is None:
        return send_file(path, mimetype=mime)

    byte1, byte2 = 0, None
    if "=" in range_header:
        range_val = range_header.split("=")[1]
        if "-" in range_val:
            byte1, byte2 = range_val.split("-")
    byte1 = int(byte1)
    byte2 = int(byte2) if byte2 else file_size - 1
    length = byte2 - byte1 + 1

    with open(path, "rb") as f:
        f.seek(byte1)
        data = f.read(length)
    resp = Response(data, 206, mimetype=mime, direct_passthrough=True)
    resp.headers.add("Content-Range", f"bytes {byte1}-{byte2}/{file_size}")
    resp.headers.add("Accept-Ranges", "bytes")
    return resp


# ------------------ 路由 ------------------
@app.route("/")
def index():
    albums=[d for d in os.listdir(UPLOAD_ROOT) if os.path.isdir(os.path.join(UPLOAD_ROOT,d))]
    return render_template('index.html', albums=sorted(albums))

@app.route("/<album_name>/stream/<path:filename>")
def stream(album_name, filename):
    album=safe_album(album_name)
    if not album:
        abort(404)
    path=os.path.join(UPLOAD_ROOT, album, filename)
    if not os.path.isfile(path):
        abort(404)
    mime=mimetypes.guess_type(path)[0] or 'application/octet-stream'
    return partial_response(path, mime)

@app.route("/<album_name>/thumb/<path:filename>")
def thumb(album_name, filename):
    album=safe_album(album_name)
    if not album:
        abort(404)
    src=os.path.join(UPLOAD_ROOT, album, filename)
    if not os.path.isfile(src):
        abort(404)
    tp=thumb_path(album, filename)
    if not os.path.isfile(tp):
        make_thumb(src, tp)
    if os.path.isfile(tp):
        return send_file(tp, mimetype='image/jpeg')
    return send_file(io.BytesIO(PLACEHOLDER), mimetype='image/gif')

@app.route("/<album_name>/preview/<path:filename>")
def preview(album_name, filename):
    album=safe_album(album_name)
    if not album:
        abort(404)
    src=os.path.join(UPLOAD_ROOT, album, filename)
    if not os.path.isfile(src):
        abort(404)
    ext=os.path.splitext(src)[1].lower()
    if ext in RAW_EXTS:
        try:
            from PIL import Image
            import rawpy
            with rawpy.imread(src) as r:
                img = Image.fromarray(r.postprocess())
            buf=io.BytesIO(); img.save(buf,'JPEG'); buf.seek(0)
            return send_file(buf, mimetype='image/jpeg')
        except Exception:
            pass
    return send_file(src)

@app.route("/<album_name>/download/<path:filename>")
def download_file_get(album_name, filename):
    album=safe_album(album_name)
    if not album:
        abort(404)
    path=os.path.join(UPLOAD_ROOT, album, filename)
    if not os.path.isfile(path):
        abort(404)
    return send_file(path, as_attachment=True, download_name=filename)

@app.route("/<album_name>/delete", methods=['POST'])
def delete_file(album_name):
    album=safe_album(album_name); data=request.get_json(force=True)
    fname=data.get('file','')
    path=os.path.join(UPLOAD_ROOT, album, fname)
    if os.path.isfile(path):
        os.remove(path)
    return jsonify({'ok':True})

@app.route("/<album_name>/delete_all", methods=['POST'])
def delete_all(album_name):
    album=safe_album(album_name)
    path=os.path.join(UPLOAD_ROOT, album)
    if os.path.isdir(path):
        shutil.rmtree(path)
        os.makedirs(path, exist_ok=True)
    return jsonify({'ok':True})

@app.route("/<album_name>/pack")
def pack_zip(album_name):
    album=safe_album(album_name)
    path=os.path.join(UPLOAD_ROOT, album)
    if not os.path.isdir(path):
        abort(404)
    files=[f for f in os.listdir(path) if os.path.isfile(os.path.join(path,f))]
    total=len(files) or 1
    zpath=os.path.join(path, f"{album}.zip")
    def gen():
        with zipfile.ZipFile(zpath,'w',zipfile.ZIP_DEFLATED) as z:
            for i,f in enumerate(files,1):
                z.write(os.path.join(path,f), arcname=f)
                yield f"data:{i/total:.4f}\n\n"
        yield "data:done\n\n"
    return Response(gen(), mimetype='text/event-stream')

@app.route("/<album_name>", methods=['GET','POST'])
def album(album_name):
    album=safe_album(album_name)
    if not album:
        abort(404)
    path=os.path.join(UPLOAD_ROOT, album); os.makedirs(path, exist_ok=True)
    if request.method=='POST':
        futures=[]
        for f in request.files.getlist('file'):
            if not f or f.filename=='':
                continue
            fname=sanitize_filename(f.filename)
            if not allowed(fname):
                flash(f'类型不允许: {fname}')
                continue
            dest=os.path.join(path, fname)
            def task(fileobj, d, name):
                fileobj.save(d)
                make_thumb(d, thumb_path(album, name))
                print(f"[{datetime.now():%H:%M:%S}] {name} SHA256={sha256(d)}")
            futures.append(executor.submit(task, f, dest, fname))
        for ft in futures:
            ft.result()
        return ('',200)
    files=sorted([x for x in os.listdir(path) if os.path.isfile(os.path.join(path,x))], key=lambda x: os.path.getmtime(os.path.join(path,x)), reverse=True)
    return render_template('album.html', album=album, files=files)

@app.route("/<album_name>/download_all")
def download_all(album_name):
    album=safe_album(album_name)
    zpath=os.path.join(UPLOAD_ROOT, album, f"{album}.zip")
    if not os.path.isfile(zpath):
        abort(404)
    resp=send_file(zpath, as_attachment=True, download_name=f"{album}.zip")
    @resp.call_on_close
    def cleanup():
        try:
            os.remove(zpath)
        except Exception:
            pass
    return resp

if __name__=='__main__':
    print('运行: http://127.0.0.1:5000')
    app.run('0.0.0.0',5000,debug=False)
