"""FastAPI 应用：用户体系 + 任务队列（asyncio）+ 蒸馏 worker + 静态页面服务。

多用户模型（"用户自带一切"）：
- 用户注册/登录（用户名+密码，session cookie 鉴权）
- 每用户存自己的 Z-Library 凭证（加密）+ 私有 HOME，zlib 子进程以该 HOME 运行，
  下载消耗用户自己账号的积分，互不干扰
- 每用户存自己的 LLM 设置（加密），蒸馏 key 从库解密注入子进程 env
- 每用户笔记存 data/users/<uid>/notes/，任务与日志落 SQLite（重启不丢）

运行（项目根目录）：
  .venv/Scripts/python -m uvicorn web.app:app --port 8000
"""
import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from datetime import datetime
from functools import wraps

from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Depends
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel
from cryptography.fernet import Fernet

from src.config import load_config
from src.zlib_client import (
    ZlibClient, ZlibError, write_user_session, user_has_session,
)

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(ROOT, "web")
DATA_DIR = os.path.join(ROOT, "data")
USERS_DIR = os.path.join(DATA_DIR, "users")
DB_PATH = os.path.join(DATA_DIR, "app.db")
MASTER_KEY_PATH = os.path.join(DATA_DIR, "master.key")
TASKS = {}  # 内存里的活跃任务（id -> dict）；历史/持久化在 SQLite
WORKER_LOCK = asyncio.Lock()
_DB_LOCK = threading.Lock()

# 上传支持格式（与 src.extract 能力一致）
ALLOWED_EXTS = {".pdf", ".epub", ".txt", ".mobi", ".azw3"}
MAX_UPLOAD_MB = 200


# ---------- 存储 / 加密 / 会话 ----------

def _ensure_data():
    os.makedirs(USERS_DIR, exist_ok=True)


def _fernet() -> Fernet:
    if not os.path.exists(MASTER_KEY_PATH):
        key = Fernet.generate_key()
        with open(MASTER_KEY_PATH, "wb") as f:
            f.write(key)
        try:
            os.chmod(MASTER_KEY_PATH, 0o600)
        except OSError:
            pass
    with open(MASTER_KEY_PATH, "rb") as f:
        return Fernet(f.read())


FERNET = None  # 懒加载，见 _get_fernet


def _get_fernet() -> Fernet:
    global FERNET
    if FERNET is None:
        FERNET = _fernet()
    return FERNET


def enc(s: str) -> str:
    return _get_fernet().encrypt((s or "").encode("utf-8")).decode("ascii")


def dec(token: str) -> str:
    if not token:
        return ""
    return _get_fernet().decrypt(token.encode("ascii")).decode("utf-8")


def hash_password(pw: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt.encode("utf-8"), 120_000)
    return f"pbkdf2${salt}${dk.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, salt, hexdk = stored.split("$")
    except ValueError:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt.encode("utf-8"), 120_000)
    return secrets.compare_digest(dk.hex(), hexdk)


# ---------- SQLite ----------

