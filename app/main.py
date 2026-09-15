import json
import os
import sqlite3
import subprocess
from pathlib import Path

import httpx
import psutil
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from passlib.context import CryptContext
from starlette.middleware.sessions import SessionMiddleware

APP_NAME = "ArrStream"
APP_VERSION = "0.2.1"
PORT = int(os.getenv("PORT", "4321"))
CONFIG_DIR = Path(os.getenv("CONFIG_DIR", "/config"))
DB_PATH = CONFIG_DIR / "arrstream.db"
MEDIA_ROOT = os.getenv("MEDIA_ROOT", "/data")
SECRET_KEY = os.getenv("SECRET_KEY", "change-me-in-production")
VIDEO_EXTENSIONS = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".ts", ".m2ts", ".webm"}

CONFIG_DIR.mkdir(parents=True, exist_ok=True)
app = FastAPI(title=APP_NAME, version=APP_VERSION)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, same_site="lax", https_only=False)
templates = Jinja2Templates(directory="app/templates")
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                email TEXT,
                password_hash TEXT NOT NULL,
                is_admin INTEGER NOT NULL DEFAULT 1,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS instances (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL CHECK(kind IN ('radarr','sonarr')),
                name TEXT NOT NULL,
                url TEXT NOT NULL,
                api_key TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                version TEXT,
                root_folders TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_kind TEXT,
                source_instance_id INTEGER,
                media_path TEXT,
                status TEXT NOT NULL DEFAULT 'queued',
                action TEXT,
                progress REAL NOT NULL DEFAULT 0,
                input_bytes INTEGER DEFAULT 0,
                output_bytes INTEGER DEFAULT 0,
                error TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                finished_at DATETIME
            );
            CREATE TABLE IF NOT EXISTS library_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                media_path TEXT UNIQUE NOT NULL,
                source_kind TEXT,
                source_instance_id INTEGER,
                size_bytes INTEGER DEFAULT 0,
                container TEXT,
                video_codec TEXT,
                audio_codec TEXT,
                readiness TEXT NOT NULL DEFAULT 'unknown',
                action TEXT,
                error TEXT,
                last_scanned DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS scan_folders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT UNIQUE NOT NULL,
                label TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            """
        )


@app.on_event("startup")
def startup():
    init_db()


def user_count():
    with db() as conn:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def instance_count():
    with db() as conn:
        return conn.execute("SELECT COUNT(*) FROM instances WHERE enabled=1").fetchone()[0]


def setup_complete():
    return user_count() > 0 and instance_count() > 0


def require_login(request: Request):
    return request.session.get("user_id")


def page_guard(request: Request):
    if user_count() == 0:
        return RedirectResponse("/setup/admin", status_code=303)
    if not require_login(request):
        return RedirectResponse("/login", status_code=303)
    if instance_count() == 0 and not str(request.url.path).startswith("/setup/"):
        return RedirectResponse("/setup/instances", status_code=303)
    return None


def settings_dict():
    with db() as conn:
        return {r["key"]: r["value"] for r in conn.execute("SELECT key,value FROM settings")}


def save_settings(values):
    with db() as conn:
        for key, value in values.items():
            conn.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )


def detect_gpus():
    gpus = []
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=index,name,memory.total", "--format=csv,noheader,nounits"],
            stderr=subprocess.DEVNULL, text=True, timeout=3,
        )
        for line in out.strip().splitlines():
            idx, name, memory = [x.strip() for x in line.split(",", 2)]
            gpus.append({"id": f"nvidia-{idx}", "vendor": "NVIDIA", "index": idx, "name": name, "memory_mb": memory})
    except Exception:
        pass
    dri = Path("/dev/dri")
    if dri.exists():
        for card in sorted(dri.glob("renderD*")):
            gpus.append({"id": str(card), "vendor": "Intel/AMD", "index": card.name, "name": card.name, "memory_mb": None})
    return gpus


def ffmpeg_encoders():
    try:
        out = subprocess.check_output(["ffmpeg", "-hide_banner", "-encoders"], stderr=subprocess.STDOUT, text=True, timeout=5)
    except Exception:
        return []
    wanted = ["libx264", "libx265", "h264_nvenc", "hevc_nvenc", "h264_qsv", "hevc_qsv", "h264_vaapi", "hevc_vaapi"]
    return [x for x in wanted if x in out]


def probe_media(path):
    try:
        raw = subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=format_name:stream=codec_type,codec_name,pix_fmt", "-of", "json", path],
            stderr=subprocess.STDOUT, text=True, timeout=20,
        )
        data = json.loads(raw)
        streams = data.get("streams", [])
        video = next((x for x in streams if x.get("codec_type") == "video"), {})
        audio = next((x for x in streams if x.get("codec_type") == "audio"), {})
        container = (data.get("format", {}).get("format_name") or "unknown").split(",")[0]
        vcodec = video.get("codec_name") or "unknown"
        acodec = audio.get("codec_name") or "unknown"
        pix_fmt = video.get("pix_fmt") or ""
        if container in {"mov", "mp4", "m4a", "3gp", "3g2", "mj2"} and vcodec == "h264" and acodec == "aac" and pix_fmt in {"", "yuv420p"}:
            readiness, action = "ready", "skip"
        elif vcodec == "h264" and acodec == "aac":
            readiness, action = "optimize", "remux"
        elif vcodec == "h264":
            readiness, action = "optimize", "audio"
        else:
            readiness, action = "optimize", "transcode"
        return container, vcodec, acodec, readiness, action, None
    except Exception as exc:
        return None, None, None, "failed", None, str(exc)[:500]


def media_root_path():
    try:
        return Path(MEDIA_ROOT).resolve()
    except Exception:
        return Path(MEDIA_ROOT)


def valid_scan_folder(value):
    try:
        path = Path(value).resolve()
        root = media_root_path()
        path.relative_to(root)
        return path if path.exists() and path.is_dir() else None
    except Exception:
        return None


def browse_media_folders(max_depth=4, limit=500):
    root = media_root_path()
    if not root.exists() or not root.is_dir():
        return []
    result = [str(root)]
    try:
        for current, dirs, _files in os.walk(root):
            cur = Path(current)
            try:
                depth = len(cur.relative_to(root).parts)
            except ValueError:
                continue
            if depth >= max_depth:
                dirs[:] = []
                continue
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for name in sorted(dirs):
                result.append(str(cur / name))
                if len(result) >= limit:
                    return result
    except OSError:
        pass
    return result


def scan_roots():
    roots = []
    with db() as conn:
        manual = conn.execute("SELECT id,path,label FROM scan_folders WHERE enabled=1 ORDER BY id").fetchall()
        if manual:
            for row in manual:
                p = valid_scan_folder(row["path"])
                if p:
                    roots.append((None, p))
            return roots
        instances = conn.execute("SELECT * FROM instances WHERE enabled=1 ORDER BY id").fetchall()
    for inst in instances:
        for root in (inst["root_folders"] or "").splitlines():
            p = Path(root)
            if root and p.exists() and p.is_dir():
                roots.append((inst, p))
    if not roots:
        p = Path(MEDIA_ROOT)
        if p.exists() and p.is_dir():
            roots = [(None, p)]
    return roots


def scan_library(limit=500, only_path=None):
    if only_path:
        p = valid_scan_folder(only_path)
        roots = [(None, p)] if p else []
    else:
        roots = scan_roots()
    scanned = 0
    for inst, root in roots:
        if not root or not root.exists():
            continue
        for path in root.rglob("*"):
            if scanned >= limit:
                return scanned
            if not path.is_file() or path.suffix.lower() not in VIDEO_EXTENSIONS:
                continue
            container, video, audio, readiness, action, error = probe_media(str(path))
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            with db() as conn:
                conn.execute(
                    """INSERT INTO library_items(media_path,source_kind,source_instance_id,size_bytes,container,video_codec,audio_codec,readiness,action,error,last_scanned)
                    VALUES(?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
                    ON CONFLICT(media_path) DO UPDATE SET source_kind=excluded.source_kind,source_instance_id=excluded.source_instance_id,size_bytes=excluded.size_bytes,container=excluded.container,video_codec=excluded.video_codec,audio_codec=excluded.audio_codec,readiness=excluded.readiness,action=excluded.action,error=excluded.error,last_scanned=CURRENT_TIMESTAMP""",
                    (str(path), inst["kind"] if inst else "local", inst["id"] if inst else None, size, container, video, audio, readiness, action, error),
                )
            scanned += 1
    return scanned


async def test_arr(kind, url, api_key):
    base = url.rstrip("/")
    headers = {"X-Api-Key": api_key}
    async with httpx.AsyncClient(timeout=8.0) as client:
        status = await client.get(f"{base}/api/v3/system/status", headers=headers)
        status.raise_for_status()
        root = await client.get(f"{base}/api/v3/rootfolder", headers=headers)
        root.raise_for_status()
    payload = status.json()
    return {"version": payload.get("version", "unknown"), "roots": [r.get("path") for r in root.json() if r.get("path")]}


def connection_test_sync(instance):
    try:
        r = httpx.get(f"{instance['url'].rstrip('/')}/api/v3/system/status", headers={"X-Api-Key": instance["api_key"]}, timeout=5)
        r.raise_for_status()
        return True, r.json().get("version", instance["version"] or "unknown"), "Connected"
    except Exception as exc:
        return False, instance["version"] or "unknown", str(exc)


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    if user_count() == 0: return RedirectResponse("/setup/admin", status_code=303)
    if not require_login(request): return RedirectResponse("/login", status_code=303)
    if instance_count() == 0: return RedirectResponse("/setup/instances", status_code=303)
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/setup/admin", response_class=HTMLResponse)
def setup_admin(request: Request):
    if user_count() > 0: return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "setup_admin.html", {"title": "Create Admin"})


@app.post("/setup/admin")
def create_admin(request: Request, username: str = Form(...), email: str = Form(""), password: str = Form(...), confirm_password: str = Form(...)):
    if user_count() > 0: return RedirectResponse("/login", status_code=303)
    if len(password) < 8 or password != confirm_password:
        return templates.TemplateResponse(request, "setup_admin.html", {"title": "Create Admin", "error": "Passwords must match and be at least 8 characters."}, status_code=400)
    try:
        with db() as conn:
            cur = conn.execute("INSERT INTO users(username,email,password_hash) VALUES(?,?,?)", (username.strip(), email.strip(), pwd_context.hash(password)))
            uid = cur.lastrowid
        request.session["user_id"] = uid
        request.session["username"] = username.strip()
        return RedirectResponse("/setup/instances", status_code=303)
    except sqlite3.IntegrityError:
        return templates.TemplateResponse(request, "setup_admin.html", {"title": "Create Admin", "error": "That username already exists."}, status_code=400)


@app.get("/setup/instances", response_class=HTMLResponse)
def setup_instances(request: Request):
    if not require_login(request): return RedirectResponse("/login", status_code=303)
    with db() as conn: instances = conn.execute("SELECT * FROM instances ORDER BY id").fetchall()
    return templates.TemplateResponse(request, "setup_instances.html", {"title": "Connect Radarr / Sonarr", "instances": instances})


@app.post("/setup/instances")
async def add_instance(request: Request, kind: str = Form(...), name: str = Form(...), url: str = Form(...), api_key: str = Form(...)):
    if not require_login(request): return RedirectResponse("/login", status_code=303)
    if kind not in {"radarr", "sonarr"}: return JSONResponse({"ok": False, "error": "Invalid instance type"}, status_code=400)
    try:
        result = await test_arr(kind, url, api_key)
    except Exception as exc:
        with db() as conn: instances = conn.execute("SELECT * FROM instances ORDER BY id").fetchall()
        return templates.TemplateResponse(request, "setup_instances.html", {"title": "Connect Radarr / Sonarr", "instances": instances, "error": f"Connection failed: {exc}"}, status_code=400)
    with db() as conn:
        conn.execute("INSERT INTO instances(kind,name,url,api_key,version,root_folders) VALUES(?,?,?,?,?,?)", (kind, name.strip(), url.rstrip('/'), api_key.strip(), result["version"], "\n".join(result["roots"])))
    return RedirectResponse("/setup/instances", status_code=303)


@app.post("/setup/instances/continue")
def continue_setup(request: Request):
    if not require_login(request): return RedirectResponse("/login", status_code=303)
    return RedirectResponse("/setup/paths" if instance_count() else "/setup/instances", status_code=303)


@app.get("/setup/paths", response_class=HTMLResponse)
def setup_paths(request: Request):
    if not require_login(request): return RedirectResponse("/login", status_code=303)
    checks = []
    with db() as conn: instances = conn.execute("SELECT * FROM instances WHERE enabled=1 ORDER BY id").fetchall()
    for inst in instances:
        for path in (inst["root_folders"] or "").splitlines():
            p = Path(path)
            checks.append({"instance": inst["name"], "kind": inst["kind"], "path": path, "exists": p.exists(), "readable": os.access(path, os.R_OK) if p.exists() else False, "writable": os.access(path, os.W_OK) if p.exists() else False})
    return templates.TemplateResponse(request, "setup_paths.html", {"title": "Verify Paths", "checks": checks, "media_root": MEDIA_ROOT})


@app.get("/setup/hardware", response_class=HTMLResponse)
def setup_hardware(request: Request):
    if not require_login(request): return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "setup_hardware.html", {"title": "Hardware", "gpus": detect_gpus(), "cpu_count": psutil.cpu_count(logical=True)})


@app.post("/setup/hardware")
async def save_hardware(request: Request):
    if not require_login(request): return RedirectResponse("/login", status_code=303)
    form = await request.form()
    save_settings({"hardware_mode": form.get("mode", "auto"), "selected_gpus": ",".join(form.getlist("gpu")), "concurrency": form.get("concurrency", "1")})
    return RedirectResponse("/setup/profile", status_code=303)


@app.get("/setup/profile", response_class=HTMLResponse)
def setup_profile(request: Request):
    if not require_login(request): return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "setup_profile.html", {"title": "IPTV Profile"})


@app.post("/setup/profile")
async def save_profile(request: Request):
    if not require_login(request): return RedirectResponse("/login", status_code=303)
    form = await request.form()
    save_settings({"profile": form.get("profile", "iptv-direct"), "auto_optimize": "1" if form.get("auto_optimize") else "0", "periodic_scan_minutes": form.get("periodic_scan_minutes", "60")})
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if user_count() == 0: return RedirectResponse("/setup/admin", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"title": "Sign In"})


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    with db() as conn: row = conn.execute("SELECT * FROM users WHERE username=?", (username.strip(),)).fetchone()
    if not row or not pwd_context.verify(password, row["password_hash"]):
        return templates.TemplateResponse(request, "login.html", {"title": "Sign In", "error": "Invalid username or password."}, status_code=401)
    request.session["user_id"] = row["id"]
    request.session["username"] = row["username"]
    return RedirectResponse("/dashboard" if instance_count() else "/setup/instances", status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    guard = page_guard(request)
    if guard: return guard
    with db() as conn:
        stats = conn.execute("SELECT COUNT(*) total, SUM(status='completed') done, SUM(status='failed') failed, SUM(status='queued') queued, COALESCE(SUM(input_bytes-output_bytes),0) saved FROM jobs").fetchone()
        lib = conn.execute("SELECT COUNT(*) total, SUM(readiness='ready') ready, SUM(readiness='optimize') optimize, SUM(readiness='failed') failed FROM library_items").fetchone()
        instances = conn.execute("SELECT * FROM instances WHERE enabled=1 ORDER BY kind,name").fetchall()
        jobs = conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 8").fetchall()
    return templates.TemplateResponse(request, "dashboard.html", {"title": "Dashboard", "stats": stats, "lib": lib, "instances": instances, "jobs": jobs, "gpus": detect_gpus(), "cpu": psutil.cpu_percent(interval=None), "memory": psutil.virtual_memory().percent})


@app.get("/queue", response_class=HTMLResponse)
def queue_page(request: Request):
    guard = page_guard(request)
    if guard: return guard
    with db() as conn: jobs = conn.execute("SELECT * FROM jobs WHERE status IN ('queued','running','paused') ORDER BY id DESC").fetchall()
    return templates.TemplateResponse(request, "queue.html", {"title": "Queue", "jobs": jobs})


@app.post("/queue/clear")
def queue_clear(request: Request):
    if not require_login(request): return RedirectResponse("/login", status_code=303)
    with db() as conn: conn.execute("DELETE FROM jobs WHERE status IN ('queued','paused','cancelled')")
    return RedirectResponse("/queue", status_code=303)


@app.post("/queue/{job_id}/cancel")
def queue_cancel(job_id: int, request: Request):
    if not require_login(request): return RedirectResponse("/login", status_code=303)
    with db() as conn: conn.execute("UPDATE jobs SET status='cancelled',finished_at=CURRENT_TIMESTAMP WHERE id=? AND status IN ('queued','running','paused')", (job_id,))
    return RedirectResponse("/queue", status_code=303)


@app.get("/library", response_class=HTMLResponse)
def library_page(request: Request, status: str = "all", error: str = ""):
    guard = page_guard(request)
    if guard: return guard
    with db() as conn:
        if status in {"ready", "optimize", "failed"}:
            items = conn.execute("SELECT * FROM library_items WHERE readiness=? ORDER BY id DESC LIMIT 1000", (status,)).fetchall()
        else:
            items = conn.execute("SELECT * FROM library_items ORDER BY id DESC LIMIT 1000").fetchall()
        counts = conn.execute("SELECT COUNT(*) total, SUM(readiness='ready') ready, SUM(readiness='optimize') optimize, SUM(readiness='failed') failed FROM library_items").fetchone()
        folders = conn.execute("SELECT * FROM scan_folders ORDER BY id").fetchall()
    return templates.TemplateResponse(request, "library.html", {"title": "Library", "items": items, "counts": counts, "filter": status, "folders": folders, "folder_choices": browse_media_folders(), "media_root": MEDIA_ROOT, "error": error})


@app.post("/library/folders")
async def library_folder_add(request: Request):
    if not require_login(request): return RedirectResponse("/login", status_code=303)
    form = await request.form()
    selected = (form.get("folder") or form.get("path") or "").strip()
    label = (form.get("label") or "").strip()
    path = valid_scan_folder(selected)
    if not path:
        return RedirectResponse("/library?error=Folder+must+exist+inside+the+mounted+media+root", status_code=303)
    try:
        with db() as conn:
            conn.execute("INSERT INTO scan_folders(path,label) VALUES(?,?)", (str(path), label or path.name or str(path)))
    except sqlite3.IntegrityError:
        pass
    return RedirectResponse("/library", status_code=303)


@app.post("/library/folders/{folder_id}/delete")
def library_folder_delete(folder_id: int, request: Request):
    if not require_login(request): return RedirectResponse("/login", status_code=303)
    with db() as conn: conn.execute("DELETE FROM scan_folders WHERE id=?", (folder_id,))
    return RedirectResponse("/library", status_code=303)


@app.post("/library/folders/{folder_id}/scan")
def library_folder_scan(folder_id: int, request: Request):
    if not require_login(request): return RedirectResponse("/login", status_code=303)
    with db() as conn: folder = conn.execute("SELECT path FROM scan_folders WHERE id=?", (folder_id,)).fetchone()
    if folder:
        scan_library(int(settings_dict().get("scan_limit", "500")), folder["path"])
    return RedirectResponse("/library", status_code=303)


@app.post("/library/scan")
def library_scan(request: Request):
    if not require_login(request): return RedirectResponse("/login", status_code=303)
    scan_library(int(settings_dict().get("scan_limit", "500")))
    return RedirectResponse("/library", status_code=303)


@app.post("/library/{item_id}/queue")
def library_queue(item_id: int, request: Request):
    if not require_login(request): return RedirectResponse("/login", status_code=303)
    with db() as conn:
        item = conn.execute("SELECT * FROM library_items WHERE id=?", (item_id,)).fetchone()
        if item and item["readiness"] != "ready":
            conn.execute("INSERT INTO jobs(source_kind,source_instance_id,media_path,status,action,input_bytes) VALUES(?,?,?,'queued',?,?)", (item["source_kind"], item["source_instance_id"], item["media_path"], item["action"], item["size_bytes"]))
    return RedirectResponse("/library", status_code=303)


@app.get("/history", response_class=HTMLResponse)
def history_page(request: Request):
    guard = page_guard(request)
    if guard: return guard
    with db() as conn: jobs = conn.execute("SELECT * FROM jobs WHERE status NOT IN ('queued','running','paused') ORDER BY id DESC LIMIT 1000").fetchall()
    return templates.TemplateResponse(request, "history.html", {"title": "History", "jobs": jobs})


@app.get("/connections", response_class=HTMLResponse)
def connections_page(request: Request):
    guard = page_guard(request)
    if guard: return guard
    rows = []
    with db() as conn: instances = conn.execute("SELECT * FROM instances ORDER BY kind,name").fetchall()
    for inst in instances:
        ok, version, message = connection_test_sync(inst)
        rows.append({"instance": inst, "ok": ok, "version": version, "message": message})
    return templates.TemplateResponse(request, "connections.html", {"title": "Connections", "rows": rows})


@app.post("/connections/add")
async def connections_add(request: Request, kind: str = Form(...), name: str = Form(...), url: str = Form(...), api_key: str = Form(...)):
    if not require_login(request): return RedirectResponse("/login", status_code=303)
    try:
        result = await test_arr(kind, url, api_key)
    except Exception as exc:
        return RedirectResponse(f"/connections?error={str(exc)[:80]}", status_code=303)
    with db() as conn:
        conn.execute("INSERT INTO instances(kind,name,url,api_key,version,root_folders) VALUES(?,?,?,?,?,?)", (kind, name.strip(), url.rstrip('/'), api_key.strip(), result["version"], "\n".join(result["roots"])))
    return RedirectResponse("/connections", status_code=303)


@app.post("/connections/{instance_id}/delete")
def connections_delete(instance_id: int, request: Request):
    if not require_login(request): return RedirectResponse("/login", status_code=303)
    with db() as conn: conn.execute("DELETE FROM instances WHERE id=?", (instance_id,))
    return RedirectResponse("/connections", status_code=303)


@app.get("/diagnostics", response_class=HTMLResponse)
def diagnostics_page(request: Request):
    guard = page_guard(request)
    if guard: return guard
    encoders = ffmpeg_encoders()
    checks = [
        {"name": "FFmpeg", "ok": bool(encoders), "detail": ", ".join(encoders) or "Not available"},
        {"name": "Media root", "ok": Path(MEDIA_ROOT).exists(), "detail": MEDIA_ROOT},
        {"name": "Config writable", "ok": os.access(CONFIG_DIR, os.W_OK), "detail": str(CONFIG_DIR)},
        {"name": "GPU access", "ok": bool(detect_gpus()), "detail": ", ".join(g["name"] for g in detect_gpus()) or "No GPU exposed; CPU mode available"},
    ]
    with db() as conn:
        instances = conn.execute("SELECT * FROM instances WHERE enabled=1").fetchall()
        manual = conn.execute("SELECT * FROM scan_folders WHERE enabled=1 ORDER BY id").fetchall()
    path_checks = []
    for folder in manual:
        p = Path(folder["path"])
        path_checks.append({"instance": "Manual", "path": folder["path"], "exists": p.exists(), "readable": os.access(p, os.R_OK) if p.exists() else False, "writable": os.access(p, os.W_OK) if p.exists() else False})
    for inst in instances:
        for root in (inst["root_folders"] or "").splitlines():
            p = Path(root)
            path_checks.append({"instance": inst["name"], "path": root, "exists": p.exists(), "readable": os.access(root, os.R_OK) if p.exists() else False, "writable": os.access(root, os.W_OK) if p.exists() else False})
    return templates.TemplateResponse(request, "diagnostics.html", {"title": "Diagnostics", "checks": checks, "path_checks": path_checks})


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    guard = page_guard(request)
    if guard: return guard
    return templates.TemplateResponse(request, "settings.html", {"title": "Settings", "settings": settings_dict(), "gpus": detect_gpus(), "encoders": ffmpeg_encoders(), "media_root": MEDIA_ROOT})


@app.post("/settings")
async def settings_save(request: Request):
    if not require_login(request): return RedirectResponse("/login", status_code=303)
    form = await request.form()
    save_settings({
        "hardware_mode": form.get("hardware_mode", "auto"),
        "selected_gpus": ",".join(form.getlist("gpu")),
        "concurrency": form.get("concurrency", "1"),
        "profile": form.get("profile", "iptv-direct"),
        "auto_optimize": "1" if form.get("auto_optimize") else "0",
        "periodic_scan_minutes": form.get("periodic_scan_minutes", "60"),
        "scan_limit": form.get("scan_limit", "500"),
    })
    return RedirectResponse("/settings?saved=1", status_code=303)


@app.get("/api/v1/health")
def api_health():
    return {"status": "ok", "app": APP_NAME, "version": APP_VERSION, "setup_complete": setup_complete(), "port": PORT}


@app.get("/api/v1/system")
def api_system():
    return {"cpu_percent": psutil.cpu_percent(interval=None), "memory_percent": psutil.virtual_memory().percent, "gpus": detect_gpus(), "encoders": ffmpeg_encoders(), "media_root": MEDIA_ROOT}


@app.get("/api/v1/connections")
def api_connections():
    with db() as conn: rows = conn.execute("SELECT id,kind,name,url,enabled,version,root_folders FROM instances ORDER BY id").fetchall()
    return {"connections": [dict(r) for r in rows]}


@app.get("/api/v1/jobs")
def api_jobs():
    with db() as conn: rows = conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 500").fetchall()
    return {"jobs": [dict(r) for r in rows]}


@app.get("/api/v1/readiness")
def api_readiness():
    with db() as conn: r = conn.execute("SELECT COUNT(*) total, SUM(readiness='ready') ready, SUM(readiness='optimize') optimize, SUM(readiness='failed') failed FROM library_items").fetchone()
    return dict(r)


@app.get("/api/v1/issues")
def api_issues():
    issues = []
    if user_count() == 0: issues.append({"code": "ADMIN_NOT_CREATED", "severity": "critical", "message": "Initial admin user has not been created."})
    if instance_count() == 0: issues.append({"code": "NO_ARR_INSTANCE", "severity": "critical", "message": "No Radarr or Sonarr instance is configured."})
    if not ffmpeg_encoders(): issues.append({"code": "FFMPEG_MISSING", "severity": "critical", "message": "FFmpeg is not available inside the ArrStream container."})
    if not Path(MEDIA_ROOT).exists(): issues.append({"code": "PATH_NOT_FOUND", "severity": "critical", "message": f"Media root {MEDIA_ROOT} is not available."})
    return {"status": "ok" if not issues else "degraded", "issues": issues}
