import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite
import pydantic
import pyotp
from fastapi import FastAPI, Request, Depends, HTTPException, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

logging.basicConfig(level=logging.INFO, format="%(asctime)s [api] %(levelname)s %(message)s")
logger = logging.getLogger("api")

DB_PATH = "/data/starlink.db"
FRONTEND_DIR = Path(__file__).parent
STATIC_DIR = FRONTEND_DIR / "static"

ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "changeme")

if ADMIN_PASS == "changeme":
    logger.warning(
        "ADMIN_PASS is still the default 'changeme' - change it in .env for production!"
    )

SESSION_SECRET = os.environ.get("SESSION_SECRET", "").strip()
if not SESSION_SECRET:
    raise RuntimeError(
        "SESSION_SECRET is not set or empty. Generate one (e.g. "
        "`openssl rand -hex 32`) and set it in .env before starting. "
        "Refusing to start with an insecure session signing key."
    )

COOKIE_NAME = "sm_session"
SESSION_TTL_S = 12 * 3600
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "false").lower() == "true"

TOTP_SECRET = os.environ.get("TOTP_SECRET", "").strip()

LAN_FALLBACK_PIN = os.environ.get("LAN_FALLBACK_PIN", "").strip()
LAN_FALLBACK_CIDRS = [
    c.strip() for c in os.environ.get("LAN_FALLBACK_CIDRS", "").split(",") if c.strip()
]
try:
    LAN_FALLBACK_NETWORKS = [
        ipaddress.ip_network(c, strict=False) for c in LAN_FALLBACK_CIDRS
    ]
except ValueError as exc:
    raise RuntimeError(f"Invalid network in LAN_FALLBACK_CIDRS: {exc}") from exc

if LAN_FALLBACK_PIN and not LAN_FALLBACK_NETWORKS:
    logger.warning(
        "LAN_FALLBACK_PIN is set but LAN_FALLBACK_CIDRS is empty - LAN PIN fallback disabled."
    )
if LAN_FALLBACK_NETWORKS and not LAN_FALLBACK_PIN:
    logger.warning(
        "LAN_FALLBACK_CIDRS is set but LAN_FALLBACK_PIN is empty - LAN PIN fallback disabled."
    )
if LAN_FALLBACK_PIN and len(LAN_FALLBACK_PIN) < 8:
    logger.warning("LAN_FALLBACK_PIN is shorter than 8 characters - consider a longer PIN.")


def _lan_pin_fallback_active() -> bool:
    return bool(LAN_FALLBACK_PIN and LAN_FALLBACK_NETWORKS)


def _client_in_lan(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in LAN_FALLBACK_NETWORKS)


def _is_https_request(request: Request) -> bool:
    if request.url.scheme == "https":
        return True
    forwarded_proto = request.headers.get("x-forwarded-proto", "")
    return forwarded_proto.split(",")[0].strip().lower() == "https"


LOGIN_MAX_ATTEMPTS = 5
LOGIN_WINDOW_S = 300
_login_attempts: dict[str, list[float]] = {}


