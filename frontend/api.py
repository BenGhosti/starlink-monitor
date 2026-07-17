"""
api.py
FastAPI-Backend des `frontend`-Containers - GENERISCHER DATEN-LAYER.

Design-Prinzip (siehe BACKEND_GUIDE.md): Das Backend kennt KEINE
Dashboard-spezifische Logik mehr (keine Range->Resolution-Aufloesung, keine
Aggregation, keine Statistik-Berechnung, kein CSV-Format). Es stellt nur noch
generische, sichere Lese-/Loesch-Zugriffe auf die SQLite-Tabellen bereit.
Die gesamte Anzeige- und Aufbereitungslogik lebt im Frontend
(static/api-client.js). Neue Charts/Auswertungen = nur JS aendern.

Endpunkte:
- GET  /api/raw/{table}      generische gefilterte Zeilen-Abfrage
- GET  /api/latest/{table}   juengste Zeile einer Tabelle
- GET  /api/columns/{table}  Spaltenliste einer Tabelle (fuer generische Clients)
- DELETE /api/admin/{table}  Zeilen in einem Zeitraum loeschen
- WS   /ws/live              pusht die neueste `metrics`-Zeile roh, 2s-Takt
- POST /api/login, /api/logout   Session-Cookie-Auth (siehe login.html)
- GET  /login                Login-Seite (unauthentifiziert erreichbar)
- /            index.html (leitet zu /login um, falls keine gueltige Session)
- /static/*    statische Dateien
"""

import asyncio
import base64
import hashlib
import hmac
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
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

logging.basicConfig(level=logging.INFO, format="%(asctime)s [api] %(levelname)s %(message)s")
logger = logging.getLogger("api")

DB_PATH = "/data/starlink.db"
STATIC_DIR = Path(__file__).parent / "static"

ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "changeme")

# Session-Auth ersetzt HTTP Basic Auth (siehe login.html/login.js). SESSION_SECRET
# sollte in .env gesetzt sein; ohne expliziten Wert wird ein Fallback aus den
# Admin-Zugangsdaten abgeleitet, damit Sessions wenigstens einen Prozess-Neustart
# ueberleben (Warnung im Log, da weniger sicher als ein eigener zufaelliger Secret).
SESSION_SECRET = os.environ.get("SESSION_SECRET")
if not SESSION_SECRET:
    logger.warning("SESSION_SECRET nicht gesetzt - leite Fallback-Secret ab. Fuer Produktion in .env setzen!")
    SESSION_SECRET = hashlib.sha256(f"{ADMIN_USER}:{ADMIN_PASS}:starlink-monitor".encode()).hexdigest()

COOKIE_NAME = "sm_session"
SESSION_TTL_S = 12 * 3600  # 12h, danach erneut einloggen
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "false").lower() == "true"

# 2FA (TOTP, RFC 6238) ist optional und rein ueber .env gesteuert - kein
# DB-Schema noetig, daher unabhaengig vom sonstigen DB-Umbau nachruestbar,
# ohne bestehende Daten anzufassen. Ist TOTP_SECRET gesetzt, verlangt
# /api/login zusaetzlich zu Benutzername/Passwort einen 6-stelligen Code aus
# einer Authenticator-App (Google Authenticator, Aegis, 1Password, ...).
# Secret generieren: scripts/generate_2fa_secret.py
TOTP_SECRET = os.environ.get("TOTP_SECRET", "").strip()

# Sehr einfache In-Memory-Rate-Limitierung fuer /api/login (kein Redis noetig
# fuer eine Single-Instance-App). Bewusst simpel: IP -> Liste von Fehlversuch-
# Zeitstempeln der letzten LOGIN_WINDOW_S Sekunden.
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


# Allowlist: einzige Stelle, die neue Tabellen kennen muss. ts_col = Name der
# Zeitstempel-Spalte (fuer from/to-Filter + Sortierung). Frontend fragt bei
# Bedarf ueber /api/columns/{table} die volle Spaltenliste ab.
TABLES = {
    "metrics": "ts",
    "metrics_minutely": "ts_minute",
    "metrics_hourly": "ts_hour",
    "metrics_daily": "ts_day",
    "events": "ts",
    "speedtests": "ts",
    "weather": "ts",
    "dish_info": None,  # Singleton-Tabelle (id=1), kein Zeitfenster
}

