# Starlink Monitor – Backend-Guide (für KI-Agenten)

## Architekturprinzip
`frontend/api.py` ist ein **dummer, generischer Datenzugriffs-Layer**. Er kennt
keine Dashboard-Logik (keine Range→Auflösung, keine Aggregation, keine
Statistik, kein CSV-Format). Diese Logik lebt komplett clientseitig in
`frontend/static/api-client.js`.

**Regel: Neue Charts, Kennzahlen, Filter oder Formate = nur JS ändern
(`api-client.js` / `dashboard.js`). `api.py` nur anfassen, wenn eine neue
Tabelle/Spalte im Schema (`collector/db.py`) dazukommt.**

## Backend-Endpunkte (frontend/api.py)
Alle Endpunkte außer `/health`, `/login`, `/api/login` und `/static/*` sind per
Session-Cookie geschützt (`sm_session`, HMAC-signiert mit `SESSION_SECRET`,
12h Gültigkeit). Anmeldung läuft über die grafische Login-Seite (`/login`,
`static/login.html` + `login.css` + `login.js`) statt über den Browser-eigenen
HTTP-Basic-Dialog. `/` leitet ohne gültige Session per 307 zu `/login` um;
`/api/*`-Aufrufe liefern stattdessen `401 JSON`, was `api-client.js`
(`_redirectToLoginOn401`) abfängt und ebenfalls zu `/login` weiterleitet.
Abmelden: `POST /api/logout` (JS) oder Browser-Aufruf von `/logout`.

| Endpunkt | Zweck |
|---|---|
| `GET /api/raw/{table}` | Generische gefilterte Zeilenabfrage. Query-Params: `from`, `to` (Unix-TS, über die Zeitspalte der Tabelle), `limit` (default 5000, max 50000), `order` (`asc`/`desc`), `filter_col`+`filter_val` (Gleichheitsfilter auf eine beliebige *echte* Spalte, wird gegen `PRAGMA table_info` geprüft). Antwort: `{table, count, data: [...]}` |
| `GET /api/latest/{table}` | Jüngste Zeile (`ORDER BY <ts_col> DESC LIMIT 1`) |
| `GET /api/columns/{table}` | Spaltenliste + Name der Zeitspalte |
| `DELETE /api/admin/{table}` | Löscht Zeilen im Zeitraum `{from_ts, to_ts}` (Body). `dish_info` ist gesperrt (Singleton) |
| `POST /api/login` | `{username, password}` → prüft gegen `ADMIN_USER`/`ADMIN_PASS`, setzt `sm_session`-Cookie. Rate-limitiert (5 Fehlversuche / 5min / IP, In-Memory). |
| `POST /api/logout` / `GET /logout` | Löscht das Session-Cookie |
| `GET /login` | Liefert die grafische Login-Seite (kein Auth nötig) |
| `GET /ws/live` | WebSocket, pusht alle 2s die neueste `metrics`-Zeile roh (unverändert, kein Vor-Processing). Auth über dasselbe Session-Cookie (Browser schickt es beim Handshake automatisch mit) |
| `GET /` | liefert `index.html`, sonst Redirect zu `/login` |

**Tabellen-Allowlist** (`TABLES` dict in `api.py`): `metrics`, `metrics_minutely`,
`metrics_hourly`, `metrics_daily`, `events`, `speedtests`, `weather`, `dish_info`.
Jede Tabelle hat dort ihre Zeitspalte hinterlegt (`ts`, `ts_minute`, `ts_hour`,
`ts_day`, oder `None` für die Singleton-Tabelle `dish_info`).

Es gibt **keine** Endpunkte mehr wie `/api/metrics?range=...`,
`/api/stats/summary`, `/api/dish/status`, `/api/weather/current`,
`/api/export/csv` — das war früher Backend-Logik und ist jetzt Teil von
`api-client.js`.

## Frontend-Daten-Layer (frontend/static/api-client.js)
Muss **vor** `dashboard.js` in `index.html` geladen werden. Stellt die
High-Level-Funktionen bereit, die `dashboard.js` aufruft:

