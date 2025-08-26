#!/usr/bin/env python3
# coding: utf-8
"""
本地图片/视频分享站（单文件）— 功能增强版
新增：
1. 删除单个文件 & 清空相册（确认弹窗）。
2. 优化移动端布局（更自然的卡片宽度与间距）。
3. 视频 Range 流式播放（支持拖动 / 极速加载）。
"""

import os, shutil, hashlib, mimetypes, io, zipfile, base64
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
    ".mp4", ".webm", ".ogg", ".avi", ".mov", ".mkv",
    ".zip"
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
AUTO_PROGRESS = {}
AUTO_RESULT = {}
PLACEHOLDER = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00"
    b"\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
)

# Format bytes for templates
def fmt_bytes(b: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    while b >= 1024 and i < len(units) - 1:
        b /= 1024
        i += 1
    return f"{b:.1f} {units[i]}" if i else f"{int(b)} B"

app.jinja_env.filters["fmt_bytes"] = fmt_bytes

# Format timestamp for templates
def fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")

app.jinja_env.filters["fmt_time"] = fmt_time

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

def valid_album(name: str) -> bool:
    """Return True if the given name is a legal album path segment."""
    import re
    return bool(re.fullmatch(r"[A-Za-z0-9_]+", name))

def sanitize_filename(name: str) -> str:
    """Allow UTF-8 filenames while stripping path separators and control chars."""
    name = os.path.basename(name)
    name = name.replace("\x00", "")
    import re
    return re.sub(r"[^\w\u4e00-\u9fff().\- ]+", "_", name)

def get_meta_time(path: str) -> float:
    """Return creation time from metadata if possible."""
    ext = os.path.splitext(path)[1].lower()
    if ext in IMAGE_EXTS | RAW_EXTS:
        try:
            from PIL import Image, ExifTags
            img = Image.open(path)
            exif = img._getexif() or {}
            tags = {ExifTags.TAGS.get(k, k): v for k, v in exif.items()}
            for key in ("DateTimeOriginal", "DateTimeDigitized", "DateTime"):
                if key in tags:
                    try:
                        dt = datetime.strptime(str(tags[key]), "%Y:%m:%d %H:%M:%S")
                        return dt.timestamp()
                    except Exception:
                        continue
        except Exception:
            pass
    return os.path.getmtime(path)

def make_thumb(src: str, dest: str):
    """Create thumbnail for image/raw/video files."""
    try:
        from PIL import Image
    except Exception:
        return
    ext = os.path.splitext(src)[1].lower()
    try:
        if ext in RAW_EXTS:
            try:
                import rawpy
            except Exception:
                return
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
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        img.thumbnail(THUMB_SIZE)
        img.save(dest, "JPEG")
    except Exception:
        pass

def thumb_path(album: str, filename: str) -> str:
    d = os.path.join(UPLOAD_ROOT, album, THUMB_DIR)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, filename + ".jpg")

def snapshot_dir(album: str, filename: str) -> str:
    d = os.path.join(UPLOAD_ROOT, album, ".review", filename)
    os.makedirs(d, exist_ok=True)
    return d

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

@app.route("/<album_name>/review/<path:filename>")
def review(album_name, filename):
    album = safe_album(album_name)
    if not album:
        abort(404)
    fname = sanitize_filename(filename)
    path = os.path.join(UPLOAD_ROOT, album, fname)
    if not os.path.isfile(path):
        abort(404)
    ext = os.path.splitext(fname)[1].lower()
    if ext not in VIDEO_EXTS:
        abort(404)
    return render_template('review.html', album=album, filename=fname)

@app.route("/<album_name>/review/<path:filename>/snapshots", methods=['GET','POST'])
def review_snapshots(album_name, filename):
    album = safe_album(album_name)
    if not album:
        abort(404)
    fname = sanitize_filename(filename)
    src = os.path.join(UPLOAD_ROOT, album, fname)
    if not os.path.isfile(src):
        abort(404)
    d = snapshot_dir(album, fname)
    if request.method == 'GET':
        items = []
        for n in os.listdir(d):
            if n.lower().endswith('.jpg'):
                base = os.path.splitext(n)[0]
                part = base.rsplit('_', 1)[-1]
                try:
                    t = float(part)
                except Exception:
                    t = 0.0
                items.append({'name': n, 'time': t})
        items.sort(key=lambda x: x['time'])
        return jsonify(items)
    data = request.get_json(force=True)
    b64 = data.get('image', '')
    t = float(data.get('time', 0))
    if ',' in b64:
        b64 = b64.split(',', 1)[1]
    img = base64.b64decode(b64)
    name = f"{t:.3f}.jpg"
    with open(os.path.join(d, name), 'wb') as f:
        f.write(img)
    return jsonify({'ok': True, 'name': name, 'time': t})