DEFAULT_LIMIT = 5000
MAX_LIMIT = 50000


def check_auth(request: Request) -> str:
    """Session-Cookie-Auth fuer /api/*-Endpunkte. Liefert 401 JSON statt einer
    Browser-Login-Box - api-client.js faengt das ab und leitet zu /login um."""
    username = verify_session_cookie(request.cookies.get(COOKIE_NAME))
    if username is None:
        raise HTTPException(status_code=401, detail="Nicht angemeldet.")
    return username


async def _connect_with_retry(max_attempts: int = 15, delay_s: float = 2.0):
    """Verbindet mit der SQLite-DB, mit Retry (collector legt Schema evtl. erst spaeter an)."""
    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            db = await aiosqlite.connect(DB_PATH)
            # Nur Verbindungs-lokale Pragmas (kein journal_mode/synchronous - die
            # sind DB-weit bereits vom Collector gesetzt und die DB ist hier :ro
            # gemountet). busy_timeout verhindert sofortige "database is locked"-
            # Fehler bei Overlap mit dem staendlichen Kompressions-Job.
            await db.execute("PRAGMA busy_timeout=5000;")
            await db.execute("PRAGMA cache_size=-32000;")
            await db.execute("PRAGMA temp_store=MEMORY;")
            await db.execute("PRAGMA mmap_size=268435456;")
            if attempt > 1:
                logger.info("DB-Verbindung erfolgreich nach %s Versuchen.", attempt)
            return db
        except aiosqlite.OperationalError as exc:
            last_exc = exc
            logger.warning(
                "DB noch nicht verfuegbar (Versuch %s/%s): %s - warte %ss...",
                attempt, max_attempts, exc, delay_s,
            )
            await asyncio.sleep(delay_s)
    raise RuntimeError(
        f"Konnte DB nach {max_attempts} Versuchen nicht oeffnen ({DB_PATH})."
    ) from last_exc


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.db = await _connect_with_retry()
    app.state.db.row_factory = aiosqlite.Row
    app.state.ws_clients: set[WebSocket] = set()
    app.state.broadcaster_task = asyncio.create_task(broadcast_loop(app))
    yield
    app.state.broadcaster_task.cancel()
    await app.state.db.close()