- `resolveRange(rangeKey)` — mappt `1d/7d/14d/1m/6m/12m/all` auf `[from, to, resolution_s]`. **Feste Ziel-Punktanzahl:** `resolution_s = max(minResolution, ceil((to-from)/TARGET_POINTS))`, TARGET_POINTS=300. Jeder Zeitraum liefert dadurch ~300 Chart-Punkte, egal wie lang die Spanne ist — nur die Aufloesung (Sekunden/Punkt) skaliert mit. `minResolution` ist pro Aufrufer parametrisierbar (Metriken: 2s Rohdaten-Takt, Speedtests: 8h Poll-Intervall) — kürzere Zeiträume mit wenig echten Datenpunkten werden nicht künstlich verfeinert. Für `all` wird die älteste vorhandene Zeile (`metrics_daily`→`metrics_hourly`→`metrics_minutely`→`metrics`) per `getEarliestMetricsTs()` erfragt und pro Seitenladung gecacht.
- `pickTimeAxisUnit(from, to)` — wählt die Chart.js-Zeitachsen-Einheit (Minute/Stunde/Tag/Monat/Jahr) anhand der tatsächlichen Spanne, nicht anhand der Range-Taste — skaliert also automatisch mit dem gewählten Zeitraum.
- `fetchMetrics(range)` — holt Rohdaten aus `metrics`/`metrics_minutely`/`metrics_hourly`/`metrics_daily` je nach Zielauflösung und aggregiert selbst (Kaskade: `metrics_hourly` für Daten älter als die älteste `metrics_minutely`-Zeile, `metrics_daily` für Daten älter als die älteste `metrics_hourly`-Zeile). Nutzt `fetchAllRaw()` zum seitenweisen Durchpaginieren, falls ein Zeitfenster mehr Zeilen als `MAX_LIMIT` enthält.
- `fetchEvents(range, type?)`, `fetchSpeedtests(range)` (gleiche Ziel-Punktanzahl-Logik, Minimum = 8h-Poll-Intervall), `fetchDishStatus()`, `fetchWeatherCurrent()`, `fetchWeatherWarnings()`, `fetchStatsSummary()` (24h-Kennzahlen, clientseitig aus `metrics`+`events` berechnet)
- `downloadCsv(range)` — baut die CSV im Browser (Blob) statt Server-Stream
- `adminDelete(target, from_ts, to_ts)` — `target='metrics'` löscht `metrics`+`metrics_minutely` zusammen, `target='all'` loopt über alle Tabellen

**Wichtig für Agenten:** `resolveRange` und `fetchSpeedtests` sind `async` (fragen bei `all` ggf. den ältesten Timestamp nach) — immer `await`en, auch wenn es beim schnellen Überfliegen wie eine reine Berechnung aussieht.

- `fetchPeakStats(range)` — Bestwerte (niedrigste Latenz, höchster Down-/Upload) aus den 2s-Live-Abtastungen. Nimmt einfach das Extremum über die min/max-Felder, die `fetchMetrics()` ohnehin schon pro Bucket mitliefert — kein zusätzlicher Rohdaten-Fetch nötig. **Schema-Limitierung:** `metrics_hourly` (Fallback für Daten älter als die älteste `metrics_minutely`-Zeile, aktuell 730 Tage Retention) hat nur `max_ping_latency_ms`, kein `min_ping_latency_ms`, `max_downlink_bps` oder `max_uplink_bps` — Peak-Werte aus Daten jenseits der Minutely-Retention sind daher unvollständig. Das ist eine Grenze der Collector-Aggregation (`collector/db.py`/`cleanup.py`), keine Frontend-Einschränkung.
- Zeitraum-Presets: `1d/7d/14d/1m/3m/6m/12m/all` (`3m` = 90 Tage, nur für die Peak-Kachel in der UI verdrahtet, aber generisch nutzbar).

## Traffic / Datenvolumen (down_bytes, up_bytes)
`metrics_minutely`, `metrics_hourly`, `metrics_daily` führen zusätzlich zu den
avg/min/max-bps-Feldern `down_bytes`/`up_bytes` — das tatsächlich übertragene
Datenvolumen des Buckets in Byte (`SUM(bps) * RAW_SAMPLE_INTERVAL_S / 8` beim
erstmaligen Aggregieren aus Rohdaten; bei der Hourly→Daily-Verdichtung exakt
`SUM(down_bytes)` der Stunden statt einer Naeherung über den Durchschnitt).
`frontend/static/api-client.js` → `fetchTraffic(range)` summiert diese Spalten
in gröberen Buckets (stündlich bei `1d`, sonst täglich/wöchentlich/monatlich,
siehe `TRAFFIC_BUCKET_S`) über dieselbe minutely→hourly→daily-Kaskade wie
`fetchMetrics()` und liefert `{data: [{ts, down_gb, up_gb}], totals}` in GB.
`dashboard.js` rendert das als Balkendiagramm (`trafficChart`) unterhalb der
Speedtest-Sektion, mit denselben `1d/7d/14d/1m/6m/12m/all`-Range-Buttons wie
die übrigen Charts.

## Collector: Datenbank-Kompression (collector/db.py, collector/cleanup.py)
Drei-Stufen-Kompression für langfristigen Betrieb (Jahre statt Monate):

1. **Rohdaten** (`metrics`, 2s-Takt) — `RETENTION_DAYS` (90) Tage, danach von
   `compress_old_metrics()` pro Stunde zu `metrics_hourly` verdichtet und gelöscht.