def _sign(payload: str) -> str:
    return hmac.new(SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()


def create_session_cookie(username: str) -> str:
    expiry = int(time.time()) + SESSION_TTL_S
    payload = f"{username}:{expiry}"
    encoded = base64.urlsafe_b64encode(payload.encode()).decode()
    return f"{encoded}.{_sign(payload)}"


def verify_session_cookie(cookie_value: str | None) -> str | None:
    if not cookie_value or "." not in cookie_value:
        return None
    encoded, _, sig = cookie_value.partition(".")
    try:
        payload = base64.urlsafe_b64decode(encoded.encode()).decode()
    except Exception:  # noqa: BLE001
        return None
    if not hmac.compare_digest(sig, _sign(payload)):
        return None
    username, _, expiry = payload.partition(":")
    if not expiry.isdigit() or int(expiry) < time.time():
        return None
    return username


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _rate_limited(ip: str) -> bool:
    now = time.time()
    attempts = [t for t in _login_attempts.get(ip, []) if now - t < LOGIN_WINDOW_S]
    _login_attempts[ip] = attempts
    return len(attempts) >= LOGIN_MAX_ATTEMPTS


def _record_failed_attempt(ip: str) -> None:
    _login_attempts.setdefault(ip, []).append(time.time())


TABLES = {
    "metrics": "ts",
    "metrics_minutely": "ts_minute",
    "metrics_hourly": "ts_hour",
    "metrics_daily": "ts_day",
    "events": "ts",
    "speedtests": "ts",
    "weather": "ts",
    "dish_info": None,
}

DEFAULT_LIMIT = 5000
MAX_LIMIT = 50000


def check_auth(request: Request) -> str:
    username = verify_session_cookie(request.cookies.get(COOKIE_NAME))
    if username is None:
        raise HTTPException(status_code=401, detail="Not authenticated.")
    return username


async def _connect_with_retry(max_attempts: int = 15, delay_s: float = 2.0):
    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            db = await aiosqlite.connect(DB_PATH)
            await db.execute("PRAGMA busy_timeout=5000;")
            await db.execute("PRAGMA cache_size=-32000;")
            await db.execute("PRAGMA temp_store=MEMORY;")
            await db.execute("PRAGMA mmap_size=268435456;")
            if attempt > 1:
                logger.info("DB connection succeeded after %s attempts.", attempt)
            return db
        except aiosqlite.OperationalError as exc:
            last_exc = exc
            logger.warning(
                "DB not available yet (attempt %s/%s): %s - retrying in %ss...",
                attempt, max_attempts, exc, delay_s,
            )
            await asyncio.sleep(delay_s)
    raise RuntimeError(
        f"Could not open DB after {max_attempts} attempts ({DB_PATH})."
    ) from last_exc


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.db = await _connect_with_retry()
    app.state.db.row_factory = aiosqlite.Row
    app.state.broadcast_db = await _connect_with_retry()
    app.state.broadcast_db.row_factory = aiosqlite.Row
    app.state.ws_clients: set[WebSocket] = set()
    app.state.broadcaster_task = asyncio.create_task(broadcast_loop(app))
    yield
    app.state.broadcaster_task.cancel()
    await app.state.db.close()
    await app.state.broadcast_db.close()


app = FastAPI(
    title="Starlink Monitor API",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.middleware("http")
async def add_robots_header(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


def _require_table(table: str) -> str:
    if table not in TABLES:
        raise HTTPException(status_code=404, detail=f"Unbekannte Tabelle '{table}'.")
    return table


async def _valid_columns(db, table: str) -> set[str]:
    cols = set()
    async with db.execute(f"PRAGMA table_info({table})") as cur:
        async for row in cur:
            cols.add(row[1])
    return cols


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/robots.txt")
async def robots_txt():
    return PlainTextResponse("User-agent: *\nDisallow: /\n")


@app.get("/favicon.ico")
async def favicon():
    return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")


@app.get("/api/columns/{table}")
async def get_columns(table: str, request: Request, _user: str = Depends(check_auth)):
    _require_table(table)
    cols = sorted(await _valid_columns(request.app.state.db, table))
    return {"table": table, "columns": cols, "ts_col": TABLES[table]}


@app.get("/api/raw/{table}")
async def get_raw(
    table: str,
    request: Request,
    from_: int | None = Query(None, alias="from"),
    to: int | None = Query(None),
    limit: int = Query(DEFAULT_LIMIT, le=MAX_LIMIT, gt=0),
    order: str = Query("asc", pattern="^(asc|desc)$"),
    filter_col: str | None = Query(None),
    filter_val: str | None = Query(None),
    _user: str = Depends(check_auth),
):
    _require_table(table)
    db = request.app.state.db
    ts_col = TABLES[table]

    clauses, params = [], []
    if ts_col and from_ is not None:
        clauses.append(f"{ts_col} >= ?"); params.append(from_)
    if ts_col and to is not None:
        clauses.append(f"{ts_col} <= ?"); params.append(to)

    if filter_col is not None:
        valid_cols = await _valid_columns(db, table)
        if filter_col not in valid_cols:
            raise HTTPException(status_code=400, detail=f"Unbekannte Spalte '{filter_col}' in '{table}'.")
        clauses.append(f"{filter_col} = ?"); params.append(filter_val)

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    order_col = ts_col or "id"
    query = f"SELECT * FROM {table} {where} ORDER BY {order_col} {order.upper()} LIMIT ?"
    params.append(limit)

    async with db.execute(query, params) as cur:
        rows = await cur.fetchall()
    return {"table": table, "count": len(rows), "data": [dict(r) for r in rows]}


@app.get("/api/latest/{table}")
async def get_latest(table: str, request: Request, _user: str = Depends(check_auth)):
    _require_table(table)
    db = request.app.state.db
    ts_col = TABLES[table]
    order_col = ts_col or "id"
    async with db.execute(f"SELECT * FROM {table} ORDER BY {order_col} DESC LIMIT 1") as cur:
        row = await cur.fetchone()
    return dict(row) if row else {}


@app.websocket("/ws/live")
async def ws_live(websocket: WebSocket):
    username = verify_session_cookie(websocket.cookies.get(COOKIE_NAME))
    if username is None:
        await websocket.close(code=4401)
        return

    await websocket.accept()
    websocket.app.state.ws_clients.add(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        websocket.app.state.ws_clients.discard(websocket)


async def broadcast_loop(app: FastAPI):
    last_ts_sent = 0
    while True:
        try:
            async with app.state.broadcast_db.execute(
                "SELECT * FROM metrics ORDER BY ts DESC LIMIT 1"
            ) as cur:
                row = await cur.fetchone()
            if row and row["ts"] != last_ts_sent:
                last_ts_sent = row["ts"]
                payload = json.dumps(dict(row))
                dead = []
                for ws in list(app.state.ws_clients):
                    try:
                        await asyncio.wait_for(ws.send_text(payload), timeout=5.0)
                    except Exception:  # noqa: BLE001
                        dead.append(ws)
                for ws in dead:
                    app.state.ws_clients.discard(ws)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(2)


class DeleteRequest(pydantic.BaseModel):
    from_ts: int | None = None
    to_ts: int | None = None


@app.delete("/api/admin/{table}")
async def delete_table(
    table: str,
    request: Request,
    body: DeleteRequest,
    _user: str = Depends(check_auth),
):
    _require_table(table)
    if table == "dish_info":
        raise HTTPException(status_code=400, detail="dish_info cannot be deleted by time range.")
    db = request.app.state.db
    ts_col = TABLES[table]
    clauses, params = [], []
    if body.from_ts is not None:
        clauses.append(f"{ts_col} >= ?"); params.append(body.from_ts)
    if body.to_ts is not None:
        clauses.append(f"{ts_col} <= ?"); params.append(body.to_ts)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    cur = await db.execute(f"DELETE FROM {table} {where}", params)
    deleted = cur.rowcount
    await db.commit()
    return {"table": table, "deleted": deleted}


@app.get("/api/config")
async def public_config():
    return {"cookie_secure": COOKIE_SECURE, "totp_required": bool(TOTP_SECRET)}


class LoginRequest(pydantic.BaseModel):
    username: str
    password: str
    totp_code: str | None = None


@app.get("/login")
async def login_page():
    return FileResponse(STATIC_DIR / "login.html")


@app.post("/api/login")
async def login(body: LoginRequest, request: Request):
    ip = _client_ip(request)
    if _rate_limited(ip):
        raise HTTPException(status_code=429, detail="Too many failed attempts. Please wait a moment.")

    correct_user = secrets.compare_digest(body.username, ADMIN_USER)
    correct_pass = secrets.compare_digest(body.password, ADMIN_PASS)
    if not (correct_user and correct_pass):
        _record_failed_attempt(ip)
        raise HTTPException(status_code=401, detail="Incorrect username or password.")

    if TOTP_SECRET:
        code = (body.totp_code or "").strip()
        lan_pin_ok = (
            _lan_pin_fallback_active()
            and _client_in_lan(ip)
            and code
            and secrets.compare_digest(code.encode(), LAN_FALLBACK_PIN.encode())
        )
        if lan_pin_ok:
            logger.warning(
                "LAN PIN fallback used for user '%s' from %s", body.username, ip
            )
        else:
            if not code:
                raise HTTPException(status_code=400, detail="2FA code required.")
            if not pyotp.TOTP(TOTP_SECRET).verify(code, valid_window=1):
                _record_failed_attempt(ip)
                raise HTTPException(status_code=401, detail="Invalid 2FA code.")

    response = JSONResponse({"status": "ok"})
    response.set_cookie(
        COOKIE_NAME,
        create_session_cookie(body.username),
        max_age=SESSION_TTL_S,
        httponly=True,
        samesite="lax",
        secure=COOKIE_SECURE and _is_https_request(request),
        path="/",
    )
    return response


@app.post("/api/logout")
async def logout():
    response = JSONResponse({"status": "ok"})
    response.delete_cookie(COOKIE_NAME, path="/")
    return response


@app.get("/logout")
async def logout_redirect():
    response = RedirectResponse(url="/login")
    response.delete_cookie(COOKIE_NAME, path="/")
    return response


@app.get("/")
async def root(request: Request):
    username = verify_session_cookie(request.cookies.get(COOKIE_NAME))
    if username is None:
        return RedirectResponse(url="/login")
    return FileResponse(FRONTEND_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