app = FastAPI(title="Starlink Monitor API", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Generische Lese-Endpunkte
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/api/columns/{table}")
async def get_columns(table: str, request: Request, _user: str = Depends(check_auth)):
    """Spaltenliste einer Tabelle - erlaubt generischen Clients, sich selbst
    an das Schema anzupassen, ohne dass das Backend dafuer Wissen braucht."""
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
    """Generische, gefilterte Zeilen-Abfrage einer Tabelle.

    - from/to: Zeitfenster ueber die Zeitspalte der Tabelle (siehe TABLES).
    - filter_col/filter_val: optionaler Gleichheitsfilter auf eine beliebige
      *tatsaechlich existierende* Spalte (z.B. filter_col=type&filter_val=disconnect
      fuer events). filter_col wird gegen PRAGMA table_info geprueft, bevor er
      in SQL interpoliert wird - kein Injection-Risiko, da nur echte Spalten-
      namen der Tabelle akzeptiert werden.
    """
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
    """Juengste Zeile einer Tabelle (nach Zeitspalte bzw. id fuer Singletons)."""
    _require_table(table)
    db = request.app.state.db
    ts_col = TABLES[table]
    order_col = ts_col or "id"
    async with db.execute(f"SELECT * FROM {table} ORDER BY {order_col} DESC LIMIT 1") as cur:
        row = await cur.fetchone()
    return dict(row) if row else {}


# ---------------------------------------------------------------------------
# WebSocket live feed - pusht die neueste `metrics`-Zeile roh, unveraendert
# ---------------------------------------------------------------------------

@app.websocket("/ws/live")
async def ws_live(websocket: WebSocket):
    # Cookies werden vom Browser beim WS-Handshake automatisch mitgeschickt
    # (gleiche Origin) - kein manuelles Header-Handling wie frueher bei Basic Auth noetig.
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
    """Pollt alle 2s den neuesten metrics-Datensatz und pusht ihn roh an alle WS-Clients."""
    last_ts_sent = 0
    while True:
        try:
            async with app.state.db.execute(
                "SELECT * FROM metrics ORDER BY ts DESC LIMIT 1"
            ) as cur:
                row = await cur.fetchone()
            if row and row["ts"] != last_ts_sent:
                last_ts_sent = row["ts"]
                payload = json.dumps(dict(row))
                dead = []
                for ws in list(app.state.ws_clients):
                    try:
                        await ws.send_text(payload)
                    except Exception:  # noqa: BLE001
                        dead.append(ws)
                for ws in dead:
                    app.state.ws_clients.discard(ws)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(2)


# ---------------------------------------------------------------------------
# Admin API - Datenbereinigung (DELETE, geschuetzt durch Session-Auth)
# ---------------------------------------------------------------------------

class DeleteRequest(pydantic.BaseModel):
    """Zeitraum fuer DELETE-Operationen. from_ts/to_ts optional (Unix-Timestamps).
    Wird keins angegeben, werden alle Zeilen der Tabelle geloescht."""
    from_ts: int | None = None
    to_ts: int | None = None


@app.delete("/api/admin/{table}")
async def delete_table(
    table: str,
    request: Request,
    body: DeleteRequest,
    _user: str = Depends(check_auth),
):
    """Loescht Zeilen einer einzelnen Tabelle in einem Zeitraum (oder alle).
    Fuer zusammenhaengende Loeschungen (z.B. metrics + metrics_minutely
    gemeinsam leeren) ruft das Frontend diesen Endpunkt mehrfach auf - das
    ist eine Anzeige-/Bedienlogik-Entscheidung, keine Backend-Aufgabe."""
    _require_table(table)
    if table == "dish_info":
        raise HTTPException(status_code=400, detail="dish_info kann nicht per Zeitraum geloescht werden.")
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


# ---------------------------------------------------------------------------
# Login / Logout
# ---------------------------------------------------------------------------

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
        raise HTTPException(status_code=429, detail="Zu viele Fehlversuche. Bitte kurz warten.")

    correct_user = secrets.compare_digest(body.username, ADMIN_USER)
    correct_pass = secrets.compare_digest(body.password, ADMIN_PASS)
    if not (correct_user and correct_pass):
        _record_failed_attempt(ip)
        raise HTTPException(status_code=401, detail="Benutzername oder Passwort falsch.")

    if TOTP_SECRET:
        if not body.totp_code:
            raise HTTPException(status_code=400, detail="2FA-Code erforderlich.")
        if not pyotp.TOTP(TOTP_SECRET).verify(body.totp_code.strip(), valid_window=1):
            _record_failed_attempt(ip)
            raise HTTPException(status_code=401, detail="2FA-Code ungueltig.")

    response = JSONResponse({"status": "ok"})
    response.set_cookie(
        COOKIE_NAME,
        create_session_cookie(body.username),
        max_age=SESSION_TTL_S,
        httponly=True,
        samesite="lax",
        secure=COOKIE_SECURE,
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
    """Bequemer Direktaufruf per Browser-URL (z.B. Lesezeichen), ohne dass
    JS eine POST-Anfrage bauen muss."""
    response = RedirectResponse(url="/login")
    response.delete_cookie(COOKIE_NAME, path="/")
    return response


# ---------------------------------------------------------------------------
# Static files (index.html, dashboard.js, style.css)
# ---------------------------------------------------------------------------

@app.get("/")
async def root(request: Request):
    username = verify_session_cookie(request.cookies.get(COOKIE_NAME))
    if username is None:
        return RedirectResponse(url="/login")
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
