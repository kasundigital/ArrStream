import json
import os
import signal
import sqlite3
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

import httpx
import psutil
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from passlib.context import CryptContext
from starlette.middleware.sessions import SessionMiddleware

APP_NAME = "ArrStream"
APP_VERSION = "0.3.0"
PORT = int(os.getenv("PORT", "4321"))
CONFIG_DIR = Path(os.getenv("CONFIG_DIR", "/config"))
DB_PATH = CONFIG_DIR / "arrstream.db"
MEDIA_ROOT = Path(os.getenv("MEDIA_ROOT", "/data"))
SECRET_KEY = os.getenv("SECRET_KEY", "change-me-in-production")
VIDEO_EXTENSIONS = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".ts", ".m2ts", ".webm"}

CONFIG_DIR.mkdir(parents=True, exist_ok=True)
app = FastAPI(title=APP_NAME, version=APP_VERSION)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, same_site="lax", https_only=False)
templates = Jinja2Templates(directory="app/templates")
pwd_context = CryptContext(schemes=["argon2", "bcrypt"], deprecated="auto")

ACTIVE_PROCESSES = {}
PROCESS_LOCK = threading.Lock()
WORKERS_STARTED = False

BUILTIN_PROFILES = [
    ("Direct Stream Universal", "h264", "aac", "mp4", 0, 20, "medium", "192k", "copy", 1),
    ("Balanced IPTV", "h264", "aac", "mp4", 1080, 22, "medium", "160k", "drop", 1),
    ("Low Bandwidth 720p", "h264", "aac", "mp4", 720, 26, "medium", "128k", "drop", 1),
    ("High Quality 1080p", "h264", "aac", "mp4", 1080, 18, "slow", "192k", "copy", 1),
    ("4K Direct", "hevc", "aac", "mp4", 2160, 20, "medium", "192k", "copy", 1),
    ("Remux Only", "copy", "copy", "mp4", 0, 0, "medium", "0", "copy", 1),
    ("Audio Fix Only", "copy", "aac", "mp4", 0, 0, "medium", "192k", "copy", 1),
]