def _db() -> sqlite3.Connection:
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _init_db():
    with _DB_LOCK, _db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users(
          id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL,
          pass_hash TEXT NOT NULL, created TEXT);
        CREATE TABLE IF NOT EXISTS user_creds(
          user_id TEXT PRIMARY KEY,
          llm_provider TEXT, llm_base_url TEXT, llm_model TEXT, llm_key_enc TEXT,
          zlib_userid_enc TEXT, zlib_userkey_enc TEXT, zlib_domain TEXT);
        CREATE TABLE IF NOT EXISTS sessions(
          token TEXT PRIMARY KEY, user_id TEXT, created TEXT, expires TEXT);
        CREATE TABLE IF NOT EXISTS jobs(
          id TEXT PRIMARY KEY, user_id TEXT, mode TEXT, book_title TEXT,
          status TEXT, created TEXT, started TEXT, finished TEXT,
          book_slug TEXT, error TEXT, req_json TEXT, result_json TEXT);
        CREATE TABLE IF NOT EXISTS job_logs(
          job_id TEXT, ts TEXT, line TEXT,
          PRIMARY KEY(job_id, ts, line));
        CREATE INDEX IF NOT EXISTS idx_jobs_user ON jobs(user_id);
        """)


def _now():
    return datetime.now().isoformat(timespec="seconds")


# 每用户路径
def user_dir(uid: str) -> str:
    d = os.path.join(USERS_DIR, uid)
    os.makedirs(d, exist_ok=True)
    return d


def user_upload_dir(uid: str) -> str:
    d = os.path.join(user_dir(uid), "uploads")
    os.makedirs(d, exist_ok=True)
    return d


def user_notes_dir(uid: str) -> str:
    d = os.path.join(user_dir(uid), "notes")
    os.makedirs(d, exist_ok=True)
    return d


def user_zlib_home(uid: str) -> str:
    d = os.path.join(user_dir(uid), "zlibhome")
    os.makedirs(d, exist_ok=True)
    return d


def get_creds(uid: str) -> dict:
    with _DB_LOCK, _db() as conn:
        row = conn.execute("SELECT * FROM user_creds WHERE user_id=?", (uid,)).fetchone()
    if not row:
        return {"user_id": uid, "llm_provider": None, "llm_base_url": None,
                "llm_model": None, "llm_key_enc": None,
                "zlib_userid_enc": None, "zlib_userkey_enc": None, "zlib_domain": None}
    return dict(row)


def upsert_creds(uid: str, **fields):
    cols = ["llm_provider", "llm_base_url", "llm_model", "llm_key_enc",
            "zlib_userid_enc", "zlib_userkey_enc", "zlib_domain"]
    with _DB_LOCK, _db() as conn:
        row = conn.execute("SELECT * FROM user_creds WHERE user_id=?", (uid,)).fetchone()
        if row is None:
            cur = dict((c, None) for c in cols)
            cur["user_id"] = uid
        else:
            cur = dict(row)
        for k, v in fields.items():
            if k in cols and v is not None:
                cur[k] = v
        placeholders = ",".join("?" for _ in list(cur.keys()))
        conn.execute(
            f"INSERT OR REPLACE INTO user_creds({','.join(cur.keys())}) VALUES({placeholders})",
            list(cur.values()),
        )


# 任务持久化
def save_job(task: dict):
    with _DB_LOCK, _db() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO jobs
               (id,user_id,mode,book_title,status,created,started,finished,book_slug,error,req_json,result_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (task["id"], task.get("user_id"), task.get("mode"), task.get("book_title"),
             task.get("status"), task.get("created"), task.get("started"), task.get("finished"),
             task.get("book_slug"), task.get("error"),
             json.dumps(task.get("_req", {}), ensure_ascii=False),
             json.dumps(task.get("result"), ensure_ascii=False) if task.get("result") else None),
        )
        # 日志：全量重写（演示规模够用；MVP 不追求增量）
        conn.execute("DELETE FROM job_logs WHERE job_id=?", (task["id"],))
        for line in task.get("logs", [])[-200:]:
            conn.execute("INSERT OR IGNORE INTO job_logs(job_id,ts,line) VALUES(?,?,?)",
                         (task["id"], "", line))


def load_jobs(uid: str, limit: int = 50) -> list:
    with _DB_LOCK, _db() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE user_id=? ORDER BY created DESC LIMIT ?", (uid, limit)).fetchall()
    out = []
    for r in rows:
        logs = [x["line"] for x in _db().execute(
            "SELECT line FROM job_logs WHERE job_id=? ", (r["id"],)).fetchall()]
        out.append({
            "id": r["id"], "mode": r["mode"], "book_title": r["book_title"],
            "status": r["status"], "created": r["created"], "finished": r["finished"],
            "book_slug": r["book_slug"], "error": r["error"], "logs": logs[-80:],
            "llm_model": json.loads(r["req_json"] or "{}").get("llm", {}).get("model", ""),
        })
    return out


def recover_interrupted():
    """启动时把上次遗留的 running/queued 任务标记为中断失败。"""
    with _DB_LOCK, _db() as conn:
        conn.execute(
            "UPDATE jobs SET status='failed', error='服务重启，任务中断', finished=? "
            "WHERE status IN ('running','queued')", (_now(),))


# ---------- 鉴权依赖 ----------

def current_user(request: Request):
    token = request.cookies.get("session")
    if not token:
        raise HTTPException(401, "未登录")
    with _DB_LOCK, _db() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE token=?", (token,)).fetchone()
    if not row or (row["expires"] and row["expires"] < _now()):
        raise HTTPException(401, "登录已过期")
    return row["user_id"]


def _make_session(uid: str):
    token = secrets.token_hex(32)
    expires = datetime.fromtimestamp(time.time() + 7 * 86400).isoformat(timespec="seconds")
    with _DB_LOCK, _db() as conn:
        conn.execute("INSERT INTO sessions(token,user_id,created,expires) VALUES(?,?,?,?)",
                     (token, uid, _now(), expires))
    return token, expires


def _log(task: dict, msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    task["logs"].append(f"[{ts}] {msg}")
    task["logs"] = task["logs"][-200:]


def _public(task: dict) -> dict:
    """去掉内部字段，返回可序列化视图（绝不含 API key / 凭证）。"""
    d = {k: v for k, v in task.items() if not k.startswith("_")}
    d["logs"] = task["logs"][-80:]
    return d


class JobRequest(BaseModel):
    mode: str = "upload"  # upload | search
    book_title: str = ""
    author: str = ""
    file_format: str = "epub"
    zlib_id: str = ""  # 搜索模式选定候选后回传
    upload_id: str = ""  # 上传模式回传


class AuthRequest(BaseModel):
    username: str
    password: str


class LlmSettings(BaseModel):
    provider: str  # anthropic | openai
    base_url: str = ""
    model: str = ""
    api_key: str = ""


class ZlibCreds(BaseModel):
    userid: str
    userkey: str
    domain: str = ""


app = FastAPI(title="懒人读书 Web")


@app.on_event("startup")
def _startup():
    load_config()
    _ensure_data()
    _init_db()
    recover_interrupted()
    logger.info("web 启动完成，data=%s", DATA_DIR)


# ---------- 认证 ----------

@app.post("/api/register")
def register(req: AuthRequest):
    uname = req.username.strip()
    if not re.match(r"^[A-Za-z0-9_\-]{2,32}$", uname):
        raise HTTPException(400, "用户名需 2-32 位字母/数字/_/-")
    if len(req.password) < 6:
        raise HTTPException(400, "密码至少 6 位")
    uid = uuid.uuid4().hex[:16]
    try:
        with _DB_LOCK, _db() as conn:
            conn.execute("INSERT INTO users(id,username,pass_hash,created) VALUES(?,?,?,?)",
                         (uid, uname, hash_password(req.password), _now()))
    except sqlite3.IntegrityError:
        raise HTTPException(400, "用户名已存在")
    token, expires = _make_session(uid)
    resp = JSONResponse({"ok": True, "username": uname})
    resp.set_cookie("session", token, max_age=7 * 86400, httponly=True, samesite="lax")
    return resp


@app.post("/api/login")
def login(req: AuthRequest):
    with _DB_LOCK, _db() as conn:
        row = conn.execute("SELECT * FROM users WHERE username=?",
                           (req.username.strip(),)).fetchone()
    if not row or not verify_password(req.password, row["pass_hash"]):
        raise HTTPException(401, "用户名或密码错误")
    token, expires = _make_session(row["id"])
    resp = JSONResponse({"ok": True, "username": row["username"]})
    resp.set_cookie("session", token, max_age=7 * 86400, httponly=True, samesite="lax")
    return resp


@app.post("/api/logout")
def logout(request: Request):
    token = request.cookies.get("session")
    if token:
        with _DB_LOCK, _db() as conn:
            conn.execute("DELETE FROM sessions WHERE token=?", (token,))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("session")
    return resp


@app.get("/api/me")
def me(uid: str = Depends(current_user)):
    with _DB_LOCK, _db() as conn:
        row = conn.execute("SELECT username,created FROM users WHERE id=?", (uid,)).fetchone()
    return {"uid": uid, "username": row["username"] if row else "",
            "created": row["created"] if row else ""}


# ---------- 用户凭证（LLM + zlib） ----------

@app.get("/api/me/status")
def me_status(uid: str = Depends(current_user)):
    binary = os.environ.get("ZLIB_BIN") or "zlib"
    creds = get_creds(uid)
    return {
        "zlib_installed": shutil.which(binary) is not None,
        "zlib_binary": binary,
        "zlib_configured": user_has_session(user_zlib_home(uid)),
        "llm_configured": bool(creds.get("llm_key_enc")),
        "llm_provider": creds.get("llm_provider"),
        "llm_model": creds.get("llm_model"),
    }


@app.get("/api/me/llm")
def get_llm(uid: str = Depends(current_user)):
    c = get_creds(uid)
    return {"provider": c.get("llm_provider") or "openai",
            "base_url": c.get("llm_base_url") or "", "model": c.get("llm_model") or "",
            "has_key": bool(c.get("llm_key_enc"))}


@app.put("/api/me/llm")
def put_llm(req: LlmSettings, uid: str = Depends(current_user)):
    fields = {"llm_provider": req.provider, "llm_base_url": req.base_url,
              "llm_model": req.model}
    if req.api_key:  # 空则保留原 key
        fields["llm_key_enc"] = enc(req.api_key)
    upsert_creds(uid, **fields)
    return {"ok": True}


@app.put("/api/me/zlib")
def put_zlib(req: ZlibCreds, uid: str = Depends(current_user)):
    home = user_zlib_home(uid)
    write_user_session(home, {"remix_userid": req.userid, "remix_userkey": req.userkey},
                       domain=req.domain or None)
    upsert_creds(uid, zlib_userid_enc=enc(req.userid), zlib_userkey_enc=enc(req.userkey),
                 zlib_domain=req.domain)
    return {"ok": True}


# ---------- 上传 / 搜索（按用户） ----------

@app.post("/api/upload")
async def upload(request: Request, file: UploadFile = File(...)):
    uid = current_user(request)
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in ALLOWED_EXTS:
        raise HTTPException(400, f"不支持的格式：{ext}（支持 {sorted(ALLOWED_EXTS)}）")
    updir = user_upload_dir(uid)
    upload_id = uuid.uuid4().hex[:12]
    dest = os.path.join(updir, upload_id + ext)
    size = 0
    with open(dest, "wb") as out:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD_MB * 1024 * 1024:
                out.close()
                os.remove(dest)
                raise HTTPException(400, f"文件超过 {MAX_UPLOAD_MB}MB 上限")
            out.write(chunk)
    _upload_meta(updir, upload_id, file.filename, size)
    return {"upload_id": upload_id, "filename": file.filename, "size": size}


def _upload_meta(updir: str, upload_id: str, filename: str, size: int):
    with open(os.path.join(updir, upload_id + ".json"), "w", encoding="utf-8") as f:
        json.dump({"filename": filename, "size": size, "time": _now()}, f, ensure_ascii=False)


def _load_upload(uid: str, upload_id: str):
    updir = user_upload_dir(uid)
    for ext in ALLOWED_EXTS:
        p = os.path.join(updir, upload_id + ext)
        if os.path.exists(p):
            return p
    return None


@app.get("/api/zlib/search")
def zlib_search(q: str, author: str = "", uid: str = Depends(current_user)):
    home = user_zlib_home(uid)
    if not user_has_session(home):
        return JSONResponse({"error": "请先在“账户”里配置你的 Z-Library 凭证（Cookie）"})
    try:
        books = ZlibClient(home=home).search(q, author or None, limit=10)
    except ZlibError as e:
        raise HTTPException(502, f"zlib 搜索失败：{e}")
    return {"results": [b.to_dict() for b in books]}


# ---------- 任务 ----------

@app.post("/api/jobs")
async def create_job(req: JobRequest, request: Request):
    uid = current_user(request)
    creds = get_creds(uid)
    if not creds.get("llm_key_enc") and not (req.mode == "upload" and os.environ.get("ANTHROPIC_API_KEY")):
        raise HTTPException(400, "请先在“账户”里配置 LLM API Key")

    if req.mode not in ("upload", "search"):
        raise HTTPException(400, "mode 必须是 upload 或 search")
    if req.mode == "upload":
        raw = _load_upload(uid, req.upload_id)
        if not raw:
            raise HTTPException(400, "upload_id 无效或文件已清理，请重新上传")
        if not req.book_title:
            req.book_title = os.path.splitext(os.path.basename(raw))[0]
    else:
        if not req.zlib_id or not req.book_title:
            raise HTTPException(400, "搜索模式需要 book_title 和 zlib_id（先调 /api/zlib/search）")
        if not user_has_session(user_zlib_home(uid)):
            raise HTTPException(400, "请先配置 Z-Library 凭证")

    # 该用户的 LLM 配置（从库解密），不再信任前端传 key
    llm = {
        "provider": creds.get("llm_provider") or "anthropic",
        "api_key": dec(creds.get("llm_key_enc") or ""),
        "base_url": creds.get("llm_base_url") or "",
        "model": creds.get("llm_model") or "",
    }

    task = {
        "id": uuid.uuid4().hex[:12],
        "user_id": uid,
        "mode": req.mode,
        "book_title": req.book_title,
        "author": req.author,
        "status": "queued",
        "created": _now(),
        "finished": None,
        "book_slug": None,
        "llm_provider": llm["provider"],
        "llm_model": llm["model"],
        "logs": [],
        "result": None,
        "error": None,
        "_req": req.model_dump() | {"llm": {"provider": llm["provider"], "model": llm["model"]}},
    }
    TASKS[task["id"]] = task
    save_job(task)
    asyncio.ensure_future(_process(task["id"]))
    return _public(task)


@app.get("/api/jobs")
def list_jobs(uid: str = Depends(current_user)):
    # 活跃任务（内存，带实时日志）+ 历史（库里）
    active = [_public(t) for t in TASKS.values() if t.get("user_id") == uid]
    active_ids = {t["id"] for t in active}
    history = [j for j in load_jobs(uid) if j["id"] not in active_ids]
    return (active + history)[:50]


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, uid: str = Depends(current_user)):
    t = TASKS.get(job_id)
    if t and t.get("user_id") == uid:
        return _public(t)
    with _DB_LOCK, _db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=? AND user_id=?", (job_id, uid)).fetchone()
    if not row:
        raise HTTPException(404, "任务不存在")
    logs = [x["line"] for x in _db().execute(
        "SELECT line FROM job_logs WHERE job_id=?", (job_id,)).fetchall()]
    return {"id": row["id"], "mode": row["mode"], "book_title": row["book_title"],
            "status": row["status"], "created": row["created"], "finished": row["finished"],
            "book_slug": row["book_slug"], "error": row["error"], "logs": logs[-80:],
            "llm_model": json.loads(row["req_json"] or "{}").get("llm", {}).get("model", "")}


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str, uid: str = Depends(current_user)):
    t = TASKS.get(job_id)
    if not t or t.get("user_id") != uid or t["status"] not in ("queued", "running"):
        raise HTTPException(400, "任务不在运行中")
    t["_cancel"] = True
    _log(t, "收到取消请求，将在当前步骤结束后停止")
    save_job(t)
    return _public(t)


# ---------- 蒸馏 worker ----------

async def _process(job_id: str):
    t = TASKS.get(job_id)
    if t is None:
        return
    async with WORKER_LOCK:  # 串行执行，避免并发打爆 LLM / 磁盘
        if t["status"] == "running":
            return
        t["status"] = "running"
        t["started"] = _now()
        save_job(t)
        req = t["_req"]
        llm = _user_llm(t["user_id"])
        try:
            raw_path = await asyncio.to_thread(_prepare_raw, t, req)
            if t.get("_cancel"):
                t["status"] = "canceled"
            else:
                env = _llm_env(llm)
                book_dir = await asyncio.to_thread(_run_distill, t, raw_path, req, env)
                t["book_slug"] = os.path.basename(book_dir)
                t["status"] = "done"
                t["result"] = {"book_dir": book_dir}
        except Exception as e:
            logger.exception("job %s failed", job_id)
            t["status"] = "failed"
            t["error"] = str(e)[:1000]
        t["finished"] = _now()
        save_job(t)


def _user_llm(uid: str) -> dict:
    creds = get_creds(uid)
    return {"provider": creds.get("llm_provider") or "anthropic",
            "api_key": dec(creds.get("llm_key_enc") or ""),
            "base_url": creds.get("llm_base_url") or "",
            "model": creds.get("llm_model") or ""}


def _prepare_raw(t: dict, req: dict) -> str:
    uid = t["user_id"]
    if req["mode"] == "upload":
        _log(t, f"使用已上传文件: {req['book_title']}")
        return _load_upload(uid, req["upload_id"])
    _log(t, f"zlib 下载: {req['book_title']} ({req.get('file_format') or 'epub'}) ...")
    from src.download import pick_format
    home = user_zlib_home(uid)
    client = ZlibClient(home=home)
    # 搜索定位 zlib_id 对应的候选（zlib download 需要 id:hash）
    books = client.search(req["book_title"], req.get("author") or None, limit=10)
    chosen = next((b for b in books if b.zlib_id == req["zlib_id"]), None)
    if chosen is None:
        raise RuntimeError(f"zlib 搜索中未找到 id={req['zlib_id']} 的候选，请重新搜索")
    preferred = req.get("file_format") or "epub"
    fmt = pick_format(chosen, preferred)
    dest_dir = user_upload_dir(uid)
    path = client.download(chosen, dest_dir)
    _log(t, f"下载完成: {os.path.basename(path)}")
    return path


def _llm_env(llm: dict) -> dict:
    """把用户填的 key 放进子进程环境变量（不落盘）。"""
    env = os.environ.copy()
    if llm.get("provider") == "openai":
        env["LLM_PROVIDER"] = "openai"
        env["OPENAI_API_KEY"] = llm.get("api_key") or "sk-no-key-local"
        if llm.get("base_url") and llm["base_url"] != "local":
            env["OPENAI_BASE_URL"] = llm["base_url"]
    else:
        env["LLM_PROVIDER"] = "anthropic"
        env["ANTHROPIC_API_KEY"] = llm.get("api_key") or ""
    if llm.get("model"):
        env["DISTILL_MODEL"] = llm["model"]
    # 清掉 book-config 里可能带进来的 key，确保用的是用户这次的
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DISTILL_MODEL"):
        if k not in env:
            env.pop(k, None)
    return env


def _run_distill(t: dict, raw_path: str, req: dict, env: dict) -> str:
    """子进程跑 CLI pipeline（复用现有全部逻辑），实时转储输出到 task logs。

    子进程显式设置 PYTHONIOENCODING=utf-8：Windows 控制台默认 GBK 代码页，
    子进程 print 中文会 UnicodeEncodeError 直接崩掉。
    """
    argv = [sys_python(), "-m", "src.pipeline", "--book", req["book_title"]]
    if req.get("author"):
        argv += ["--author", req["author"]]
    argv += ["--skip-download", "--raw-file", raw_path]
    if req.get("mode") == "search":
        argv += ["--format", req.get("file_format") or "epub"]
    if req.get("llm", {}).get("provider") == "openai":
        argv += ["--provider", "openai"]
    if req.get("llm", {}).get("model"):
        argv += ["--model", req["llm"]["model"]]

    env = dict(env)
    env["PYTHONIOENCODING"] = "utf-8"
    env["BOOK_NOTES_DIR"] = user_notes_dir(t["user_id"])
    _log(t, "启动蒸馏子进程 ...")
    proc = subprocess.Popen(
        argv, cwd=ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
        bufsize=1,
    )
    # 逐行读输出，转成任务日志（前端轮询展示）
    import threading

    def pump():
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                _log(t, line)
        proc.wait()

    th = threading.Thread(target=pump, daemon=True)
    th.start()
    while th.is_alive():
        if t.get("_cancel"):
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            _log(t, "已取消")
            raise RuntimeError("任务已取消")
        th.join(timeout=1)
    if proc.returncode != 0:
        raise RuntimeError(f"蒸馏进程退出码 {proc.returncode}，日志见上方")
    # 输出目录在**该用户**的 notes/ 下（以书名安全化为目录名）
    from src.pipeline import _safe_filename
    book_dir = os.path.join(user_notes_dir(t["user_id"]), _safe_filename(req["book_title"]))
    if not os.path.isdir(book_dir):
        raise RuntimeError("蒸馏结束但未找到输出目录，请检查日志")
    _log(t, f"蒸馏完成：{book_dir}")
    return book_dir


def sys_python() -> str:
    import sys
    return sys.executable or "python"


# ---------- 书库 / 成品页（按用户） ----------

def _user_notes(uid: str) -> str:
    return user_notes_dir(uid)


@app.get("/api/books")
def list_books(uid: str = Depends(current_user)):
    books = []
    ndir = _user_notes(uid)
    if not os.path.isdir(ndir):
        return books
    for name in sorted(os.listdir(ndir)):
        d = os.path.join(ndir, name)
        if not os.path.isdir(d):
            continue
        readme = os.path.join(d, "README.md")
        overview = ""
        if os.path.exists(readme):
            with open(readme, "r", encoding="utf-8") as f:
                text = f.read()
            m = re.search(r"## 全书概览\s*\n+(.+?)(?:\n##|\Z)", text, re.S)
            if m:
                overview = m.group(1).strip()[:300]
        html = [f for f in os.listdir(d) if f.lower().endswith(".html")]
        books.append({"slug": name, "title": name, "overview": overview,
                      "has_html": bool(html)})
    return books


@app.get("/book/{uid}/{slug}")
def book_page(uid: str, slug: str):
    d = _safe_path(_user_notes(uid), slug)
    htmls = [f for f in os.listdir(d) if f.lower().endswith(".html")] if os.path.isdir(d) else []
    if not htmls:
        raise HTTPException(404, "这本书还没有生成网页")
    with open(os.path.join(d, sorted(htmls)[0]), "r", encoding="utf-8") as f:
        html = f.read()
    # 书 HTML 里原文引用是相对路径（如 "书名.pdf"），file:// 打开时正确；
    # 走 /book/<uid>/<slug> URL 访问时会被解析到错误路径（404），
    # 用一个 <base> 把它们改写到 /file/<uid>/<slug>/ 下。href 由紧随的 inline
    # script 在解析时设置（声明式属性不行——解析期设 href 会被解析成空路径）。
    # 本地直接打开文件时该前缀不存在，相对路径解析保持原样，双兼容。
    html = html.replace(
        '<meta charset="utf-8">',
        f'<meta charset="utf-8">\n'
        f'<base id="__webrebase" fetchmod="x" data-rebase="/file/{uid}/{slug}/">'
        f'<script>var b=document.getElementById("__webrebase");'
        f'if(b)b.href=b.getAttribute("data-rebase");</script>',
        1,
    )
    return HTMLResponse(html)


@app.get("/file/{uid}/{slug}/{filename}")
def book_file(uid: str, slug: str, filename: str):
    d = _safe_path(_user_notes(uid), slug)
    full = _safe_path(d, filename)
    if not os.path.isfile(full):
        raise HTTPException(404, "文件不存在")
    media = {".pdf": "application/pdf", ".epub": "application/epub",
             ".txt": "text/plain; charset=utf-8"}
    ext = os.path.splitext(filename)[1].lower()
    return FileResponse(full, media_type=media.get(ext, "application/octet-stream"))


def _safe_path(base: str, *parts: str) -> str:
    """拼路径并防目录穿越。"""
    full = os.path.realpath(os.path.join(base, *parts))
    if not full.startswith(os.path.realpath(base) + os.sep) and full != os.path.realpath(base):
        raise HTTPException(400, "非法路径")
    return full


# ---------- 首页 ----------

@app.get("/", response_class=HTMLResponse)
async def index():
    with open(os.path.join(WEB_DIR, "index.html"), "r", encoding="utf-8") as f:
        return f.read()
