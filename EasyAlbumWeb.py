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
from datetime import datetime
from flask import (
    Flask, request, render_template_string, abort, flash, send_file, Response, redirect, url_for, jsonify
)
from werkzeug.utils import secure_filename

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

# ------------------ HTML 模板 ------------------
INDEX = """<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><title>相册</title>
<style>body{font-family:system-ui,Arial,sans-serif;margin:0;padding:2rem 1rem;max-width:800px;}
.album{display:block;padding:.75rem 1rem;margin:.4rem 0;border-radius:8px;background:#f5f5f5;text-decoration:none;color:#333;box-shadow:inset 0 0 0 1px #ddd;}
.album:hover{background:#eee;}</style></head><body>
<h2>全部相册</h2>
{% for a in albums %}<a class='album' href='{{ url_for('album', album_name=a) }}'>{{ a }}</a>{% endfor %}
{% if not albums %}<p>暂无相册，在地址栏输入 <kbd>/album_name</kbd> 创建。</p>{% endif %}
</body></html>"""

ALBUM = """<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><title>{{ album }}</title>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<style>
:root{--gap:12px;--card-bg:#fff;--primary:#1976d2;--radius:8px;}
*{box-sizing:border-box;}body{font-family:system-ui,Arial,sans-serif;margin:0;padding:var(--gap);background:#fafafa;}
#upload{background:var(--card-bg);padding:var(--gap);border-radius:var(--radius);box-shadow:0 1px 3px rgba(0,0,0,.1);}
button,.btn{background:var(--primary);color:#fff;border:none;border-radius:4px;padding:.5rem 1rem;margin-right:.5rem;text-decoration:none;display:inline-block;cursor:pointer;}
button:disabled{opacity:.4;}
#progwrap{display:none;height:20px;background:#e0e0e0;border-radius:10px;margin-top:6px;overflow:hidden;}#progbar{height:100%;width:0;background:#4caf50;transition:width .2s;}#progtext{font-size:.85rem;margin-top:4px;}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:var(--gap);margin-top:var(--gap);}figure{margin:0;padding:var(--gap);background:var(--card-bg);border-radius:var(--radius);box-shadow:0 1px 3px rgba(0,0,0,.08);display:flex;flex-direction:column;align-items:center;}
figure img,figure video{width:100%;border-radius:4px;cursor:pointer;}figcaption{margin-top:4px;font-size:.8rem;word-break:break-all;text-align:center;}
#overlay{position:fixed;inset:0;display:none;background:rgba(0,0,0,.85);align-items:center;justify-content:center;z-index:999;}#overlay img,#overlay video{max-width:90vw;max-height:90vh;border-radius:6px;}
</style></head><body>
<h2>{{ album }}</h2>
<div id='upload'><input type='file' id='files' multiple>
<button id='btnup' class='btn' onclick='upload()'>上传</button>
<button class='btn' onclick='delAll()'>清空相册</button>
<a id='btnpack' class='btn' href='javascript:void(0)' onclick='pack()'>打包下载</a>
<div id='progwrap'><div id='progbar'></div></div><div id='progtext'></div></div>
{% with m=get_flashed_messages() %}{% if m %}<ul>{% for x in m %}<li style='color:#d32f2f;'>{{ x }}</li>{% endfor %}</ul>{% endif %}{% endwith %}
<div class='grid'>
{% for f in files %}{% set ext=f.split('.')[-1]|lower %}<figure>{% if ext in ['mp4','webm','ogg','avi','mov','mkv'] %}<video src='{{ url_for('stream', album_name=album, filename=f) }}#t=0.1' data-full='{{ url_for('stream', album_name=album, filename=f) }}' preload='metadata' muted onclick='showVideo(this)'></video>{% elif ext in ['dng','raw','nef','cr2','arw','rw2'] %}<img src='{{ url_for('thumb', album_name=album, filename=f) }}' data-full='{{ url_for('preview', album_name=album, filename=f) }}' onclick='showImage(this)'>{% else %}<img src='{{ url_for('thumb', album_name=album, filename=f) }}' data-full='{{ url_for('static', filename=album+'/'+f) }}' onclick='showImage(this)'>{% endif %}
<figcaption>{{ f }}</figcaption>
<div><a class='btn' href='{{ url_for('download_file_get', album_name=album, filename=f) }}'>下载</a> <button class='btn' onclick="delFile('{{ f }}')">删除</button></div>
</figure>{% endfor %}
</div>
<div id='overlay' onclick='hide()'><img><video controls style='display:none'></video></div>
<script>const pw=document.getElementById('progwrap'),pb=document.getElementById('progbar'),pt=document.getElementById('progtext');
function fmt(b){const u=['B','KB','MB','GB','TB'];let i=0;while(b>=1024&&i<u.length-1){b/=1024;++i;}return b.toFixed(i?1:0)+' '+u[i];}
async function upload(){const fs=[...document.getElementById('files').files];if(!fs.length)return alert('请选择文件');if(!confirm(`确定上传 ${fs.length} 个文件吗?`))return;document.getElementById('btnup').disabled=true;pw.style.display='block';let done=0,bytes=0,t=fs.reduce((a,b)=>a+b.size,0);
 const doOne=f=>new Promise(r=>{const fd=new FormData();fd.append('file',f);const x=new XMLHttpRequest();x.open('POST',location.pathname);x.upload.onprogress=e=>{pb.style.width=((bytes+e.loaded)/t*100).toFixed(1)+'%';pt.textContent=`${done}/${fs.length}  ${fmt(bytes+e.loaded)} / ${fmt(t)}`;};x.onload=()=>{bytes+=f.size;done++;r();};x.onerror=()=>{alert('失败 '+f.name);r();};x.send(fd);});
 for(const f of fs)await doOne(f);location.reload();}
function delFile(name){if(!confirm('删除 '+name+' ?'))return;fetch(location.pathname+'/delete', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file:name})}).then(()=>location.reload());}
function delAll(){if(!confirm('确定清空相册?'))return;fetch(location.pathname+'/delete_all',{method:'POST'}).then(()=>location.reload());}
function pack(){const es=new EventSource(location.pathname+'/pack');pw.style.display='block';pb.style.width='0';pt.textContent='';es.onmessage=e=>{if(e.data==='done'){es.close();location.href=location.pathname+'/download_all';}else{pb.style.width=(parseFloat(e.data)*100).toFixed(1)+'%';pt.textContent='打包 '+(parseFloat(e.data)*100).toFixed(0)+'%';}};}
function showImage(el){const o=document.getElementById('overlay');const img=o.querySelector('img');const v=o.querySelector('video');v.pause();v.style.display='none';v.src='';img.style.display='block';img.src=el.dataset.full;o.style.display='flex';}
function showVideo(el){const o=document.getElementById('overlay');const img=o.querySelector('img');const v=o.querySelector('video');img.style.display='none';img.src='';v.style.display='block';v.src=el.dataset.full;o.style.display='flex';v.play();}
function hide(){const o=document.getElementById('overlay');o.style.display='none';const v=o.querySelector('video');v.pause();v.src='';}
</script></body></html>"""

# ------------------ 路由 ------------------
@app.route("/")
def index():
    albums=[d for d in os.listdir(UPLOAD_ROOT) if os.path.isdir(os.path.join(UPLOAD_ROOT,d))]
    return render_template_string(INDEX, albums=sorted(albums))

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
        for f in request.files.getlist('file'):
            if not f or f.filename=='':
                continue
            fname=secure_filename(f.filename)
            if not allowed(fname):
                flash(f'类型不允许: {fname}'); continue
            dest=os.path.join(path, fname)
            f.save(dest)
            make_thumb(dest, thumb_path(album, fname))
            print(f"[{datetime.now():%H:%M:%S}] {fname} SHA256={sha256(dest)}")
        return ('',200)
    files=sorted([x for x in os.listdir(path) if os.path.isfile(os.path.join(path,x))], key=lambda x: os.path.getmtime(os.path.join(path,x)), reverse=True)
    return render_template_string(ALBUM, album=album, files=files)

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