@app.route("/<album_name>/review/<path:filename>/snapshots/<snap_name>")
def review_snapshot_file(album_name, filename, snap_name):
    album = safe_album(album_name)
    fname = sanitize_filename(filename)
    snap = sanitize_filename(snap_name)
    path = os.path.join(snapshot_dir(album, fname), snap)
    if not os.path.isfile(path):
        abort(404)
    return send_file(path, mimetype='image/jpeg')

@app.route("/<album_name>/review/<path:filename>/snapshots/<snap_name>/rename", methods=['POST'])
def review_snapshot_rename(album_name, filename, snap_name):
    album = safe_album(album_name)
    fname = sanitize_filename(filename)
    snap = sanitize_filename(snap_name)
    data = request.get_json(force=True)
    prefix = sanitize_filename(data.get('prefix', ''))
    d = snapshot_dir(album, fname)
    src = os.path.join(d, snap)
    if not os.path.isfile(src):
        abort(404)
    base = os.path.splitext(snap)[0]
    part = base.rsplit('_', 1)[-1]
    new_base = f"{prefix}_{part}" if prefix else part
    new = new_base + '.jpg'
    dst = os.path.join(d, new)
    if os.path.isfile(dst):
        return jsonify({'ok': False, 'msg': 'exists'}), 400
    os.rename(src, dst)
    return jsonify({'ok': True, 'name': new})

@app.route("/<album_name>/review/<path:filename>/snapshots/<snap_name>/delete", methods=['POST'])
def review_snapshot_delete(album_name, filename, snap_name):
    album = safe_album(album_name)
    fname = sanitize_filename(filename)
    snap = sanitize_filename(snap_name)
    d = snapshot_dir(album, fname)
    path = os.path.join(d, snap)
    if os.path.isfile(path):
        os.remove(path)
    return jsonify({'ok': True})

@app.route("/<album_name>/review/<path:filename>/snapshots/delete_all", methods=['POST'])
def review_snapshot_delete_all(album_name, filename):
    album = safe_album(album_name)
    fname = sanitize_filename(filename)
    d = snapshot_dir(album, fname)
    for n in os.listdir(d):
        if n.lower().endswith('.jpg'):
            try:
                os.remove(os.path.join(d, n))
            except FileNotFoundError:
                pass
    return jsonify({'ok': True})


