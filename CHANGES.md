# Starlink Monitor — Backend-Umbau (Datenbank + Auth)

## Projektüberblick

Starlink Monitor ist ein selbst-gehosteter Monitoring-Stack für eine
Starlink-Anlage, bestehend aus zwei Docker-Containern:

- **collector**: pollt die Dish per gRPC (`starlink_grpc.py`), schreibt alle
  2s Metriken in SQLite, führt Ping-Watchdog, Wetter-Polling, Speedtests
  (alle 8h) und Discord-Alerting aus. Aggregiert außerdem laufend Minuten-
  Buckets und komprimiert alte Daten (`cleanup.py`).
- **frontend**: FastAPI-Backend (`api.py`) als generischer, dashboard-freier
  Datenzugriffs-Layer + statisches Dashboard (`index.html`/`dashboard.js`/
  `api-client.js`), das die gesamte Anzeige-/Aggregationslogik clientseitig
  übernimmt (siehe `BACKEND_GUIDE.md`).

Beide teilen sich `/data/starlink.db` (SQLite, WAL-Modus), collector
schreibend, frontend nur lesend gemountet.

## Was geändert wurde

### 1. Datenbank-Backend: dreistufige Kompression für Langzeitbetrieb
Bisher gab es zwei Stufen (Rohdaten 2s → `metrics_hourly` nach 90 Tagen).
Bei mehrjährigem Betrieb wurde `metrics_hourly` selbst beliebig groß.
Jetzt gibt es eine dritte Stufe:

| Stufe | Tabelle | Auflösung | Retention |
|---|---|---|---|
| 1 | `metrics` | 2s (roh) | 90 Tage, dann → Stufe 2 |
| 2 | `metrics_hourly` | 1h | 365 Tage (`HOURLY_ROLLUP_DAYS`), dann → Stufe 3 |
| 3 | `metrics_daily` | 1 Tag | unbegrenzt |
| parallel | `metrics_minutely` | 1min | 730 Tage (`MINUTELY_RETENTION_DAYS`), danach gelöscht (Stufe 2/3 decken den Zeitraum ab) |

`cleanup.py` führt die neue Stufe 2→3-Verdichtung (`compress_old_hourly()`)
im selben täglichen Job aus wie die bestehende Stufe 1→2-Verdichtung, inkl.
korrekter gewichteter Mittelwertbildung über `sample_count`. Nach jedem Lauf
wird zusätzlich `PRAGMA optimize` ausgeführt (aktualisiert Query-Planner-
Statistiken, kein teures `VACUUM`).

**Ergebnis:** Bei 3 Jahren Betrieb wären es vorher ~26.000 `metrics_hourly`-
Zeilen (unbegrenzt wachsend); jetzt nach 1 Jahr automatisch auf ~365
`metrics_daily`-Zeilen/Jahr verdichtet — die DB bleibt dauerhaft klein und
schnell abfragbar, ohne dass alte Verlaufsdaten verloren gehen.

### 2. Tuned SQLite-Pragmas
Zentral in `collector/db.py` (`PRAGMAS`), von Collector *und* Frontend
genutzt: `busy_timeout=5000` (kein sofortiger "database is locked"-Fehler
bei Schreib-/Lese-Überschneidung), `cache_size`/`mmap_size` (heißer
Datenbereich im RAM), `temp_store=MEMORY`, `wal_autocheckpoint=1000`.
Frontend nutzt zusätzlich einen abgespeckten Satz rein verbindungslokaler
Pragmas (kein `journal_mode`/`synchronous`, da `:ro`-Mount).

### 3. Neue Tabelle `metrics_daily` im Schema & API
`collector/db.py` (Schema + Index), `frontend/api.py` (`TABLES`-Allowlist),
`frontend/static/api-client.js` (Fallback-Kaskade in `fetchMetrics()` und
`getEarliestMetricsTs()` um die dritte Stufe erweitert, sonst hätten Charts
für sehr alte Zeiträume nach dem ersten Rollup-Lauf Lücken gezeigt).

### 4. Login: HTTP Basic Auth → Session-Cookie + grafische Login-Seite
Vorher: Browser-eigener Basic-Auth-Dialog auf jedem Endpunkt. Jetzt:

- `frontend/static/login.html` + `login.css` + `login.js` — eigenständige,
  zum Dashboard passende Login-Seite (gleiches Dark-Theme, JetBrains Mono,
  Farbpalette, animierter Dish-Ring-Hintergrund), ohne dass Layout/Design
  von `index.html`/`style.css`/`dashboard.js` angefasst wurde.
- `frontend/api.py`: `POST /api/login` prüft `ADMIN_USER`/`ADMIN_PASS` und
  setzt ein HMAC-signiertes, `HttpOnly`-Session-Cookie (`sm_session`,
  12h gültig). `POST /api/logout` bzw. `GET /logout` löschen es wieder.
  Alle bisherigen `Depends(security)`-Stellen (Basic Auth) wurden durch
  `Depends(check_auth)` auf Cookie-Basis ersetzt; `GET /` leitet ohne
  gültige Session per Redirect zu `/login` um (statt Basic-Auth-Popup).
  Der WebSocket-Handshake (`/ws/live`) prüft jetzt dasselbe Cookie statt
  eines manuell geparsten `Authorization`-Headers.