2. **`metrics_hourly`** — `HOURLY_ROLLUP_DAYS` (Env-Var, Default 365) Tage,
   danach von `compress_old_hourly()` pro Kalendertag zu `metrics_daily`
   verdichtet (gewichteter Durchschnitt über `sample_count`) und gelöscht.
3. **`metrics_daily`** — wird nicht weiter komprimiert oder gelöscht (Volumen
   nach zwei Rollup-Stufen bereits minimal, ca. 1 Zeile/Tag).

`metrics_minutely` läuft parallel dazu mit eigener, längerer Retention
(`MINUTELY_RETENTION_DAYS`, Default 730 Tage) und wird danach ersatzlos
gelöscht statt weiter komprimiert — `metrics_hourly`/`metrics_daily` decken
diesen Zeitraum bereits ab. Nach jedem Kompressionslauf setzt `cleanup.py`
zusätzlich `PRAGMA optimize` (aktualisiert Query-Planner-Statistiken, kein
Tabellen-Rewrite). Verbindungs-Pragmas (`cache_size`, `mmap_size`,
`busy_timeout`, `wal_autocheckpoint`) sind zentral in `db.py` (`PRAGMAS`)
gepflegt und werden von Collector und Frontend gleichermaßen genutzt.

## Collector: Speedtest-Zeitplan & Messdauer (collector/speedtest_runner.py)
- Läuft **nicht** mehr im 8h-Intervall ab Programmstart, sondern zu festen Uhrzeiten **00:00, 08:00, 16:00 Europe/Berlin** (`SCHEDULE_HOURS_BERLIN`), DST-sicher via `zoneinfo`. `tzdata`-Pip-Paket in `requirements.txt` ergänzt, falls das Docker-Base-Image keine System-Tzdata hat.
- **Sustained-Messung:** `speedtest-cli` lädt pro Aufruf nur ein festes Datenvolumen (für „normale" Leitungen auf ~10s Dauer dimensioniert) — bei schnellen Leitungen (Starlink) ist das in 2-3s durchgeladen, was zu kurzen/ungenauen Werten führt. `_sustained_measure()` wiederholt Download/Upload-Runden, bis insgesamt mind. `MIN_TEST_DURATION_S` (Default 10s, Env-Var `SPEEDTEST_MIN_DURATION_S`) vergangen sind, und mittelt über die Summe aller Runden — ähnlich dem Verhalten des offiziellen Ookla-Clients bei schnellen Verbindungen.

## Auth (frontend/api.py, static/login.*)
Session-Cookie statt HTTP Basic (siehe Endpunkt-Tabelle oben). `SESSION_SECRET`
in `.env` setzen (32+ Byte zufällig, z.B. `python3 -c "import secrets;
print(secrets.token_hex(32))"`) — ohne expliziten Wert wird ein schwächerer
Fallback aus `ADMIN_USER`/`ADMIN_PASS` abgeleitet (Log-Warnung). `COOKIE_SECURE=true`
setzen, sobald die App hinter HTTPS (Reverse Proxy) läuft. Login-Rate-Limit ist
In-Memory (pro Prozess) — bei mehreren Frontend-Replicas nicht geteilt, für den
Ein-Container-Betrieb dieser App aber ausreichend.

**Optionale 2FA (TOTP, RFC 6238):** `TOTP_SECRET` in `.env` setzen (Generator:
`scripts/generate_2fa_secret.py`, lokal ausführen, gibt `.env`-Zeile +
otpauth-URI + ASCII-QR aus). Ist der Wert gesetzt, verlangt `/api/login`
zusätzlich zu Benutzername/Passwort einen `totp_code` (6-stellig, `pyotp`,
±1 Zeitfenster Toleranz für Uhr-Drift). Rein `.env`-basiert, **kein
DB-Schema betroffen** — unabhängig vom übrigen DB-Umbau jederzeit
nachrüstbar oder wieder deaktivierbar, ohne bestehende Daten anzufassen.

## Typischer Änderungs-Workflow
- **Neue Kennzahl/neuer Chart anzeigen** → nur `api-client.js` (neue
  Fetch-/Aggregationsfunktion) + `dashboard.js` (Chart/Rendering) anfassen.
- **Neue Spalte im Schema nutzen** → falls Tabelle schon in `TABLES`
  gelistet ist, reicht `/api/raw/{table}` bereits (liefert alle Spalten roh) —
  kein Backend-Change nötig, nur `api-client.js` muss das Feld mappen.
- **Neue Tabelle** → in `collector/db.py` Schema ergänzen, dann in `api.py`
  einen Eintrag zu `TABLES` hinzufügen (Tabellenname → Zeitspalte). Das ist
  der einzige Fall, der Backend-Änderungen braucht.