def _auto_split_worker(album: str, fname: str, threshold: float):
    """Background worker performing scene detection and snapshot saving."""
    key = f"{album}/{fname}"
    src = os.path.join(UPLOAD_ROOT, album, fname)
    out_dir = snapshot_dir(album, fname)
    try:
        from scenedetect import VideoManager, SceneManager
        from scenedetect.detectors import ContentDetector
        import cv2
    except Exception:
        AUTO_RESULT[key] = {'ok': False, 'msg': 'missing dependency', 'saved': 0}
        AUTO_PROGRESS.pop(key, None)
        return

    video_manager = VideoManager([src])
    scene_manager = SceneManager()
    scene_manager.add_detector(ContentDetector(threshold=threshold))
    video_manager.start()
    scene_manager.detect_scenes(frame_source=video_manager)
    scene_list = scene_manager.get_scene_list()
    video_manager.release()

    cap = cv2.VideoCapture(src)
    if not cap.isOpened():
        AUTO_RESULT[key] = {'ok': False, 'msg': 'open failed', 'saved': 0}
        AUTO_PROGRESS.pop(key, None)
        return
    fps = cap.get(cv2.CAP_PROP_FPS)
    saved = 0

    def save_frame(path, frame):
        ret, buf = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
        if ret:
            buf.tofile(path)

    # After detection done, mark half progress
    AUTO_PROGRESS[key] = 0.5

    if not scene_list:
        frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT)
        if frame_count > 0:
            mid_f = int(frame_count // 2)
            cap.set(cv2.CAP_PROP_POS_FRAMES, mid_f)
            ok, frame = cap.read()
            if ok and frame is not None:
                actual = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0 if fps > 0 else 0.0
                name = f"场景1_{actual:.3f}.jpg"
                save_frame(os.path.join(out_dir, name), frame)
                saved = 1
        cap.release()
        AUTO_RESULT[key] = {'ok': True, 'saved': saved}
        AUTO_PROGRESS.pop(key, None)
        return

    total = len(scene_list)
    for i, (start_tc, end_tc) in enumerate(scene_list, start=1):
        start_f = start_tc.get_frames()
        end_f = end_tc.get_frames()
        if end_f <= start_f:
            continue
        mid_f = (start_f + end_f) // 2
        cap.set(cv2.CAP_PROP_POS_FRAMES, mid_f)
        ok, frame = cap.read()
        if not ok or frame is None:
            continue
        actual = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0 if fps > 0 else 0.0
        name = f"场景{i}_{actual:.3f}.jpg"
        save_frame(os.path.join(out_dir, name), frame)
        saved += 1
        AUTO_PROGRESS[key] = 0.5 + 0.5 * (i / total)
    cap.release()
    AUTO_RESULT[key] = {'ok': True, 'saved': saved}
    AUTO_PROGRESS.pop(key, None)


@app.route("/<album_name>/review/<path:filename>/snapshots/auto", methods=['POST'])
def review_snapshot_auto(album_name, filename):
    """Kick off background auto scene detection."""
    album = safe_album(album_name)
    fname = sanitize_filename(filename)
    src = os.path.join(UPLOAD_ROOT, album, fname)
    if not os.path.isfile(src):
        abort(404)
    key = f"{album}/{fname}"
    if key in AUTO_PROGRESS:
        return jsonify({'ok': False, 'msg': 'busy'}), 409

    data = request.get_json(silent=True) or {}
    try:
        threshold = float(data.get('threshold', 27))
    except Exception:
        threshold = 27.0

    AUTO_PROGRESS[key] = 0.0
    AUTO_RESULT.pop(key, None)
    executor.submit(_auto_split_worker, album, fname, threshold)
    return jsonify({'ok': True})


@app.route("/<album_name>/review/<path:filename>/snapshots/auto/progress")
def review_snapshot_auto_progress(album_name, filename):
    album = safe_album(album_name)
    fname = sanitize_filename(filename)
    key = f"{album}/{fname}"
    p = AUTO_PROGRESS.get(key)
    res = AUTO_RESULT.get(key, {})
    done = p is None
    return jsonify({'ok': True, 'progress': p if p is not None else 1.0, 'done': done, **res})


@app.route("/<album_name>/export/<path:filename>")
def export_video(album_name, filename):
    album = safe_album(album_name)
    fname = sanitize_filename(filename)
    path = os.path.join(UPLOAD_ROOT, album, fname)
    if not os.path.isfile(path) or os.path.splitext(fname)[1].lower() not in VIDEO_EXTS:
        abort(404)
    snap_dir = os.path.join(UPLOAD_ROOT, album, '.review', fname)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        z.write(path, arcname=fname)
        if os.path.isdir(snap_dir):
            for n in os.listdir(snap_dir):
                if n.lower().endswith('.jpg'):
                    z.write(os.path.join(snap_dir, n), arcname=n)
    buf.seek(0)
    dl = os.path.splitext(fname)[0] + '.zip'
    return send_file(buf, as_attachment=True, download_name=dl, mimetype='application/zip')

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
        with zipfile.ZipFile(zpath,'w',zipfile.ZIP_DEFLATED) as outer:
            for i,f in enumerate(files,1):
                src=os.path.join(path,f)
                ext=os.path.splitext(f)[1].lower()
                if ext in VIDEO_EXTS:
                    buf=io.BytesIO()
                    with zipfile.ZipFile(buf,'w',zipfile.ZIP_DEFLATED) as z:
                        z.write(src, arcname=f)
                        sdir=os.path.join(path, '.review', f)
                        if os.path.isdir(sdir):
                            for n in os.listdir(sdir):
                                if n.lower().endswith('.jpg'):
                                    z.write(os.path.join(sdir,n), arcname=n)
                    outer.writestr(os.path.splitext(f)[0]+'.zip', buf.getvalue())
                else:
                    outer.write(src, arcname=f)
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
            ext = os.path.splitext(fname)[1].lower()
            if ext == '.zip':
                try:
                    data = f.read()
                    with zipfile.ZipFile(io.BytesIO(data)) as z:
                        vids = [n for n in z.namelist() if os.path.splitext(n)[1].lower() in VIDEO_EXTS]
                        if len(vids) != 1:
                            flash(f'压缩包缺少视频: {fname}')
                            continue
                        vname = sanitize_filename(os.path.basename(vids[0]))
                        vdest = os.path.join(path, vname)
                        if os.path.exists(vdest) and request.args.get('overwrite') != '1':
                            return jsonify({'ok': False, 'msg': 'exists'}), 409
                        if os.path.exists(vdest):
                            os.remove(vdest)
                        thumb_old = thumb_path(album, vname)
                        if os.path.exists(thumb_old):
                            os.remove(thumb_old)
                        sdir = os.path.join(UPLOAD_ROOT, album, '.review', vname)
                        if os.path.isdir(sdir):
                            shutil.rmtree(sdir, ignore_errors=True)
                        os.makedirs(sdir, exist_ok=True)
                        with z.open(vids[0]) as vf, open(vdest, 'wb') as out:
                            shutil.copyfileobj(vf, out)
                        make_thumb(vdest, thumb_path(album, vname))
                        for n in z.namelist():
                            if n == vids[0] or n.endswith('/'):
                                continue
                            if os.path.splitext(n)[1].lower() == '.jpg':
                                bn = sanitize_filename(os.path.basename(n))
                                with z.open(n) as sf, open(os.path.join(sdir, bn), 'wb') as out:
                                    shutil.copyfileobj(sf, out)
                    continue
                except Exception:
                    flash(f'压缩包解析失败: {fname}')
                    continue
            if not allowed(fname):
                flash(f'类型不允许: {fname}')
                continue
            dest=os.path.join(path, fname)
            if os.path.exists(dest) and request.args.get('overwrite') != '1':
                return jsonify({'ok': False, 'msg': 'exists'}), 409
            if os.path.exists(dest):
                os.remove(dest)
                tp = thumb_path(album, fname)
                if os.path.exists(tp):
                    os.remove(tp)
                rev = os.path.join(UPLOAD_ROOT, album, '.review', fname)
                if os.path.isdir(rev):
                    shutil.rmtree(rev, ignore_errors=True)
            def task(fileobj, d, name):
                fileobj.save(d)
                make_thumb(d, thumb_path(album, name))
                print(f"[{datetime.now():%H:%M:%S}] {name} SHA256={sha256(d)}")
            futures.append(executor.submit(task, f, dest, fname))
        for ft in futures:
            ft.result()
        return ('',200)
    sort = request.args.get('sort', 'mtime')
    order = request.args.get('order', 'desc')
    rev = (order == 'desc')
    items = []
    counts = {'image': 0, 'video': 0, 'raw': 0, 'other': 0}
    for name in os.listdir(path):
        fp = os.path.join(path, name)
        if not os.path.isfile(fp):
            continue
        ext = os.path.splitext(name)[1].lower()
        if ext in IMAGE_EXTS:
            counts['image'] += 1
        elif ext in VIDEO_EXTS:
            counts['video'] += 1
        elif ext in RAW_EXTS:
            counts['raw'] += 1
        else:
            counts['other'] += 1
        items.append({
            'name': name,
            'size': os.path.getsize(fp),
            'mtime': os.path.getmtime(fp),
            'ctime': get_meta_time(fp),
        })
    if sort == 'name':
        items.sort(key=lambda x: x['name'].lower(), reverse=rev)
    elif sort == 'size':
        items.sort(key=lambda x: x['size'], reverse=rev)
    elif sort == 'ctime':
        items.sort(key=lambda x: x['ctime'], reverse=rev)
    else:
        items.sort(key=lambda x: x['mtime'], reverse=rev)
        sort = 'mtime'
    if order not in {'asc','desc'}:
        order = 'desc'
    return render_template('album.html', album=album, files=items, sort=sort, order=order, counts=counts)

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


@app.route("/<album_name>/rename_file", methods=['POST'])
def rename_file(album_name):
    """Rename a single file within an album."""
    album = safe_album(album_name)
    data = request.get_json(force=True)
    old = data.get('old', '')
    newname = sanitize_filename(data.get('new', ''))
    if not old or not newname:
        return jsonify({'ok': False, 'msg': 'invalid'}), 400
    src = os.path.join(UPLOAD_ROOT, album, old)
    dst = os.path.join(UPLOAD_ROOT, album, newname)
    if not os.path.isfile(src):
        abort(404)
    if os.path.isfile(dst):
        return jsonify({'ok': False, 'msg': 'exists'}), 400
    os.rename(src, dst)
    tp_old = thumb_path(album, old)
    tp_new = thumb_path(album, newname)
    if os.path.isfile(tp_old):
        os.rename(tp_old, tp_new)
    rev_old = os.path.join(UPLOAD_ROOT, album, '.review', old)
    rev_new = os.path.join(UPLOAD_ROOT, album, '.review', newname)
    if os.path.isdir(rev_old):
        try:
            os.rename(rev_old, rev_new)
        except OSError:
            pass
    return jsonify({'ok': True, 'new': newname})


@app.route("/<album_name>/rename", methods=['POST'])
def rename_album(album_name):
    """Rename an album folder."""
    album = safe_album(album_name)
    data = request.get_json(force=True)
    newname = data.get('name', '')
    if not valid_album(newname):
        return jsonify({'ok': False, 'msg': 'invalid'}), 400
    new = safe_album(newname)
    src = os.path.join(UPLOAD_ROOT, album)
    dst = os.path.join(UPLOAD_ROOT, new)
    if not os.path.isdir(src):
        abort(404)
    if os.path.isdir(dst):
        return jsonify({'ok': False, 'msg': 'exists'}), 400
    os.rename(src, dst)
    return jsonify({'ok': True, 'new': new})

if __name__=='__main__':
    PORT = 5191
    app.run('0.0.0.0',PORT,debug=False)