- Einfache In-Memory-Rate-Limitierung (5 Fehlversuche / 5 Minuten / IP) gegen
  Brute-Force auf `/api/login`.
- `SESSION_SECRET` (neuer Env-Var, siehe `.env`) signiert die Cookies;
  `COOKIE_SECURE` steuert das `Secure`-Flag für HTTPS-Betrieb hinter einem
  Reverse Proxy.
- `frontend/static/api-client.js`: alle drei bestehenden Fetch-Wrapper
  (`apiRaw`, `apiLatest`, `apiDeleteTable`) leiten bei `401` automatisch zu
  `/login` weiter (Session während offener Dashboard-Seite abgelaufen) —
  einzige Änderung an bestehender Anzeigelogik, keine neue UI.

### Nicht verändert
- `index.html`, `style.css`, `dashboard.js` (Design/Layout unangetastet,
  wie gewünscht) — abgesehen von der rein internen 401-Weiterleitung in
  `api-client.js` gibt es keine funktionalen Änderungen an bestehenden
  Charts/Kacheln.
- Der generische Anzeige-freie Charakter von `api.py` (`BACKEND_GUIDE.md`-
  Prinzip) bleibt erhalten — es kam nur eine Tabelle + Auth-Mechanismus
  dazu, keine Dashboard-Logik ins Backend zurückgewandert.
- Alle anderen Collector-Module (`metrics_collector.py`, `ping_watchdog.py`,
  `weather_poller.py`, `speedtest_runner.py`, `discord_alert.py`,
  `metrics_minutely_aggregator.py`) unverändert.

## Migration / Rollout-Hinweise
- Beim ersten Start nach dem Update legt `init_db()` automatisch Tabelle
  `metrics_daily` + Index an (idempotent, `CREATE TABLE IF NOT EXISTS`).
  Kein manueller Migrationsschritt nötig.
- `.env` wurde um `SESSION_SECRET` (zufällig generiert) und `COOKIE_SECURE`
  ergänzt, außerdem `MINUTELY_RETENTION_DAYS`/`HOURLY_ROLLUP_DAYS` explizit
  sichtbar gemacht (vorher nur Code-Defaults). Vor dem Deploy prüfen, ob
  `SESSION_SECRET` wirklich das erwartete zufällige Secret ist, und bei
  Reverse-Proxy-Betrieb hinter HTTPS `COOKIE_SECURE=true` setzen.
- Bestehende Browser-Basic-Auth-Anmeldungen greifen nach dem Update nicht
  mehr — einmalig über `/login` neu anmelden.
- `data/starlink.db-shm`/`-wal` (Runtime-Artefakte, ohnehin in `.gitignore`)
  wurden aus dem ausgelieferten Projektstand entfernt.

## Nachtrag: Traffic-Anzeige (Datenvolumen Down/Up)

Zusätzlich zur ersten Umbau-Runde wurde eine Traffic-Auswertung ergänzt:
wie viel GB über Starlink insgesamt gesendet/empfangen wurden, mit denselben
Zeiträumen wie die übrigen Charts (1d/7d/14d/1m/6m/12m/all) — als Balken-
diagramm direkt unter dem Speedtest-Panel.

**Backend** (`collector/db.py`, `collector/metrics_minutely_aggregator.py`,
`collector/cleanup.py`): Starlink liefert keinen eigenen "Bytes verbraucht"-
Zähler über die normale Status-Abfrage, aber `metrics.downlink_bps`/
`uplink_bps` (Momentanwert, alle 2s gepollt) reicht für eine exakte
Volumen-Berechnung: `Bytes = bps · 2s / 8`. Damit das nicht bei jeder
Chart-Anfrage aus Millionen Rohzeilen neu aufsummiert werden muss, wird das
jetzt bei der bereits bestehenden Aggregation direkt mitgeschrieben:

- Neue Spalten `down_bytes`/`up_bytes` in `metrics_minutely`, `metrics_hourly`,
  `metrics_daily` (Migration über bestehenden `EXPECTED_COLUMNS`-Mechanismus,
  kein manueller Schritt nötig).
- `metrics_minutely_aggregator.py`: `SUM(downlink_bps)`/`SUM(uplink_bps)` pro
  Minute zusätzlich zu den bestehenden AVG/MIN/MAX abgefragt und in Bytes
  umgerechnet gespeichert.
- `cleanup.py` (`compress_old_metrics`, Rohdaten→Stunde): dieselbe Umrechnung
  beim Verdichten alter Rohdaten.
- `cleanup.py` (`compress_old_hourly`, Stunde→Tag): hier wird `down_bytes`/
  `up_bytes` nicht erneut geschätzt, sondern exakt aus `SUM(down_bytes)` der
  zugrunde liegenden Stunden übernommen — verlustfrei über beide
  Rollup-Stufen hinweg.

