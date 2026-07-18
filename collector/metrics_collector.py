"""
metrics_collector.py
Polls the Starlink dish via gRPC (using starlink-grpc-tools by sparky8512)
every 2 seconds and writes each tick as a row into the `metrics` table.

Notes (verified against starlink_grpc.py, 2026):
- status_data() returns exactly (StatusDict, ObstructionDict, AlertDict) -
  three flat dicts with fixed keys, not a nested tuple of generic dicts.
- "snr" is always None in the current protocol ("obsoleted in grpc service");
  the field only remains for compatibility.
- Channel setup goes through ChannelContext(target=...); there's no
  standalone get_channel() function in the module.
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
    """Protobuf3 submessages are never None - an unset one is an empty default
    object, so string fields read back as '' instead of missing. Normalize to None."""
    if value == "":
        return None
    return value


def poll_once(context: starlink_grpc.ChannelContext) -> dict:
    """Reads one status snapshot from the dish and maps it to our DB schema.
    status_data() is a blocking gRPC call, so this runs via run_in_executor."""
    global _first_poll_logged

    # Timestamp BEFORE the gRPC call, not after: the call can take a few
    # hundred ms depending on the network path to the dish, and the measured
    # state (latency, drop rate, ...) reflects the moment of the request, not
    # when the response arrives. At a 2s cadence a systematic after-the-fact
    # offset would otherwise add up noticeably.
    ts = int(time.time())

    general, obstruction, _alerts = starlink_grpc.status_data(context)
    # general["alerts"] is already the finished bitfield (StatusDict.alerts) -
    # the individual AlertDict (_alerts) isn't needed; bit->name mapping
    # happens in the frontend at display time.

    if not _first_poll_logged:
        # One-time debug log on the very first successful poll, including a
        # raw protobuf introspection of device_info/gps_stats: since protobuf3
        # submessages are never None, an empty device_info yields device_id=""
        # rather than None, which looks like "missing data" in the frontend
        # but actually means "the dish/firmware didn't set this field". This
        # makes that distinction visible instead of just guessing. Disable
        # with LOG_DISH_DEBUG=0.
        if os.environ.get("LOG_DISH_DEBUG", "1") != "0":
            logger.info(
                "First dish poll succeeded. Mapped values: device_id=%r hardware_version=%r "
                "software_version=%r direction_azimuth=%r direction_elevation=%r "
                "gps_ready=%r gps_enabled=%r gps_sats=%r is_snr_above_noise_floor=%r "
                "(None means the dish/firmware doesn't provide this field)",
                general.get("id"), general.get("hardware_version"), general.get("software_version"),
                general.get("direction_azimuth"), general.get("direction_elevation"),
                general.get("gps_ready"), general.get("gps_enabled"), general.get("gps_sats"),
                general.get("is_snr_above_noise_floor"),
            )
            try:
                raw_status = starlink_grpc.get_status(context)
                logger.info(
                    "Raw protobuf introspection: HasField(device_info)=%s HasField(gps_stats)=%s "
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
                logger.exception("Raw protobuf introspection failed (not critical)")
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
        "snr": general.get("snr"),  # practically always None, see note above
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
        # Static device info, goes into dish_info instead of metrics (see insert_metric).
        "device_id": _empty_str_to_none(general.get("id")),
        "hardware_version": _empty_str_to_none(general.get("hardware_version")),
        "software_version": _empty_str_to_none(general.get("software_version")),
    }


def _to_int_bool(value) -> int | None:
    """SQLite has no bool type - convert explicitly to 0/1/NULL rather than
    relying on implicit Python truthiness (None must stay None, not become 0)."""
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

    # dish_info is a singleton row (id=1), updated on every tick. Only write
    # it if at least one field is present, so a partial outage doesn't
    # overwrite real values with NULL.
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
    logger.info("metrics_collector starting against %s, interval %ss", TARGET, POLL_INTERVAL_S)
    db = await get_db()
    context = starlink_grpc.ChannelContext(target=TARGET)
    loop = asyncio.get_running_loop()

    try:
        while True:
            start = time.monotonic()
            try:
                # status_data() is blocking gRPC - don't call it directly on the event loop
                row = await loop.run_in_executor(None, poll_once, context)
                await insert_metric(db, row)
            except starlink_grpc.GrpcError as exc:
                logger.warning("gRPC error polling the dish: %s", exc)
            except Exception:  # noqa: BLE001
                logger.exception("Unexpected error in metrics_collector tick")

            elapsed = time.monotonic() - start
            await asyncio.sleep(max(0.0, POLL_INTERVAL_S - elapsed))
    finally:
        await db.close()
        context.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
