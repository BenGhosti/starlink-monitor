"""
metrics_collector.py
Pollt die Starlink-Schuessel per gRPC (ueber starlink-grpc-tools von sparky8512)
alle 2 Sekunden und schreibt jeden Tick als Zeile in die Tabelle `metrics`.

Wichtig (verifiziert gegen die echte starlink_grpc.py, Stand 2026):
- status_data() gibt KEIN verschachteltes Tuple aus mehreren generischen dicts
  zurueck, sondern exakt (StatusDict, ObstructionDict, AlertDict) - drei
  bereits flache dicts mit fest definierten Keys.
- "snr" ist im aktuellen Protokoll immer None ("obsoleted in grpc service") -
  das Feld existiert nur noch aus Kompatibilitaetsgruenden.
- Channelaufbau erfolgt ueber ChannelContext(target=...), es gibt keine
  freistehende get_channel()-Funktion im Modul.
"""

import asyncio
import logging
import os
import time

import starlink_grpc

from db import get_db

logger = logging.getLogger("metrics_collector")

STARLINK_HOST = os.environ.get("STARLINK_HOST", "192.168.100.1")
STARLINK_PORT = int(os.environ.get("STARLINK_PORT", "9200"))
POLL_INTERVAL_S = 2

TARGET = f"{STARLINK_HOST}:{STARLINK_PORT}"


_first_poll_logged = False


def _empty_str_to_none(value):
    """Protobuf3-Submessages sind nie None, sondern bei Nichtsetzung ein leeres
    Default-Objekt - string-Felder darin liefern dann '' statt eines fehlenden
    Werts. Normalisiert das zu echtem None."""
    if value == "":
        return None
    return value


def poll_once(context: starlink_grpc.ChannelContext) -> dict:
    """Liest einen Status-Snapshot von der Dish und mappt ihn auf unser DB-Schema.
    status_data() ist synchron/blocking (gRPC-Unary-Call), daher wird diese
    Funktion ueber run_in_executor aus dem asyncio-Loop heraus aufgerufen."""
    global _first_poll_logged

    # Timestamp VOR dem gRPC-Call nehmen, nicht danach: der Call kann je nach
    # Netzwerkpfad zur Dish einige hundert ms dauern, und der gemessene Zustand
    # (Latenz, Drop-Rate etc.) bezieht sich auf den Moment der Anfrage, nicht
    # auf den Moment, an dem die Antwort bei uns eintrifft. Bei 2s-Takt-Daten
    # summiert sich ein systematischer Nachher-Versatz sonst spuerbar auf.
    ts = int(time.time())

    general, obstruction, _alerts = starlink_grpc.status_data(context)
    # general["alerts"] ist bereits das fertige Bitfeld (siehe StatusDict.alerts) -
    # das einzelne AlertDict (_alerts) brauchen wir nicht, das Bitfeld reicht zum
    # Speichern; die Bit->Name-Zuordnung passiert beim Anzeigen im Frontend.

    if not _first_poll_logged:
        # Einmaliges Debug-Log beim allerersten erfolgreichen Poll: zeigt sowohl
        # die bereits gemappten Werte als auch (wichtiger) eine rohe Protobuf-
        # Introspektion von device_info/gps_stats. Hintergrund: status_data()
        # liest diese ueber getattr(status, "device_info", None) - bei Protobuf3
        # sind Submessages NIE None, sondern immer ein (ggf. leeres) Default-
        # Objekt. Ein leeres device_info liefert dann device_id="" (leerer
        # String) statt None, was sich im Frontend wie "fehlende Daten"
        # darstellt, aber technisch bedeutet: "die Dish/Firmware hat dieses
        # Feld nicht gesetzt". Diese rohe Introspektion macht das eindeutig
        # sichtbar, statt nur zu raten. Per LOG_DISH_DEBUG=0 env var abschaltbar.
        if os.environ.get("LOG_DISH_DEBUG", "1") != "0":
            logger.info(
                "Erster Dish-Poll erfolgreich. Gemappte Werte: device_id=%r hardware_version=%r "
                "software_version=%r direction_azimuth=%r direction_elevation=%r "
                "gps_ready=%r gps_enabled=%r gps_sats=%r is_snr_above_noise_floor=%r "
                "(None bedeutet: Dish/Firmware liefert dieses Feld nicht)",
                general.get("id"), general.get("hardware_version"), general.get("software_version"),
                general.get("direction_azimuth"), general.get("direction_elevation"),
                general.get("gps_ready"), general.get("gps_enabled"), general.get("gps_sats"),
                general.get("is_snr_above_noise_floor"),
            )
            try:
                raw_status = starlink_grpc.get_status(context)
                logger.info(
                    "Rohe Protobuf-Introspektion: HasField(device_info)=%s HasField(gps_stats)=%s "
                    "device_info.id=%r device_info.hardware_version=%r gps_stats.gps_valid=%r "
                    "gps_stats.gps_sats=%r boresight_azimuth_deg=%r boresight_elevation_deg=%r",
                    raw_status.HasField("device_info") if hasattr(raw_status, "HasField") else "n/a",
                    raw_status.HasField("gps_stats") if hasattr(raw_status, "HasField") else "n/a",
                    getattr(getattr(raw_status, "device_info", None), "id", "n/a"),
                    getattr(getattr(raw_status, "device_info", None), "hardware_version", "n/a"),
                    getattr(getattr(raw_status, "gps_stats", None), "gps_valid", "n/a"),
                    getattr(getattr(raw_status, "gps_stats", None), "gps_sats", "n/a"),
                    getattr(raw_status, "boresight_azimuth_deg", "n/a"),
                    getattr(raw_status, "boresight_elevation_deg", "n/a"),
                )
            except Exception:  # noqa: BLE001
                logger.exception("Rohe Protobuf-Introspektion fehlgeschlagen (nicht kritisch)")
        _first_poll_logged = True

    return {
        "ts": ts,
        "ping_drop_rate": general.get("pop_ping_drop_rate"),
        "ping_latency_ms": general.get("pop_ping_latency_ms"),
        "obstr_fraction": general.get("fraction_obstructed"),
        "obstr_valid_s": obstruction.get("valid_s"),
        "downlink_bps": general.get("downlink_throughput_bps"),
        "uplink_bps": general.get("uplink_throughput_bps"),
        "state": general.get("state", "UNKNOWN"),
        "snr": general.get("snr"),  # vom Protokoll her praktisch immer None, siehe Hinweis oben
        "uptime_s": general.get("uptime"),
        "seconds_to_first_nonempty_slot": general.get("seconds_to_first_nonempty_slot"),
        "currently_obstructed": general.get("currently_obstructed"),
        "obstruction_duration": general.get("obstruction_duration"),
        "obstruction_interval": general.get("obstruction_interval"),
        "direction_azimuth": general.get("direction_azimuth"),
        "direction_elevation": general.get("direction_elevation"),
        "is_snr_above_noise_floor": general.get("is_snr_above_noise_floor"),
        "gps_ready": general.get("gps_ready"),
        "gps_enabled": general.get("gps_enabled"),
        "gps_sats": general.get("gps_sats"),
        "alerts_bitfield": general.get("alerts"),
        # Statische Geraeteinfo, geht in dish_info statt metrics (siehe insert_metric).
        # _empty_str_to_none: device_info kann als leere Protobuf-Submessage
        # vorliegen (Firmware liefert sie nicht) -> id/hardware_version/
        # software_version waeren sonst '' statt None.
        "device_id": _empty_str_to_none(general.get("id")),
        "hardware_version": _empty_str_to_none(general.get("hardware_version")),
        "software_version": _empty_str_to_none(general.get("software_version")),
    }