def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_column(conn, table, column, definition):
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db():
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT,username TEXT UNIQUE NOT NULL,email TEXT,password_hash TEXT NOT NULL,is_admin INTEGER NOT NULL DEFAULT 1,created_at DATETIME DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS instances (id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT NOT NULL CHECK(kind IN ('radarr','sonarr')),name TEXT NOT NULL,url TEXT NOT NULL,api_key TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1,version TEXT,root_folders TEXT,created_at DATETIME DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE IF NOT EXISTS profiles (id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT UNIQUE NOT NULL,video_codec TEXT NOT NULL DEFAULT 'h264',audio_codec TEXT NOT NULL DEFAULT 'aac',container TEXT NOT NULL DEFAULT 'mp4',max_height INTEGER NOT NULL DEFAULT 1080,quality INTEGER NOT NULL DEFAULT 22,preset TEXT NOT NULL DEFAULT 'medium',audio_bitrate TEXT NOT NULL DEFAULT '160k',subtitle_mode TEXT NOT NULL DEFAULT 'drop',builtin INTEGER NOT NULL DEFAULT 0,enabled INTEGER NOT NULL DEFAULT 1,created_at DATETIME DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS media_folders (id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,media_type TEXT NOT NULL DEFAULT 'mixed',host_path TEXT,container_path TEXT UNIQUE NOT NULL,profile_id INTEGER,source_kind TEXT,source_instance_id INTEGER,enabled INTEGER NOT NULL DEFAULT 1,processing_enabled INTEGER NOT NULL DEFAULT 1,auto_optimize INTEGER NOT NULL DEFAULT 0,created_at DATETIME DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS jobs (id INTEGER PRIMARY KEY AUTOINCREMENT,source_kind TEXT,source_instance_id INTEGER,media_path TEXT,status TEXT NOT NULL DEFAULT 'queued',action TEXT,progress REAL NOT NULL DEFAULT 0,input_bytes INTEGER DEFAULT 0,output_bytes INTEGER DEFAULT 0,error TEXT,created_at DATETIME DEFAULT CURRENT_TIMESTAMP,finished_at DATETIME);
        CREATE TABLE IF NOT EXISTS library_items (id INTEGER PRIMARY KEY AUTOINCREMENT,media_path TEXT UNIQUE NOT NULL,source_kind TEXT,source_instance_id INTEGER,size_bytes INTEGER DEFAULT 0,container TEXT,video_codec TEXT,audio_codec TEXT,readiness TEXT NOT NULL DEFAULT 'unknown',action TEXT,error TEXT,last_scanned DATETIME DEFAULT CURRENT_TIMESTAMP);
        """)
        for table, column, definition in [("jobs","folder_id","INTEGER"),("jobs","profile_id","INTEGER"),("jobs","started_at","DATETIME"),("jobs","eta_seconds","INTEGER DEFAULT 0"),("jobs","speed","TEXT"),("jobs","encoder","TEXT"),("jobs","temp_path","TEXT"),("jobs","duration_seconds","REAL DEFAULT 0"),("library_items","folder_id","INTEGER"),("library_items","profile_id","INTEGER"),("library_items","duration_seconds","REAL DEFAULT 0")]:
            ensure_column(conn, table, column, definition)
        for p in BUILTIN_PROFILES:
            conn.execute("""INSERT INTO profiles(name,video_codec,audio_codec,container,max_height,quality,preset,audio_bitrate,subtitle_mode,builtin) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(name) DO NOTHING""", p)
        defaults={"processing_state":"stopped","hardware_mode":"auto","concurrency":"1","scan_limit":"500","periodic_scan_minutes":"60","auto_optimize":"0","storage_mode":"docker","host_root":"","container_root":str(MEDIA_ROOT),"wizard_complete":"0","schedule_enabled":"0","process_window_start":"00:00","process_window_end":"23:59"}
        for k,v in defaults.items(): conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO NOTHING",(k,v))
        conn.execute("UPDATE jobs SET status='queued' WHERE status IN ('starting','running','paused')")
        conn.execute("UPDATE jobs SET status='cancelled',finished_at=CURRENT_TIMESTAMP WHERE status='cancelling'")


def get_setting(key, default=None):
    with db() as conn: row=conn.execute("SELECT value FROM settings WHERE key=?",(key,)).fetchone()
    return row["value"] if row else default


def settings_dict():
    with db() as conn: return {r["key"]:r["value"] for r in conn.execute("SELECT key,value FROM settings")}


def save_settings(values):
    with db() as conn:
        for key,value in values.items(): conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(key,str(value)))


def user_count():
    with db() as conn: return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def require_login(request): return request.session.get("user_id")


def page_guard(request,allow_wizard=False):
    if user_count()==0: return RedirectResponse("/setup/admin",status_code=303)
    if not require_login(request): return RedirectResponse("/login",status_code=303)
    if not allow_wizard and get_setting("wizard_complete","0")!="1": return RedirectResponse("/wizard",status_code=303)
    return None


def detect_gpus():
    gpus=[]
    try:
        out=subprocess.check_output(["nvidia-smi","--query-gpu=index,name,memory.total","--format=csv,noheader,nounits"],stderr=subprocess.DEVNULL,text=True,timeout=3)
        for line in out.strip().splitlines():
            idx,name,memory=[x.strip() for x in line.split(",",2)]; gpus.append({"id":f"nvidia-{idx}","vendor":"NVIDIA","index":idx,"name":name,"memory_mb":memory})
    except Exception: pass
    dri=Path("/dev/dri")
    if dri.exists():
        for card in sorted(dri.glob("renderD*")): gpus.append({"id":str(card),"vendor":"Intel/AMD","index":card.name,"name":card.name,"memory_mb":None})
    return gpus


def ffmpeg_encoders():
    try: out=subprocess.check_output(["ffmpeg","-hide_banner","-encoders"],stderr=subprocess.STDOUT,text=True,timeout=5)
    except Exception: return []
    wanted=["libx264","libx265","h264_nvenc","hevc_nvenc","h264_qsv","hevc_qsv","h264_vaapi","hevc_vaapi"]
    return [x for x in wanted if x in out]


def profile_row(profile_id=None):
    with db() as conn:
        if profile_id:
            row=conn.execute("SELECT * FROM profiles WHERE id=? AND enabled=1",(profile_id,)).fetchone()
            if row:return row
        return conn.execute("SELECT * FROM profiles WHERE name='Balanced IPTV'").fetchone()


def folder_choices(root=None,depth=2):
    root=Path(root or get_setting("container_root",str(MEDIA_ROOT))); result=[]
    if not root.exists() or not root.is_dir(): return result
    result.append(str(root)); base_parts=len(root.parts)
    try:
        for p in root.rglob("*"):
            if p.is_dir() and len(p.parts)-base_parts<=depth: result.append(str(p))
            if len(result)>=250: break
    except Exception: pass
    return sorted(set(result))


def probe_media(path):
    try:
        raw=subprocess.check_output(["ffprobe","-v","error","-show_entries","format=format_name,duration:stream=codec_type,codec_name,pix_fmt,height","-of","json",path],stderr=subprocess.STDOUT,text=True,timeout=30)
        data=json.loads(raw); streams=data.get("streams",[]); video=next((x for x in streams if x.get("codec_type")=="video"),{}); audio=next((x for x in streams if x.get("codec_type")=="audio"),{})
        return {"container":(data.get("format",{}).get("format_name") or "unknown").split(",")[0],"duration":float(data.get("format",{}).get("duration") or 0),"video_codec":video.get("codec_name") or "unknown","audio_codec":audio.get("codec_name") or "unknown","pix_fmt":video.get("pix_fmt") or "","height":int(video.get("height") or 0)},None
    except Exception as exc: return None,str(exc)[:500]


def classify_media(info,profile):
    if not info:return "failed",None
    pvideo=profile["video_codec"];paudio=profile["audio_codec"];pcontainer=profile["container"];max_height=int(profile["max_height"] or 0)
    video_ok=pvideo=="copy" or info["video_codec"]==pvideo; audio_ok=paudio=="copy" or info["audio_codec"]==paudio
    container_ok=pcontainer in info["container"] or (pcontainer=="mp4" and info["container"] in {"mov","mp4","m4a","3gp","3g2","mj2"})
    size_ok=max_height==0 or info["height"]==0 or info["height"]<=max_height; pix_ok=info["pix_fmt"] in {"","yuv420p"} or pvideo in {"hevc","copy"}
    if video_ok and audio_ok and container_ok and size_ok and pix_ok:return "ready","skip"
    if video_ok and audio_ok and size_ok:return "optimize","remux"
    if video_ok and size_ok:return "optimize","audio"
    return "optimize","transcode"


def scan_folder(folder_id,limit=500):
    with db() as conn: folder=conn.execute("SELECT * FROM media_folders WHERE id=? AND enabled=1",(folder_id,)).fetchone()
    if not folder:return 0
    root=Path(folder["container_path"])
    if not root.exists():return 0
    profile=profile_row(folder["profile_id"]);scanned=0
    for path in root.rglob("*"):
        if scanned>=limit:break
        if not path.is_file() or path.suffix.lower() not in VIDEO_EXTENSIONS:continue
        info,error=probe_media(str(path));readiness,action=classify_media(info,profile) if info else ("failed",None);size=path.stat().st_size if path.exists() else 0
        with db() as conn:
            conn.execute("""INSERT INTO library_items(media_path,source_kind,source_instance_id,size_bytes,container,video_codec,audio_codec,readiness,action,error,last_scanned,folder_id,profile_id,duration_seconds) VALUES(?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP,?,?,?) ON CONFLICT(media_path) DO UPDATE SET source_kind=excluded.source_kind,source_instance_id=excluded.source_instance_id,size_bytes=excluded.size_bytes,container=excluded.container,video_codec=excluded.video_codec,audio_codec=excluded.audio_codec,readiness=excluded.readiness,action=excluded.action,error=excluded.error,last_scanned=CURRENT_TIMESTAMP,folder_id=excluded.folder_id,profile_id=excluded.profile_id,duration_seconds=excluded.duration_seconds""",(str(path),folder["source_kind"] or "local",folder["source_instance_id"],size,info["container"] if info else None,info["video_codec"] if info else None,info["audio_codec"] if info else None,readiness,action,error,folder["id"],profile["id"],info["duration"] if info else 0))
            if readiness=="optimize" and (folder["auto_optimize"] or get_setting("auto_optimize","0")=="1"):
                exists=conn.execute("SELECT 1 FROM jobs WHERE media_path=? AND status IN ('queued','running','paused')",(str(path),)).fetchone()
                if not exists:conn.execute("INSERT INTO jobs(source_kind,source_instance_id,media_path,status,action,input_bytes,folder_id,profile_id,duration_seconds) VALUES(?,?,?,'queued',?,?,?,?,?)",(folder["source_kind"] or "local",folder["source_instance_id"],str(path),action,size,folder["id"],profile["id"],info["duration"] if info else 0))
        scanned+=1
    return scanned


def scan_library(limit=500):
    with db() as conn: folders=conn.execute("SELECT id FROM media_folders WHERE enabled=1 ORDER BY id").fetchall()
    total=0
    for f in folders:
        if total>=limit:break
        total+=scan_folder(f["id"],max(0,limit-total))
    return total


def choose_encoder(profile):
    target=profile["video_codec"]
    if target=="copy":return "copy"
    encoders=ffmpeg_encoders();mode=get_setting("hardware_mode","auto");selected=get_setting("selected_gpus","")
    if mode!="cpu":
        if ("nvidia-" in selected or mode=="auto") and f"{target}_nvenc" in encoders:return f"{target}_nvenc"
        if ("/dev/dri" in selected or mode=="auto") and f"{target}_qsv" in encoders:return f"{target}_qsv"
        if ("/dev/dri" in selected or mode=="auto") and f"{target}_vaapi" in encoders:return f"{target}_vaapi"
    return "libx265" if target=="hevc" else "libx264"


def scale_filter(max_height):
    return None if not max_height else f"scale=trunc(iw*min(1\\,{max_height}/ih)/2)*2:trunc(ih*min(1\\,{max_height}/ih)/2)*2"


def build_ffmpeg_cmd(job,profile,temp_path):
    src=job["media_path"];action=job["action"] or "transcode";cmd=["ffmpeg","-hide_banner","-y","-nostats","-progress","pipe:1","-i",src,"-map","0:v:0","-map","0:a?"];encoder="copy"
    if action in {"remux","audio"}:cmd += ["-c:v","copy"]
    else:
        encoder=choose_encoder(profile);max_height=int(profile["max_height"] or 0)
        if encoder.endswith("_vaapi"):
            device=next((g["id"] for g in detect_gpus() if str(g["id"]).startswith("/dev/dri/")),"/dev/dri/renderD128");cmd += ["-vaapi_device",device];vf="format=nv12,hwupload"
            if max_height:vf += f",scale_vaapi=w=-2:h={max_height}:force_original_aspect_ratio=decrease"
            cmd += ["-vf",vf,"-c:v",encoder,"-qp",str(profile["quality"] or 22)]
        else:
            sf=scale_filter(max_height)
            if sf:cmd += ["-vf",sf]
            cmd += ["-c:v",encoder];q=str(profile["quality"] or 22)
            if encoder.endswith("_nvenc"):cmd += ["-cq",q,"-preset","p5"]
            elif encoder.endswith("_qsv"):cmd += ["-global_quality",q]
            else:cmd += ["-crf",q,"-preset",profile["preset"] or "medium"]
    if action=="remux" and profile["audio_codec"]=="copy":cmd += ["-c:a","copy"]
    else:
        acodec=profile["audio_codec"]
        cmd += ["-c:a","copy"] if acodec=="copy" else ["-c:a",acodec,"-b:a",profile["audio_bitrate"] or "160k"]
    if profile["subtitle_mode"]=="copy":cmd += ["-map","0:s?","-c:s","mov_text"]
    cmd += ["-movflags","+faststart",temp_path];return cmd,encoder


def safe_finalize(job,temp_path):
    src=Path(job["media_path"])
    if not Path(temp_path).exists() or Path(temp_path).stat().st_size==0:raise RuntimeError("FFmpeg output file is missing or empty")
    info,error=probe_media(temp_path)
    if error or not info or info["video_codec"]=="unknown":raise RuntimeError(f"Output validation failed: {error or 'no video stream'}")
    target=src if src.suffix.lower()==".mp4" else src.with_suffix(".mp4")
    if target==src:os.replace(temp_path,src)
    else:
        if target.exists():raise RuntimeError(f"Refusing to overwrite existing target: {target}")
        os.replace(temp_path,target);src.unlink(missing_ok=True)
    return str(target),target.stat().st_size


def process_job(job_id):
    with db() as conn:job=conn.execute("SELECT * FROM jobs WHERE id=?",(job_id,)).fetchone()
    if not job:return
    profile=profile_row(job["profile_id"]);src=Path(job["media_path"])
    if not src.exists():
        with db() as conn:conn.execute("UPDATE jobs SET status='failed',error=?,finished_at=CURRENT_TIMESTAMP WHERE id=?",("Source file not found",job_id))
        return
    temp_path=str(src.with_name(f".{src.stem}.arrstream-{job_id}.tmp.mp4"));cmd,encoder=build_ffmpeg_cmd(job,profile,temp_path)
    with db() as conn:conn.execute("UPDATE jobs SET status='running',started_at=CURRENT_TIMESTAMP,temp_path=?,encoder=?,error=NULL WHERE id=?",(temp_path,encoder,job_id))
    proc=subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,bufsize=1)
    with PROCESS_LOCK:ACTIVE_PROCESSES[job_id]=proc
    duration=float(job["duration_seconds"] or 0);speed=""
    try:
        for line in proc.stdout or []:
            line=line.strip()
            if "=" not in line:continue
            key,value=line.split("=",1);progress=None
            if key in {"out_time_us","out_time_ms"} and duration>0:
                try:progress=min(99.5,max(0.0,(float(value)/1_000_000)/duration*100))
                except ValueError:pass
            elif key=="speed":speed=value
            if progress is not None:
                try:mult=float(speed.rstrip("x")) if speed.endswith("x") else 0;eta=int(max(0,duration*(100-progress)/100/mult)) if mult>0 else 0
                except Exception:eta=0
                with db() as conn:conn.execute("UPDATE jobs SET progress=?,speed=?,eta_seconds=? WHERE id=?",(progress,speed,eta,job_id))
        rc=proc.wait()
        with db() as conn:state=conn.execute("SELECT status FROM jobs WHERE id=?",(job_id,)).fetchone()["status"]
        if state=="cancelling":
            Path(temp_path).unlink(missing_ok=True)
            with db() as conn:conn.execute("UPDATE jobs SET status='cancelled',finished_at=CURRENT_TIMESTAMP,error=NULL WHERE id=?",(job_id,))
            return
        if rc!=0:raise RuntimeError(f"FFmpeg exited with code {rc}")
        new_path,out_size=safe_finalize(job,temp_path)
        with db() as conn:
            conn.execute("UPDATE jobs SET media_path=?,status='completed',progress=100,output_bytes=?,finished_at=CURRENT_TIMESTAMP,error=NULL WHERE id=?",(new_path,out_size,job_id))
            conn.execute("UPDATE library_items SET media_path=?,size_bytes=?,container='mp4',readiness='ready',action='skip',last_scanned=CURRENT_TIMESTAMP WHERE media_path=?",(new_path,out_size,job["media_path"]))
    except Exception as exc:
        Path(temp_path).unlink(missing_ok=True)
        with db() as conn:
            current=conn.execute("SELECT status FROM jobs WHERE id=?",(job_id,)).fetchone();status="cancelled" if current and current["status"]=="cancelling" else "failed";conn.execute("UPDATE jobs SET status=?,error=?,finished_at=CURRENT_TIMESTAMP WHERE id=?",(status,str(exc)[:1000],job_id))
    finally:
        with PROCESS_LOCK:ACTIVE_PROCESSES.pop(job_id,None);no_active=not ACTIVE_PROCESSES
        if no_active and get_setting("processing_state","stopped")=="stopping":save_settings({"processing_state":"stopped"})


def within_schedule():
    if get_setting("schedule_enabled","0")!="1":return True
    now=datetime.now().strftime("%H:%M");start=get_setting("process_window_start","00:00");end=get_setting("process_window_end","23:59")
    return start<=now<=end if start<=end else now>=start or now<=end


def claim_job():
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE");row=conn.execute("""SELECT j.id FROM jobs j LEFT JOIN media_folders f ON f.id=j.folder_id WHERE j.status='queued' AND COALESCE(f.processing_enabled,1)=1 ORDER BY j.id LIMIT 1""").fetchone()
        if not row:conn.commit();return None
        conn.execute("UPDATE jobs SET status='starting' WHERE id=? AND status='queued'",(row["id"],));conn.commit();return row["id"]


def worker_loop(index):
    while True:
        try:
            concurrency=max(1,min(16,int(get_setting("concurrency","1"))));state=get_setting("processing_state","stopped")
            if index>=concurrency or state!="running" or not within_schedule():time.sleep(1);continue
            job_id=claim_job()
            if not job_id:time.sleep(1);continue
            process_job(job_id)
        except Exception:time.sleep(2)


def scanner_loop():
    last_scan=0.0
    while True:
        try:
            interval=max(5,int(get_setting("periodic_scan_minutes","60")))*60
            if get_setting("wizard_complete","0")=="1" and time.time()-last_scan>=interval:
                scan_library(int(get_setting("scan_limit","500")));last_scan=time.time()
        except Exception:pass
        time.sleep(30)


def start_workers():
    global WORKERS_STARTED
    if WORKERS_STARTED:return
    WORKERS_STARTED=True
    for i in range(16):threading.Thread(target=worker_loop,args=(i,),daemon=True,name=f"arrstream-worker-{i}").start()
    threading.Thread(target=scanner_loop,daemon=True,name="arrstream-scanner").start()


@app.on_event("startup")
def startup():init_db();start_workers()


async def test_arr(kind,url,api_key):
    base=url.rstrip("/");headers={"X-Api-Key":api_key}
    async with httpx.AsyncClient(timeout=8.0) as client:
        status=await client.get(f"{base}/api/v3/system/status",headers=headers);status.raise_for_status();root=await client.get(f"{base}/api/v3/rootfolder",headers=headers);root.raise_for_status()
    payload=status.json();return {"version":payload.get("version","unknown"),"roots":[r.get("path") for r in root.json() if r.get("path")]}


@app.get("/",response_class=HTMLResponse)
def home(request:Request):
    if user_count()==0:return RedirectResponse("/setup/admin",status_code=303)
    if not require_login(request):return RedirectResponse("/login",status_code=303)
    if get_setting("wizard_complete","0")!="1":return RedirectResponse("/wizard",status_code=303)
    return RedirectResponse("/dashboard",status_code=303)


@app.get("/setup/admin",response_class=HTMLResponse)
def setup_admin(request:Request):
    if user_count()>0:return RedirectResponse("/login",status_code=303)
    return templates.TemplateResponse(request,"setup_admin.html",{"title":"Create Admin"})


@app.post("/setup/admin")
def create_admin(request:Request,username:str=Form(...),email:str=Form(""),password:str=Form(...),confirm_password:str=Form(...)):
    if user_count()>0:return RedirectResponse("/login",status_code=303)
    if len(password)<8 or password!=confirm_password:return templates.TemplateResponse(request,"setup_admin.html",{"title":"Create Admin","error":"Passwords must match and be at least 8 characters."},status_code=400)
    try:
        with db() as conn:cur=conn.execute("INSERT INTO users(username,email,password_hash) VALUES(?,?,?)",(username.strip(),email.strip(),pwd_context.hash(password)));uid=cur.lastrowid
        request.session["user_id"]=uid;request.session["username"]=username.strip();return RedirectResponse("/wizard",status_code=303)
    except sqlite3.IntegrityError:return templates.TemplateResponse(request,"setup_admin.html",{"title":"Create Admin","error":"That username already exists."},status_code=400)


@app.get("/login",response_class=HTMLResponse)
def login_page(request:Request):
    if user_count()==0:return RedirectResponse("/setup/admin",status_code=303)
    return templates.TemplateResponse(request,"login.html",{"title":"Sign In"})


@app.post("/login")
def login(request:Request,username:str=Form(...),password:str=Form(...)):
    with db() as conn:row=conn.execute("SELECT * FROM users WHERE username=?",(username.strip(),)).fetchone()
    if not row or not pwd_context.verify(password,row["password_hash"]):return templates.TemplateResponse(request,"login.html",{"title":"Sign In","error":"Invalid username or password."},status_code=401)
    request.session["user_id"]=row["id"];request.session["username"]=row["username"];return RedirectResponse("/wizard" if get_setting("wizard_complete","0")!="1" else "/dashboard",status_code=303)


@app.get("/logout")
def logout(request:Request):request.session.clear();return RedirectResponse("/login",status_code=303)


@app.get("/wizard",response_class=HTMLResponse)
def wizard(request:Request):
    guard=page_guard(request,allow_wizard=True)
    if guard:return guard
    with db() as conn:
        folders=conn.execute("SELECT f.*,p.name profile_name FROM media_folders f LEFT JOIN profiles p ON p.id=f.profile_id ORDER BY f.id").fetchall();profiles=conn.execute("SELECT * FROM profiles WHERE enabled=1 ORDER BY builtin DESC,name").fetchall();instances=conn.execute("SELECT * FROM instances WHERE enabled=1 ORDER BY kind,name").fetchall()
    root=get_setting("container_root",str(MEDIA_ROOT));return templates.TemplateResponse(request,"wizard.html",{"title":"Setup Wizard","settings":settings_dict(),"folders":folders,"profiles":profiles,"instances":instances,"choices":folder_choices(root),"gpus":detect_gpus(),"encoders":ffmpeg_encoders()})


@app.post("/wizard/storage")
async def wizard_storage(request:Request):
    if not require_login(request):return RedirectResponse("/login",status_code=303)
    form=await request.form();save_settings({"storage_mode":form.get("storage_mode","docker"),"host_root":form.get("host_root","").strip(),"container_root":form.get("container_root",str(MEDIA_ROOT)).strip() or str(MEDIA_ROOT)});return RedirectResponse("/wizard#folders",status_code=303)


@app.post("/wizard/hardware")
async def wizard_hardware(request:Request):
    if not require_login(request):return RedirectResponse("/login",status_code=303)
    form=await request.form();save_settings({"hardware_mode":form.get("hardware_mode","auto"),"selected_gpus":",".join(form.getlist("gpu")),"concurrency":form.get("concurrency","1")});return RedirectResponse("/wizard#finish",status_code=303)


@app.post("/wizard/complete")
def wizard_complete(request:Request):
    if not require_login(request):return RedirectResponse("/login",status_code=303)
    with db() as conn:count=conn.execute("SELECT COUNT(*) FROM media_folders WHERE enabled=1").fetchone()[0]
    if count==0:return RedirectResponse("/wizard?error=folder-required",status_code=303)
    save_settings({"wizard_complete":"1"});return RedirectResponse("/dashboard",status_code=303)


@app.post("/folders/add")
async def folder_add(request:Request):
    if not require_login(request):return RedirectResponse("/login",status_code=303)
    form=await request.form();path=(form.get("container_path") or "").strip()
    if not path:return RedirectResponse("/wizard?error=path-required",status_code=303)
    p=Path(path)
    if not p.exists() or not p.is_dir():return RedirectResponse("/wizard?error=path-not-found",status_code=303)
    profile_id=int(form.get("profile_id") or profile_row()["id"])
    with db() as conn:
        try:conn.execute("""INSERT INTO media_folders(name,media_type,host_path,container_path,profile_id,source_kind,source_instance_id,auto_optimize) VALUES(?,?,?,?,?,?,?,?)""",(form.get("name",p.name or "Media").strip(),form.get("media_type","mixed"),form.get("host_path","").strip(),path,profile_id,form.get("source_kind") or None,int(form.get("source_instance_id")) if form.get("source_instance_id") else None,1 if form.get("auto_optimize") else 0))
        except sqlite3.IntegrityError:pass
    return RedirectResponse("/wizard#folders",status_code=303)


@app.post("/folders/{folder_id}/delete")
def folder_delete(folder_id:int,request:Request):
    if not require_login(request):return RedirectResponse("/login",status_code=303)
    with db() as conn:conn.execute("DELETE FROM media_folders WHERE id=?",(folder_id,))
    return RedirectResponse(request.headers.get("referer","/library"),status_code=303)


@app.post("/folders/{folder_id}/toggle")
def folder_toggle(folder_id:int,request:Request):
    if not require_login(request):return RedirectResponse("/login",status_code=303)
    with db() as conn:conn.execute("UPDATE media_folders SET processing_enabled=CASE processing_enabled WHEN 1 THEN 0 ELSE 1 END WHERE id=?",(folder_id,))
    return RedirectResponse("/library",status_code=303)


@app.post("/folders/{folder_id}/profile")
async def folder_profile(folder_id:int,request:Request):
    if not require_login(request):return RedirectResponse("/login",status_code=303)
    form=await request.form();pid=int(form.get("profile_id"))
    with db() as conn:conn.execute("UPDATE media_folders SET profile_id=?,auto_optimize=? WHERE id=?",(pid,1 if form.get("auto_optimize") else 0,folder_id));conn.execute("UPDATE library_items SET profile_id=? WHERE folder_id=?",(pid,folder_id))
    return RedirectResponse("/library",status_code=303)


@app.post("/folders/{folder_id}/scan")
def folder_scan(folder_id:int,request:Request):
    if not require_login(request):return RedirectResponse("/login",status_code=303)
    scan_folder(folder_id,int(get_setting("scan_limit","500")));return RedirectResponse("/library",status_code=303)


@app.get("/setup/instances")
@app.get("/setup/paths")
@app.get("/setup/hardware")
@app.get("/setup/profile")
def legacy_setup_redirect(request:Request):
    if not require_login(request):return RedirectResponse("/login",status_code=303)
    return RedirectResponse("/wizard",status_code=303)


@app.get("/profiles",response_class=HTMLResponse)
def profiles_page(request:Request):
    guard=page_guard(request,allow_wizard=True)
    if guard:return guard
    with db() as conn:profiles=conn.execute("SELECT * FROM profiles WHERE enabled=1 ORDER BY builtin DESC,name").fetchall()
    return templates.TemplateResponse(request,"profiles.html",{"title":"Profiles","profiles":profiles})


@app.post("/profiles/add")
async def profile_add(request:Request):
    if not require_login(request):return RedirectResponse("/login",status_code=303)
    form=await request.form()
    with db() as conn:conn.execute("""INSERT INTO profiles(name,video_codec,audio_codec,container,max_height,quality,preset,audio_bitrate,subtitle_mode,builtin) VALUES(?,?,?,?,?,?,?,?,?,0)""",(form.get("name","Custom").strip(),form.get("video_codec","h264"),form.get("audio_codec","aac"),"mp4",int(form.get("max_height") or 0),int(form.get("quality") or 22),form.get("preset","medium"),form.get("audio_bitrate","160k"),form.get("subtitle_mode","drop")))
    return RedirectResponse("/profiles",status_code=303)


@app.post("/profiles/{profile_id}/delete")
def profile_delete(profile_id:int,request:Request):
    if not require_login(request):return RedirectResponse("/login",status_code=303)
    with db() as conn:
        row=conn.execute("SELECT builtin FROM profiles WHERE id=?",(profile_id,)).fetchone()
        if row and not row["builtin"]:conn.execute("UPDATE profiles SET enabled=0 WHERE id=?",(profile_id,))
    return RedirectResponse("/profiles",status_code=303)


@app.get("/dashboard",response_class=HTMLResponse)
def dashboard(request:Request):
    guard=page_guard(request)
    if guard:return guard
    with db() as conn:
        stats=conn.execute("SELECT COUNT(*) total, SUM(status='completed') done, SUM(status='failed') failed, SUM(status='queued') queued, SUM(status='running') running, COALESCE(SUM(input_bytes-output_bytes),0) saved FROM jobs").fetchone();lib=conn.execute("SELECT COUNT(*) total, SUM(readiness='ready') ready, SUM(readiness='optimize') optimize, SUM(readiness='failed') failed FROM library_items").fetchone();jobs=conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 10").fetchall();folders=conn.execute("SELECT f.*,p.name profile_name FROM media_folders f LEFT JOIN profiles p ON p.id=f.profile_id ORDER BY f.id").fetchall()
    return templates.TemplateResponse(request,"dashboard.html",{"title":"Dashboard","stats":stats,"lib":lib,"jobs":jobs,"folders":folders,"gpus":detect_gpus(),"cpu":psutil.cpu_percent(interval=None),"memory":psutil.virtual_memory().percent,"processing_state":get_setting("processing_state","stopped")})


@app.post("/processing/{action}")
def processing_control(action:str,request:Request):
    if not require_login(request):return RedirectResponse("/login",status_code=303)
    if action=="start":save_settings({"processing_state":"running"})
    elif action=="pause":
        save_settings({"processing_state":"paused"})
        with PROCESS_LOCK:
            for proc in ACTIVE_PROCESSES.values():
                try:os.kill(proc.pid,signal.SIGSTOP)
                except Exception:pass
        with db() as conn:conn.execute("UPDATE jobs SET status='paused' WHERE status='running'")
    elif action=="resume":
        save_settings({"processing_state":"running"})
        with PROCESS_LOCK:
            for proc in ACTIVE_PROCESSES.values():
                try:os.kill(proc.pid,signal.SIGCONT)
                except Exception:pass
        with db() as conn:conn.execute("UPDATE jobs SET status='running' WHERE status='paused'")
    elif action=="stop-after":save_settings({"processing_state":"stopping"})
    elif action=="stop-now":
        save_settings({"processing_state":"stopped"})
        with db() as conn:conn.execute("UPDATE jobs SET status='cancelling' WHERE status IN ('running','paused','starting')")
        with PROCESS_LOCK:
            for proc in ACTIVE_PROCESSES.values():
                try:proc.terminate()
                except Exception:pass
    return RedirectResponse(request.headers.get("referer","/dashboard"),status_code=303)


@app.get("/queue",response_class=HTMLResponse)
def queue_page(request:Request):
    guard=page_guard(request)
    if guard:return guard
    with db() as conn:jobs=conn.execute("SELECT j.*,p.name profile_name,f.name folder_name FROM jobs j LEFT JOIN profiles p ON p.id=j.profile_id LEFT JOIN media_folders f ON f.id=j.folder_id WHERE j.status IN ('queued','starting','running','paused','cancelling') ORDER BY j.id").fetchall()
    return templates.TemplateResponse(request,"queue.html",{"title":"Queue","jobs":jobs,"processing_state":get_setting("processing_state","stopped")})


@app.post("/queue/{job_id}/{action}")
def job_control(job_id:int,action:str,request:Request):
    if not require_login(request):return RedirectResponse("/login",status_code=303)
    with PROCESS_LOCK:proc=ACTIVE_PROCESSES.get(job_id)
    if action=="pause":
        if proc:
            try:os.kill(proc.pid,signal.SIGSTOP)
            except Exception:pass
        with db() as conn:conn.execute("UPDATE jobs SET status='paused' WHERE id=? AND status='running'",(job_id,))
    elif action=="resume":
        if proc:
            try:os.kill(proc.pid,signal.SIGCONT)
            except Exception:pass
        with db() as conn:conn.execute("UPDATE jobs SET status='running' WHERE id=? AND status='paused'",(job_id,))
    elif action=="cancel":
        with db() as conn:conn.execute("UPDATE jobs SET status='cancelling' WHERE id=? AND status IN ('queued','starting','running','paused')",(job_id,))
        if proc:
            try:proc.terminate()
            except Exception:pass
        else:
            with db() as conn:conn.execute("UPDATE jobs SET status='cancelled',finished_at=CURRENT_TIMESTAMP WHERE id=?",(job_id,))
    return RedirectResponse("/queue",status_code=303)


@app.post("/queue/clear")
def queue_clear(request:Request):
    if not require_login(request):return RedirectResponse("/login",status_code=303)
    with db() as conn:conn.execute("DELETE FROM jobs WHERE status IN ('queued','cancelled','failed')")
    return RedirectResponse("/queue",status_code=303)


@app.get("/library",response_class=HTMLResponse)
def library_page(request:Request,status:str="all"):
    guard=page_guard(request)
    if guard:return guard
    with db() as conn:
        folders=conn.execute("SELECT f.*,p.name profile_name FROM media_folders f LEFT JOIN profiles p ON p.id=f.profile_id ORDER BY f.id").fetchall();profiles=conn.execute("SELECT * FROM profiles WHERE enabled=1 ORDER BY builtin DESC,name").fetchall();items=conn.execute("SELECT * FROM library_items WHERE readiness=? ORDER BY id DESC LIMIT 1000",(status,)).fetchall() if status in {"ready","optimize","failed"} else conn.execute("SELECT * FROM library_items ORDER BY id DESC LIMIT 1000").fetchall();counts=conn.execute("SELECT COUNT(*) total, SUM(readiness='ready') ready, SUM(readiness='optimize') optimize, SUM(readiness='failed') failed FROM library_items").fetchone()
    return templates.TemplateResponse(request,"library.html",{"title":"Library","items":items,"counts":counts,"filter":status,"folders":folders,"profiles":profiles})


@app.post("/library/scan")
def library_scan(request:Request):
    if not require_login(request):return RedirectResponse("/login",status_code=303)
    scan_library(int(get_setting("scan_limit","500")));return RedirectResponse("/library",status_code=303)


@app.post("/library/queue-all")
async def library_queue_all(request:Request):
    if not require_login(request):return RedirectResponse("/login",status_code=303)
    form=await request.form();action_filter=form.get("action","all")
    with db() as conn:
        query="SELECT * FROM library_items WHERE readiness='optimize'";params=()
        if action_filter in {"remux","audio","transcode"}:query += " AND action=?";params=(action_filter,)
        for item in conn.execute(query,params).fetchall():
            exists=conn.execute("SELECT 1 FROM jobs WHERE media_path=? AND status IN ('queued','starting','running','paused')",(item["media_path"],)).fetchone()
            if not exists:conn.execute("INSERT INTO jobs(source_kind,source_instance_id,media_path,status,action,input_bytes,folder_id,profile_id,duration_seconds) VALUES(?,?,?,'queued',?,?,?,?,?)",(item["source_kind"],item["source_instance_id"],item["media_path"],item["action"],item["size_bytes"],item["folder_id"],item["profile_id"],item["duration_seconds"]))
    return RedirectResponse("/queue",status_code=303)


@app.post("/library/{item_id}/queue")
def library_queue(item_id:int,request:Request):
    if not require_login(request):return RedirectResponse("/login",status_code=303)
    with db() as conn:
        item=conn.execute("SELECT * FROM library_items WHERE id=?",(item_id,)).fetchone()
        if item and item["readiness"]=="optimize":
            exists=conn.execute("SELECT 1 FROM jobs WHERE media_path=? AND status IN ('queued','starting','running','paused')",(item["media_path"],)).fetchone()
            if not exists:conn.execute("INSERT INTO jobs(source_kind,source_instance_id,media_path,status,action,input_bytes,folder_id,profile_id,duration_seconds) VALUES(?,?,?,'queued',?,?,?,?,?)",(item["source_kind"],item["source_instance_id"],item["media_path"],item["action"],item["size_bytes"],item["folder_id"],item["profile_id"],item["duration_seconds"]))
    return RedirectResponse("/library",status_code=303)


@app.get("/history",response_class=HTMLResponse)
def history_page(request:Request):
    guard=page_guard(request)
    if guard:return guard
    with db() as conn:jobs=conn.execute("SELECT j.*,p.name profile_name FROM jobs j LEFT JOIN profiles p ON p.id=j.profile_id WHERE j.status NOT IN ('queued','starting','running','paused','cancelling') ORDER BY j.id DESC LIMIT 1000").fetchall()
    return templates.TemplateResponse(request,"history.html",{"title":"History","jobs":jobs})


@app.get("/connections",response_class=HTMLResponse)
def connections_page(request:Request):
    guard=page_guard(request,allow_wizard=True)
    if guard:return guard
    rows=[]
    with db() as conn:instances=conn.execute("SELECT * FROM instances ORDER BY kind,name").fetchall()
    for inst in instances:
        try:r=httpx.get(f"{inst['url'].rstrip('/')}/api/v3/system/status",headers={"X-Api-Key":inst["api_key"]},timeout=5);r.raise_for_status();rows.append({"instance":inst,"ok":True,"version":r.json().get("version",inst["version"] or "unknown"),"message":"Connected"})
        except Exception as exc:rows.append({"instance":inst,"ok":False,"version":inst["version"] or "unknown","message":str(exc)})
    return templates.TemplateResponse(request,"connections.html",{"title":"Connections","rows":rows})


@app.post("/connections/add")
async def connections_add(request:Request,kind:str=Form(...),name:str=Form(...),url:str=Form(...),api_key:str=Form(...)):
    if not require_login(request):return RedirectResponse("/login",status_code=303)
    result=await test_arr(kind,url,api_key)
    with db() as conn:conn.execute("INSERT INTO instances(kind,name,url,api_key,version,root_folders) VALUES(?,?,?,?,?,?)",(kind,name.strip(),url.rstrip("/"),api_key.strip(),result["version"],"\n".join(result["roots"])))
    return RedirectResponse("/connections",status_code=303)


@app.post("/connections/{instance_id}/delete")
def connections_delete(instance_id:int,request:Request):
    if not require_login(request):return RedirectResponse("/login",status_code=303)
    with db() as conn:conn.execute("DELETE FROM instances WHERE id=?",(instance_id,))
    return RedirectResponse("/connections",status_code=303)


@app.get("/diagnostics",response_class=HTMLResponse)
def diagnostics_page(request:Request):
    guard=page_guard(request)
    if guard:return guard
    checks=[{"name":"FFmpeg","ok":bool(ffmpeg_encoders()),"detail":", ".join(ffmpeg_encoders()) or "Not available"},{"name":"Media root","ok":Path(get_setting("container_root",str(MEDIA_ROOT))).exists(),"detail":get_setting("container_root",str(MEDIA_ROOT))},{"name":"Config writable","ok":os.access(CONFIG_DIR,os.W_OK),"detail":str(CONFIG_DIR)},{"name":"GPU access","ok":bool(detect_gpus()),"detail":", ".join(g["name"] for g in detect_gpus()) or "No GPU exposed; CPU mode available"}]
    with db() as conn:folders=conn.execute("SELECT * FROM media_folders ORDER BY id").fetchall()
    path_checks=[{"instance":f["name"],"path":f["container_path"],"exists":Path(f["container_path"]).exists(),"readable":os.access(f["container_path"],os.R_OK),"writable":os.access(f["container_path"],os.W_OK)} for f in folders]
    return templates.TemplateResponse(request,"diagnostics.html",{"title":"Diagnostics","checks":checks,"path_checks":path_checks})


@app.get("/settings",response_class=HTMLResponse)
def settings_page(request:Request):
    guard=page_guard(request)
    if guard:return guard
    return templates.TemplateResponse(request,"settings.html",{"title":"Settings","settings":settings_dict(),"gpus":detect_gpus(),"encoders":ffmpeg_encoders(),"media_root":MEDIA_ROOT})


@app.post("/settings")
async def settings_save(request:Request):
    if not require_login(request):return RedirectResponse("/login",status_code=303)
    form=await request.form();save_settings({"hardware_mode":form.get("hardware_mode","auto"),"selected_gpus":",".join(form.getlist("gpu")),"concurrency":form.get("concurrency","1"),"auto_optimize":"1" if form.get("auto_optimize") else "0","periodic_scan_minutes":form.get("periodic_scan_minutes","60"),"scan_limit":form.get("scan_limit","500"),"schedule_enabled":"1" if form.get("schedule_enabled") else "0","process_window_start":form.get("process_window_start","00:00"),"process_window_end":form.get("process_window_end","23:59")});return RedirectResponse("/settings?saved=1",status_code=303)


@app.get("/api/v1/health")
def api_health():return {"status":"ok","app":APP_NAME,"version":APP_VERSION,"wizard_complete":get_setting("wizard_complete","0")=="1","processing_state":get_setting("processing_state","stopped"),"port":PORT}


@app.get("/api/v1/system")
def api_system():return {"cpu_percent":psutil.cpu_percent(interval=None),"memory_percent":psutil.virtual_memory().percent,"gpus":detect_gpus(),"encoders":ffmpeg_encoders(),"media_root":str(MEDIA_ROOT)}


@app.get("/api/v1/jobs")
def api_jobs():
    with db() as conn:rows=conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 500").fetchall()
    return {"jobs":[dict(r) for r in rows]}


@app.get("/api/v1/readiness")
def api_readiness():
    with db() as conn:r=conn.execute("SELECT COUNT(*) total, SUM(readiness='ready') ready, SUM(readiness='optimize') optimize, SUM(readiness='failed') failed FROM library_items").fetchone()
    return dict(r)


@app.get("/api/v1/issues")
def api_issues():
    issues=[]
    if user_count()==0:issues.append({"code":"ADMIN_NOT_CREATED","severity":"critical","message":"Initial admin user has not been created."})
    if get_setting("wizard_complete","0")!="1":issues.append({"code":"WIZARD_INCOMPLETE","severity":"warning","message":"Initial setup wizard is not complete."})
    if not ffmpeg_encoders():issues.append({"code":"FFMPEG_MISSING","severity":"critical","message":"FFmpeg is not available inside the ArrStream container."})
    with db() as conn:bad=conn.execute("SELECT name,container_path FROM media_folders").fetchall()
    for f in bad:
        if not Path(f["container_path"]).exists():issues.append({"code":"PATH_NOT_FOUND","severity":"critical","message":f"{f['name']}: {f['container_path']} is not available."})
    return {"status":"ok" if not issues else "degraded","issues":issues}
