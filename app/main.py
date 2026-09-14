import os
import sqlite3
import subprocess
from pathlib import Path
from typing import Optional

import httpx
import psutil
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from passlib.context import CryptContext
from starlette.middleware.sessions import SessionMiddleware

APP_NAME = "ArrStream"
PORT = int(os.getenv("PORT", "4321"))
CONFIG_DIR = Path(os.getenv("CONFIG_DIR", "/config"))
DB_PATH = CONFIG_DIR / "arrstream.db"
MEDIA_ROOT = os.getenv("MEDIA_ROOT", "/data")
SECRET_KEY = os.getenv("SECRET_KEY", "change-me-in-production")

CONFIG_DIR.mkdir(parents=True, exist_ok=True)
app = FastAPI(title=APP_NAME, version="0.1.0")
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
            """
        )


@app.on_event("startup")
def startup():
    init_db()


def user_count() -> int:
    with db() as conn:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def instance_count() -> int:
    with db() as conn:
        return conn.execute("SELECT COUNT(*) FROM instances WHERE enabled=1").fetchone()[0]


def setup_complete() -> bool:
    return user_count() > 0 and instance_count() > 0


def require_login(request: Request):
    return request.session.get("user_id")


def redirect_for_state(request: Request):
    if user_count() == 0:
        return RedirectResponse("/setup/admin", status_code=303)
    if not require_login(request):
        return RedirectResponse("/login", status_code=303)
    if instance_count() == 0:
        return RedirectResponse("/setup/instances", status_code=303)
    return None


def detect_gpus():
    gpus = []
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=index,name,memory.total", "--format=csv,noheader,nounits"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
        )
        for line in out.strip().splitlines():
            idx, name, memory = [x.strip() for x in line.split(",", 2)]
            gpus.append({"id": f"nvidia-{idx}", "vendor": "NVIDIA", "index": idx, "name": name, "memory_mb": memory})
    except Exception:
        pass
    dri = Path("/dev/dri")
    if dri.exists():
        cards = sorted(dri.glob("renderD*"))
        for card in cards:
            gpus.append({"id": str(card), "vendor": "Intel/AMD", "index": card.name, "name": card.name, "memory_mb": None})
    return gpus


async def test_arr(kind: str, url: str, api_key: str):
    base = url.rstrip("/")
    headers = {"X-Api-Key": api_key}
    async with httpx.AsyncClient(timeout=8.0) as client:
        status = await client.get(f"{base}/api/v3/system/status", headers=headers)
        status.raise_for_status()
        root = await client.get(f"{base}/api/v3/rootfolder", headers=headers)
        root.raise_for_status()
    status_json = status.json()
    roots = root.json()
    return {
        "version": status_json.get("version", "unknown"),
        "roots": [r.get("path") for r in roots if r.get("path")],
    }


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    if user_count() == 0:
        return RedirectResponse("/setup/admin", status_code=303)
    if not require_login(request):
        return RedirectResponse("/login", status_code=303)
    if instance_count() == 0:
        return RedirectResponse("/setup/instances", status_code=303)
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/setup/admin", response_class=HTMLResponse)
def setup_admin(request: Request):
    if user_count() > 0:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "setup_admin.html", {"title": "Create Admin"})


@app.post("/setup/admin")
def create_admin(request: Request, username: str = Form(...), email: str = Form(""), password: str = Form(...), confirm_password: str = Form(...)):
    if user_count() > 0:
        return RedirectResponse("/login", status_code=303)
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
    if not require_login(request):
        return RedirectResponse("/login", status_code=303)
    with db() as conn:
        instances = conn.execute("SELECT * FROM instances ORDER BY id").fetchall()
    return templates.TemplateResponse(request, "setup_instances.html", {"title": "Connect Radarr / Sonarr", "instances": instances})


@app.post("/setup/instances")
async def add_instance(request: Request, kind: str = Form(...), name: str = Form(...), url: str = Form(...), api_key: str = Form(...)):
    if not require_login(request):
        return RedirectResponse("/login", status_code=303)
    if kind not in {"radarr", "sonarr"}:
        return JSONResponse({"ok": False, "error": "Invalid instance type"}, status_code=400)
    try:
        result = await test_arr(kind, url, api_key)
    except Exception as exc:
        with db() as conn:
            instances = conn.execute("SELECT * FROM instances ORDER BY id").fetchall()
        return templates.TemplateResponse(request, "setup_instances.html", {"title": "Connect Radarr / Sonarr", "instances": instances, "error": f"Connection failed: {exc}"}, status_code=400)
    roots = "\n".join(result["roots"])
    with db() as conn:
        conn.execute("INSERT INTO instances(kind,name,url,api_key,version,root_folders) VALUES(?,?,?,?,?,?)", (kind, name.strip(), url.rstrip('/'), api_key.strip(), result["version"], roots))
    return RedirectResponse("/setup/instances", status_code=303)


@app.post("/setup/instances/continue")
def continue_setup(request: Request):
    if not require_login(request):
        return RedirectResponse("/login", status_code=303)
    if instance_count() == 0:
        return RedirectResponse("/setup/instances", status_code=303)
    return RedirectResponse("/setup/paths", status_code=303)


@app.get("/setup/paths", response_class=HTMLResponse)
def setup_paths(request: Request):
    if not require_login(request):
        return RedirectResponse("/login", status_code=303)
    checks = []
    with db() as conn:
        instances = conn.execute("SELECT * FROM instances WHERE enabled=1 ORDER BY id").fetchall()
    for inst in instances:
        for path in (inst["root_folders"] or "").splitlines():
            p = Path(path)
            checks.append({"instance": inst["name"], "kind": inst["kind"], "path": path, "exists": p.exists(), "readable": os.access(path, os.R_OK) if p.exists() else False, "writable": os.access(path, os.W_OK) if p.exists() else False})
    return templates.TemplateResponse(request, "setup_paths.html", {"title": "Verify Paths", "checks": checks, "media_root": MEDIA_ROOT})


@app.get("/setup/hardware", response_class=HTMLResponse)
def setup_hardware(request: Request):
    if not require_login(request):
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "setup_hardware.html", {"title": "Hardware", "gpus": detect_gpus(), "cpu_count": psutil.cpu_count(logical=True)})


@app.post("/setup/hardware")
async def save_hardware(request: Request):
    if not require_login(request):
        return RedirectResponse("/login", status_code=303)
    form = await request.form()
    selected = form.getlist("gpu")
    mode = form.get("mode", "auto")
    concurrency = form.get("concurrency", "1")
    with db() as conn:
        for key, value in {"hardware_mode": mode, "selected_gpus": ",".join(selected), "concurrency": concurrency}.items():
            conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    return RedirectResponse("/setup/profile", status_code=303)


@app.get("/setup/profile", response_class=HTMLResponse)
def setup_profile(request: Request):
    if not require_login(request):
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "setup_profile.html", {"title": "IPTV Profile"})


@app.post("/setup/profile")
async def save_profile(request: Request):
    if not require_login(request):
        return RedirectResponse("/login", status_code=303)
    form = await request.form()
    values = {
        "profile": form.get("profile", "iptv-direct"),
        "auto_optimize": "1" if form.get("auto_optimize") else "0",
        "periodic_scan_minutes": form.get("periodic_scan_minutes", "60"),
    }
    with db() as conn:
        for key, value in values.items():
            conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if user_count() == 0:
        return RedirectResponse("/setup/admin", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"title": "Sign In"})


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE username=?", (username.strip(),)).fetchone()
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
    state_redirect = redirect_for_state(request)
    if state_redirect:
        return state_redirect
    with db() as conn:
        stats = conn.execute("SELECT COUNT(*) total, SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) done, SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) failed, SUM(CASE WHEN status='queued' THEN 1 ELSE 0 END) queued, COALESCE(SUM(input_bytes-output_bytes),0) saved FROM jobs").fetchone()
        instances = conn.execute("SELECT * FROM instances WHERE enabled=1 ORDER BY kind,name").fetchall()
        jobs = conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 8").fetchall()
    return templates.TemplateResponse(request, "dashboard.html", {"title": "Dashboard", "stats": stats, "instances": instances, "jobs": jobs, "gpus": detect_gpus(), "cpu": psutil.cpu_percent(interval=None), "memory": psutil.virtual_memory().percent})


@app.get("/api/v1/health")
def api_health():
    return {"status": "ok", "app": APP_NAME, "version": "0.1.0", "setup_complete": setup_complete(), "port": PORT}


@app.get("/api/v1/system")
def api_system():
    return {"cpu_percent": psutil.cpu_percent(interval=None), "memory_percent": psutil.virtual_memory().percent, "gpus": detect_gpus(), "media_root": MEDIA_ROOT}


@app.get("/api/v1/issues")
def api_issues():
    issues = []
    if user_count() == 0:
        issues.append({"code": "ADMIN_NOT_CREATED", "severity": "critical", "message": "Initial admin user has not been created."})
    if instance_count() == 0:
        issues.append({"code": "NO_ARR_INSTANCE", "severity": "critical", "message": "No Radarr or Sonarr instance is configured."})
    try:
        subprocess.check_output(["ffmpeg", "-version"], stderr=subprocess.STDOUT, timeout=3)
    except Exception:
        issues.append({"code": "FFMPEG_MISSING", "severity": "critical", "message": "FFmpeg is not available inside the ArrStream container."})
    return {"status": "ok" if not issues else "degraded", "issues": issues}
