#!/usr/bin/env python3
# coding: utf-8
"""
本地图片/视频分享站（单文件）— 懒加载 & RAW 支持版
---------------------------------------------------
变更摘要
1. **懒加载缩略图**：页面只加载 320 px 的缩略图/占位，点击后才请求原始图片或视频数据。
2. **RAW 镜像支持**：允许上传主流 RAW 格式（CR2/NEF/ARW/DNG/ORF/RW2 等），自动提取缩略图并可在线预览、下载。
3. 其余删除、清空、Range 流、移动端自适应等功能保持。依赖新增 Pillow(>=10)；若要预览 RAW，建议安装 rawpy+imageio。

运行前：
```bash
pip install flask pillow
# 可选 RAW 预览
pip install rawpy imageio
```
"""
import os, shutil, hashlib, mimetypes, io
from datetime import datetime
from pathlib import Path
from flask import (
    Flask, request, render_template_string, abort, flash, send_file,
    Response, jsonify
)
from werkzeug.utils import secure_filename
from PIL import Image, ImageOps

try:
    import rawpy, imageio
    RAW_ENABLED = True
except ImportError:
    RAW_ENABLED = False  # 若无 rawpy 依旧可用，但无法预览 RAW

# ------------------ 配置 ------------------
UPLOAD_ROOT = Path("uploads").resolve()
THUMB_SIZE = 320                               # 宽度 320px
THUMB_DIRNAME = ".thumbs"                    # 相册内缩略图子目录
ALLOWED_EXTS = {
    # 图片
    ".jpg", ".jpeg", ".png", ".gif", ".bmp",
    # 视频
    ".mp4", ".webm", ".ogg", ".avi", ".mov", ".mkv",
    # RAW
    ".cr2", ".nef", ".arw", ".dng", ".rw2", ".orf", ".raf", ".pef", ".srw", ".raw"
}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp"}
VIDEO_EXTS = {".mp4", ".webm", ".ogg", ".avi", ".mov", ".mkv"}
RAW_EXTS   = ALLOWED_EXTS - IMAGE_EXTS - VIDEO_EXTS
MAX_CONTENT_LENGTH = 5 * 1024 * 1024 * 1024  # 5 GB/请求

app = Flask(__name__, static_folder=str(UPLOAD_ROOT), static_url_path="/uploads")
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH
app.secret_key = "local-secret"
UPLOAD_ROOT.mkdir(exist_ok=True)

# ------------------ 工具 ------------------

def allowed(fn: str) -> bool:
    return Path(fn).suffix.lower() in ALLOWED_EXTS

def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

def safe_album(name: str) -> str:
    return "".join(c for c in name if c.isalnum() or c == "_")

# ---------- 缩略图生成 ----------

def thumb_path(file_path: Path) -> Path:
    return file_path.parent / THUMB_DIRNAME / (file_path.stem + ".jpg")

def ensure_thumbnail(file_path: Path):
    album_thumb_dir = file_path.parent / THUMB_DIRNAME
    album_thumb_dir.mkdir(exist_ok=True)
    tpath = thumb_path(file_path)
    if tpath.exists():
        return tpath
    ext = file_path.suffix.lower()
    try:
        if ext in IMAGE_EXTS:
            img = Image.open(file_path)
        elif ext in RAW_EXTS and RAW_ENABLED:
            with rawpy.imread(str(file_path)) as raw:
                img_data = raw.postprocess(use_camera_wb=True, half_size=True)
            img = Image.fromarray(img_data)
        else:
            # 视频或不支持 -> 返回占位缩略图
            return None
        img = ImageOps.exif_transpose(img)
        img.thumbnail((THUMB_SIZE, THUMB_SIZE))
        img.save(tpath, "JPEG", quality=85)
        return tpath
    except Exception as e:
        print("Thumbnail error", e)
        return None

# ---------- 视频 Range 流 ----------

def partial_response(path: Path, mime: str):
    fsize = path.stat().st_size
    rng = request.headers.get("Range", None)
    if rng is None:
        return send_file(path, mimetype=mime)
    byte1, byte2 = 0, None
    if "=" in rng:
        rng = rng.split("=")[1]
        if "-" in rng:
            byte1, byte2 = rng.split("-")
    byte1 = int(byte1)
    byte2 = int(byte2) if byte2 else fsize - 1
    length = byte2 - byte1 + 1
    with path.open("rb") as f:
        f.seek(byte1)
        data = f.read(length)
    resp = Response(data, 206, mimetype=mime, direct_passthrough=True)
    resp.headers.add("Content-Range", f"bytes {byte1}-{byte2}/{fsize}")
    resp.headers.add("Accept-Ranges", "bytes")
    return resp

# ------------------ 模板 ------------------
INDEX_HTML = """<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><title>相册</title>
<style>body{font-family:system-ui,Arial,sans-serif;margin:0;padding:2rem 1rem;max-width:900px;}a.album{display:block;padding:.8rem 1rem;margin:.4rem 0;border-radius:8px;background:#f5f5f5;text-decoration:none;color:#333;box-shadow:inset 0 0 0 1px #ddd;}a.album:hover{background:#eee;}</style></head><body>
<h2>全部相册</h2>{% for a in albums %}<a class='album' href='{{ url_for('album', album_name=a) }}'>{{ a }}</a>{% endfor %}{% if not albums %}<p>暂无相册，在地址栏输入 <kbd>/album_name</kbd> 创建。</p>{% endif %}</body></html>"""