def _to_int_bool(value) -> int | None:
    """SQLite kennt keinen bool-Typ - explizit nach 0/1/NULL wandeln statt
    Python-Truthiness implizit casten zu lassen (None soll None bleiben, nicht 0)."""
    if value is None:
        return None
    return 1 if value else 0


async def insert_metric(db, row: dict):
    await db.execute(
        """
        INSERT INTO metrics
            (ts, ping_drop_rate, ping_latency_ms, obstr_fraction, obstr_valid_s,
             downlink_bps, uplink_bps, state, snr, uptime_s,
             seconds_to_first_nonempty_slot, currently_obstructed,
             obstruction_duration, obstruction_interval,
             direction_azimuth, direction_elevation, is_snr_above_noise_floor,
             gps_ready, gps_enabled, gps_sats, alerts_bitfield)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row["ts"],
            row["ping_drop_rate"],
            row["ping_latency_ms"],
            row["obstr_fraction"],
            row["obstr_valid_s"],
            row["downlink_bps"],
            row["uplink_bps"],
            row["state"],
            row["snr"],
            row["uptime_s"],
            row["seconds_to_first_nonempty_slot"],
            _to_int_bool(row["currently_obstructed"]),
            row["obstruction_duration"],
            row["obstruction_interval"],
            row["direction_azimuth"],
            row["direction_elevation"],
            _to_int_bool(row["is_snr_above_noise_floor"]),
            _to_int_bool(row["gps_ready"]),
            _to_int_bool(row["gps_enabled"]),
            row["gps_sats"],
            row.get("alerts_bitfield"),
        ),
    )

    # dish_info ist eine Singleton-Zeile (id=1), bei jedem Tick aktualisiert.
    # Nur schreiben wenn wir tatsaechlich mind. ein Feld haben, sonst wuerden
    # wir bei einem Teil-Ausfall echte Werte mit NULL ueberschreiben.
    if row["device_id"] or row["hardware_version"] or row["software_version"]:
        await db.execute(
            """
            INSERT INTO dish_info (id, device_id, hardware_version, software_version, alerts_bitfield, last_seen_ts)
            VALUES (1, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                device_id=excluded.device_id,
                hardware_version=excluded.hardware_version,
                software_version=excluded.software_version,
                alerts_bitfield=excluded.alerts_bitfield,
                last_seen_ts=excluded.last_seen_ts
            """,
            (row["device_id"], row["hardware_version"], row["software_version"],
             row["alerts_bitfield"], row["ts"]),
        )

    await db.commit()


async def run():
    logger.info("metrics_collector startet gegen %s, Intervall %ss", TARGET, POLL_INTERVAL_S)
    db = await get_db()
    context = starlink_grpc.ChannelContext(target=TARGET)
    loop = asyncio.get_running_loop()

    try:
        while True:
            start = time.monotonic()
            try:
                # status_data() ist blocking gRPC - nicht direkt im Event-Loop aufrufen
                row = await loop.run_in_executor(None, poll_once, context)
                await insert_metric(db, row)
            except starlink_grpc.GrpcError as exc:
                logger.warning("gRPC-Fehler bei Dish-Abfrage: %s", exc)
            except Exception:  # noqa: BLE001
                logger.exception("Unerwarteter Fehler im metrics_collector Tick")

            elapsed = time.monotonic() - start
            await asyncio.sleep(max(0.0, POLL_INTERVAL_S - elapsed))
    finally:
        await db.close()
        context.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