Getestet: 30 künstliche 2s-Samples à 100 Mbit/s down / 20 Mbit/s up ergaben
exakt die erwarteten 750.000.000 / 150.000.000 Bytes in `metrics_minutely`;
ein 48h-Hourly→Daily-Rollup summierte die Bytes ebenso exakt weiter.

**Frontend** (`frontend/static/api-client.js`, `dashboard.js`, `index.html`):
- `fetchTraffic(range)` — neue Funktion, nutzt für `1d` die Rohdaten
  (`metrics`, Bytes pro Zeile live berechnet) und für alle anderen Ranges
  dieselbe minutely→hourly→daily-Kaskade wie `fetchMetrics()`, summiert aber
  Bytes statt Werte zu mitteln. Bucket-Breite ist an die Lesbarkeit als
  Balken angepasst (stündlich bei 1d, sonst täglich/wöchentlich/monatlich je
  nach Zeitraum), nicht an die 300-Punkte-Logik der Liniencharts.
- `dashboard.js`: neuer `trafficChart` (Chart.js `bar`, zwei Datasets
  Download/Upload GB), `loadTraffic()`, Einbindung in Range-Buttons, Init-
  Ladevorgang und Admin-Panel-Refresh nach Datenlöschung.
- `index.html`: neues Panel „Datenvolumen (gesendet/empfangen)" direkt unter
  dem Speedtest-Panel, exakt im bestehenden Panel-/Range-Button-Markup-Muster
  (kein neues CSS nötig, nur bestehende Klassen wiederverwendet).

Kein neuer Backend-Endpunkt: `down_bytes`/`up_bytes` sind einfach zusätzliche
Spalten in bereits über `/api/raw/{table}` generisch zugänglichen Tabellen —
passend zum bestehenden Architekturprinzip aus `BACKEND_GUIDE.md`.

## Nachtrag: Optionale 2FA (TOTP)

Zusätzliche Absicherung des Logins per Einmalcode (Google Authenticator,
Aegis, 1Password, ...), rein `.env`-gesteuert — **keine DB-Änderung, keine
Migration nötig**, unabhängig vom übrigen DB-Umbau jederzeit an-/abschaltbar.

- Neue Dependency `pyotp` (`frontend/requirements.txt`).
- `frontend/api.py`: `TOTP_SECRET` aus `.env` gelesen. Ist der Wert gesetzt,
  verlangt `POST /api/login` zusätzlich zu `username`/`password` ein Feld
  `totp_code` (6-stellig) und verifiziert es mit `pyotp.TOTP(...).verify(code,
  valid_window=1)` (±30s Toleranz für Uhr-Drift). Fehlt der Code komplett →
  `400` ("2FA-Code erforderlich"), falscher Code → `401` (zählt zusätzlich
  gegen das bestehende Rate-Limit). Ist `TOTP_SECRET` leer/nicht gesetzt,
  verhält sich der Login exakt wie vorher (2FA ist standardmäßig aus).
- `frontend/static/login.html`/`login.js`: drittes Formularfeld "2FA-Code
  (falls aktiviert)", wird bei jedem Login-Versuch mitgeschickt (leer, wenn
  2FA nicht genutzt wird — kostet nichts, da serverseitig ignoriert).
- `scripts/generate_2fa_secret.py`: einmalig lokal ausführen (nicht im
  Container), erzeugt einen zufälligen Secret + gibt die fertige
  `.env`-Zeile, die otpauth-URI und einen scanbaren ASCII-QR-Code aus (QR
  optional, braucht zusätzlich `pip install qrcode` — ohne das Paket wird
  nur die URI ausgegeben, die sich auch manuell in jede Authenticator-App
  eintragen lässt). Der Secret verlässt dabei nie den eigenen Rechner.

Aktivieren: Script laufen lassen → `TOTP_SECRET=...` in `.env` eintragen →
frontend-Container neu starten. Deaktivieren: Zeile wieder auskommentieren/
entfernen, neu starten.

## Nachtrag: Deployment hinter Nginx Proxy Manager (Cloudflare → IONOS-VPS → WireGuard → NPM)

Für den Betrieb hinter einem Reverse-Proxy-Setup, bei dem der VPS den
Traffic unverändert per WireGuard bis zu NPM durchreicht und NPM Header/TLS
mit einem Origin-Zertifikat selbst übernimmt:

- **`.env`**: `COOKIE_SECURE=true` gesetzt (Session-Cookie braucht `Secure`,
  sobald der Client durchgehend über HTTPS reinkommt).
- **NPM Proxy Host**: „Websockets Support" muss aktiviert sein — sonst
  bricht der Live-Modus (`/ws/live`) ständig ab. Forward-Ziel ist die
  Container-IP/Port des `frontend`-Containers, Scheme intern `http` (TLS
  terminiert ja bereits bei NPM).
- **Cloudflare**: SSL/TLS-Modus „Full (strict)" passend zum NPM-Origin-Zertifikat.
- Keine Anpassung an der App selbst nötig, solange über eine eigene
  Subdomain (nicht einen Sub-Path) zugegriffen wird — alle Pfade
  (`/static/...`, `/api/...`, `/ws/live`) sind absolut und ohne Prefix-Support.