ALBUM_HTML = """<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><title>{{ album }}</title><meta name='viewport' content='width=device-width,initial-scale=1'>
<style>:root{--gap:12px;--bg:#fff;--pri:#1976d2;--rad:8px;}*{box-sizing:border-box;}body{font-family:system-ui,Arial,sans-serif;margin:0;padding:var(--gap);background:#fafafa;}#up{background:var(--bg);padding:var(--gap);border-radius:var(--rad);box-shadow:0 1px 3px rgba(0,0,0,.08);}button{background:var(--pri);color:#fff;border:none;border-radius:4px;padding:.5rem 1rem;margin-right:.5rem;}#prog{display:none;height:20px;background:#e0e0e0;border-radius:10px;margin-top:6px;overflow:hidden;}#bar{height:100%;width:0;background:#4caf50;transition:width .2s;}#txt{font-size:.85rem;margin-top:4px;}grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:var(--gap);margin-top:var(--gap);}figure{margin:0;padding:var(--gap);background:var(--bg);border-radius:var(--rad);box-shadow:0 1px 3px rgba(0,0,0,.06);display:flex;flex-direction:column;align-items:center;}figcaption{margin-top:4px;font-size:.75rem;word-break:break-all;text-align:center;width:100%;}figure img,div.vid-thumb{max-width:100%;border-radius:4px;cursor:pointer;}div.vid-thumb{background:#000;position:relative;padding-top:56%;width:100%;}div.vid-thumb i{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);font-size:48px;color:#fff;}#ov{position:fixed;inset:0;display:none;background:rgba(0,0,0,.85);align-items:center;justify-content:center;z-index:999;}#ov img{max-width:90vw;max-height:90vh;border-radius:8px;}</style></head><body>
<h2>{{ album }}</h2><div id='up'><input type='file' id='files' multiple><button id='u' onclick='up()'>上传</button><button onclick='clearAlbum()'>清空相册</button><div id='prog'><div id='bar'></div></div><div id='txt'></div></div>{% with m=get_flashed_messages() %}{% if m %}<ul>{% for x in m %}<li style='color:#d32f2f;'>{{ x }}</li>{% endfor %}</ul>{% endif %}{% endwith %}
<grid>
{% for f in files %}{% set ext=f.split('.')[-1]|lower %}<figure>
{% if ext in VIDEO_EXTS %}<div class='vid-thumb' data-src='{{ url_for('stream', album_name=album, filename=f) }}' onclick='playVideo(this)'><i>▶</i></div>{% else %}<img src='{{ url_for('thumb', album_name=album, filename=f) }}' data-full='{{ url_for('static', filename=album+'/'+f) }}' onclick='zoom(this.dataset.full)'>{% endif %}
<figcaption>{{ f }}</figcaption><button onclick="delFile('{{ f }}')">删除</button></figure>{% endfor %}
</grid>
<div id='ov' onclick='hide()'><img></div>
<script>const pWrap=document.getElementById('prog'),bar=document.getElementById('bar'),txt=document.getElementById('txt');function fmt(b){const u=['B','KB','MB','GB','TB'];let i=0;while(b>=1024&&i<u.length-1){b/=1024;i++;}return b.toFixed(i?1:0)+' '+u[i];}
async function up(){const fs=[...document.getElementById('files').files];if(!fs.length)return alert('请选择文件');if(!confirm(`上传 ${fs.length} 个文件?`))return;document.getElementById('u').disabled=true;pWrap.style.display='block';let done=0,bytes=0,total=fs.reduce((a,b)=>a+b.size,0);const send=f=>new Promise(r=>{const fd=new FormData();fd.append('file',f);const x=new XMLHttpRequest();x.open('POST',location.pathname);x.upload.onprogress=e=>{bar.style.width=((bytes+e.loaded)/total*100).toFixed(1)+'%';txt.textContent=`${done}/${fs.length}  ${fmt(bytes+e.loaded)} / ${fmt(total)}`};x.onload=()=>{bytes+=f.size;done++;r();};x.onerror=()=>{alert('失败 '+f.name);r();};x.send(fd);});for(const f of fs)await send(f);location.reload();}
function delFile(n){if(!confirm('删除 '+n+'?'))return;fetch(location.pathname+'/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file:n})}).then(()=>location.reload());}
function clearAlbum(){if(!confirm('清空相册?'))return;fetch(location.pathname+'/delete_all',{method:'POST'}).then(()=>location.reload());}
function zoom(src){const o=document.getElementById('ov');o.style.display='flex';o.querySelector('img').src=src;}
function hide(){document.getElementById('ov').style.display='none';}
function playVideo(el){const src=el.dataset.src;const v=document.createElement('video');v.src=src;v.controls=true;v.style.maxWidth='100%';el.parentNode.replaceChild(v, el);v.play();}
</script></body></html>"""

# ------------------ 路由 ------------------
@app.route("/")
def index():
    albums=[d.name for d in UPLOAD_ROOT.iterdir() if d.is_dir()]
    return render_template_string(INDEX_HTML, albums=sorted(albums))

@app.route("/<album_name>/thumb/<path:filename>")
def thumb(album_name, filename):
    album = safe_album(album_name)
    if not album:
        abort(404)
    fpath = UPLOAD_ROOT / album / filename
    if not fpath.exists():
        abort(404)
    tpath = ensure_thumbnail(fpath)
    if tpath and tpath.exists():
        return send_file(tpath, mimetype="image/jpeg")
    # fallback 占位
    placeholder = Image.new("RGB", (THUMB_SIZE, THUMB_SIZE), color=(200,200,200))
    buf = io.BytesIO(); placeholder.save(buf, "JPEG"); buf.seek
