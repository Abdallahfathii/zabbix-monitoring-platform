import json
import logging
import os
import socket
import struct
import time
from datetime import datetime, timezone
from paho.mqtt import client as mqtt_client
from sqlalchemy.orm import Session
from .database import SessionLocal
from sqlalchemy import text
from .models import Device, Measurement, MQTTMessage, History, SensorSummary, Alert
from .config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


def send_to_zabbix(key: str, value) -> bool:
    """Push a value to Zabbix Server via trapper protocol (port 10051)."""
    try:
        data = {
            "request": "sender data",
            "data": [{
                "host": settings.ZABBIX_TRAPPER_HOSTNAME,
                "key": key,
                "value": str(value),
                "clock": int(time.time()),
                "ns": time.time_ns() % 1_000_000_000
            }]
        }
        payload = json.dumps(data).encode()
        packet = b"ZBXD\x01" + struct.pack("<Q", len(payload)) + payload

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        sock.connect((settings.ZABBIX_TRAPPER_HOST, settings.ZABBIX_TRAPPER_PORT))
        sock.sendall(packet)
        sock.recv(1024)
        sock.close()
        logger.info(f"Sent to Zabbix: {key}={value}")
        return True
    except Exception as e:
        logger.error(f"Zabbix trapper error: {e}")
        return False


DEFAULT_TEMP_MIN = 2.0
DEFAULT_TEMP_MAX = 8.0
DEFAULT_BATTERY_MIN = 20.0

# Only store one reading into the `history` table every N minutes per device.
HISTORY_INTERVAL_MINUTES = 15

# Topic segment -> canonical device_id, for hardware that publishes a short
# slot number instead of its serial. Case pairs are listed explicitly.
DEVICE_ALIAS = {
    "D0": "ZTHT37",
    "d0": "ZTHT37",
    "D28": "ZTHT028",
    "d28": "ZTHT028",
}


def resolve_device_id(topic: str, payload: dict = None) -> str:
    """Canonical device_id for one MQTT message.

    The payload's `serial` wins when present. Several loggers are flashed with
    the same slot topic (all publish `zeta/datalogger/D0`), so the topic alone
    cannot tell them apart - only the serial can. Falls back to the topic
    segment (and DEVICE_ALIAS) for payloads that carry no serial.
    """
    serial = (payload or {}).get("serial")
    if isinstance(serial, str) and serial.strip():
        raw = serial.strip()
    else:
        parts = (topic or "").split("/")
        if parts[0] == "zeta" and len(parts) >= 3:
            raw = parts[-1]
        else:
            raw = parts[1] if len(parts) > 1 else "unknown"
    return DEVICE_ALIAS.get(raw, raw)

_db_tz_cache = None


def _db_timezone():
    """Timezone the DB stores naive timestamps in (Postgres session TimeZone).

    Reading tables are `timestamp without time zone`, so a value must arrive
    already converted to that zone or the offset would be silently dropped.
    """
    global _db_tz_cache
    if _db_tz_cache is None:
        from zoneinfo import ZoneInfo
        try:
            name = SessionLocal().execute(
                text("SELECT current_setting('TimeZone')")).scalar()
            _db_tz_cache = ZoneInfo(name or "UTC")
        except Exception as e:
            logger.warning(f"Could not read DB timezone ({e}); using TZ/UTC")
            try:
                _db_tz_cache = ZoneInfo(os.environ.get("TZ", "UTC"))
            except Exception:
                _db_tz_cache = ZoneInfo("UTC")
    return _db_tz_cache


def _db_now() -> datetime:
    """Current wall-clock time in the DB timezone (naive)."""
    return datetime.now(_db_timezone()).replace(tzinfo=None)


def sensor_time(payload_data: dict) -> datetime:
    """Reading time the sensor reported (payload 'epoch'), as DB wall-clock time.

    Matches the device's own `time`/`date` string, so history rows line up with
    what the datalogger displays. Falls back to the server clock when the
    payload has no usable epoch.
    """
    epoch = payload_data.get("epoch")
    try:
        if epoch is not None:
            instant = datetime.fromtimestamp(float(epoch), tz=timezone.utc)
            db_tz = _db_timezone()
            return instant.astimezone(db_tz).replace(tzinfo=None)
    except (TypeError, ValueError):
        logger.warning(f"Invalid sensor epoch {epoch!r}, falling back to server time")
    return _db_now()


def _history_bucket(ts: datetime) -> datetime:
    """Start of the 15-minute sensor-time bucket that contains ts."""
    minute = (ts.minute // HISTORY_INTERVAL_MINUTES) * HISTORY_INTERVAL_MINUTES
    return ts.replace(minute=minute, second=0, microsecond=0)


def _history_due(db: Session, device_id: str, sensor_ts: datetime) -> bool:
    """True when this device has no reading yet in the sensor's 15-minute bucket."""
    bucket = _history_bucket(sensor_ts)
    row = db.execute(text("""
        SELECT count(*)
        FROM history
        WHERE device_id = :d
          AND received_at >= :bucket
          AND received_at < :bucket + interval '15 min'
    """), {"d": device_id, "bucket": bucket}).fetchone()
    return not row or row[0] == 0


def _get_thresholds(payload_data: dict, idx: int):
    """Return (min, max) thresholds for temperature field idx (0-based for sensor_1..4)."""
    t_min = payload_data.get(f"ext_t{idx+1}_min")
    t_max = payload_data.get(f"ext_t{idx+1}_max")
    if t_min is None:
        t_min = payload_data.get("temperature_min", DEFAULT_TEMP_MIN)
    if t_max is None:
        t_max = payload_data.get("temperature_max", DEFAULT_TEMP_MAX)
    try:
        t_min = float(t_min)
    except (TypeError, ValueError):
        t_min = DEFAULT_TEMP_MIN
    try:
        t_max = float(t_max)
    except (TypeError, ValueError):
        t_max = DEFAULT_TEMP_MAX
    return t_min, t_max


def _device_thresholds(payload_data: dict):
    """Device-wide alarm limits = envelope of the per-probe limits in the payload."""
    mins, maxs = zip(*(_get_thresholds(payload_data, i) for i in range(4)))
    return min(mins), max(maxs)


def _upsert_alert(db: Session, device_id: str, alert_type: str, message: str,
                  severity: str = "warning", sensor_ts: datetime = None):
    existing = db.query(Alert).filter(
        Alert.device_id == device_id,
        Alert.alert_type == alert_type,
        Alert.status == "active"
    ).first()
    if existing:
        existing.message = message
        existing.severity = severity
        if sensor_ts is not None:
            existing.created_at = sensor_ts
    else:
        db.add(Alert(
            device_id=device_id,
            alert_type=alert_type,
            message=message,
            severity=severity,
            status="active",
            created_at=sensor_ts or _db_now()
        ))


def _resolve_alert(db: Session, device_id: str, alert_type: str, sensor_ts: datetime = None):
    active = db.query(Alert).filter(
        Alert.device_id == device_id,
        Alert.alert_type == alert_type,
        Alert.status == "active"
    ).all()
    for a in active:
        a.status = "resolved"
        a.resolved_at = sensor_ts or _db_now()


def check_alerts(db: Session, device_id: str, payload_data: dict, values: dict,
                 sensor_ts: datetime = None):
    """Evaluate thresholds and payload alarm flags; upsert/resolve Alert rows.

    All alert timestamps use the sensor clock (sensor_ts) so they line up with
    the reading that triggered them.
    """
    # Sensor-specific threshold checks (sensor_1..sensor_4 and temperature)
    for i, key in enumerate(["sensor_1", "sensor_2", "sensor_3", "sensor_4"]):
        val = values.get(key)
        if val is None:
            continue
        try:
            val = float(val)
        except (TypeError, ValueError):
            continue
        t_min, t_max = _get_thresholds(payload_data, i)
        if val > t_max:
            _upsert_alert(db, device_id, f"{key}_high",
                          f"{key} = {val:.2f}°C exceeds max {t_max:.2f}°C", "critical", sensor_ts=sensor_ts)
            _resolve_alert(db, device_id, f"{key}_low", sensor_ts=sensor_ts)
        elif val < t_min:
            _upsert_alert(db, device_id, f"{key}_low",
                          f"{key} = {val:.2f}°C below min {t_min:.2f}°C", "critical", sensor_ts=sensor_ts)
            _resolve_alert(db, device_id, f"{key}_high", sensor_ts=sensor_ts)
        else:
            _resolve_alert(db, device_id, f"{key}_high", sensor_ts=sensor_ts)
            _resolve_alert(db, device_id, f"{key}_low", sensor_ts=sensor_ts)

    # Overall temperature check
    temp = values.get("temperature")
    if temp is not None:
        try:
            temp = float(temp)
        except (TypeError, ValueError):
            temp = None
    if temp is not None:
        t_min, t_max = _get_thresholds(payload_data, 0)
        if temp > t_max:
            _upsert_alert(db, device_id, "temperature_high",
                          f"Temperature = {temp:.2f}°C exceeds max {t_max:.2f}°C", "critical", sensor_ts=sensor_ts)
            _resolve_alert(db, device_id, "temperature_low", sensor_ts=sensor_ts)
        elif temp < t_min:
            _upsert_alert(db, device_id, "temperature_low",
                          f"Temperature = {temp:.2f}°C below min {t_min:.2f}°C", "critical", sensor_ts=sensor_ts)
            _resolve_alert(db, device_id, "temperature_high", sensor_ts=sensor_ts)
        else:
            _resolve_alert(db, device_id, "temperature_high", sensor_ts=sensor_ts)
            _resolve_alert(db, device_id, "temperature_low", sensor_ts=sensor_ts)

    # Battery low check
    bat = values.get("battery")
    if bat is not None:
        try:
            bat = float(bat)
        except (TypeError, ValueError):
            bat = None
    if bat is not None:
        bat_min = payload_data.get("battery_min", DEFAULT_BATTERY_MIN)
        try:
            bat_min = float(bat_min)
        except (TypeError, ValueError):
            bat_min = DEFAULT_BATTERY_MIN
        if bat < bat_min:
            _upsert_alert(db, device_id, "battery_low",
                          f"Battery = {bat:.1f}% below threshold {bat_min:.1f}%", "warning", sensor_ts=sensor_ts)
        else:
            _resolve_alert(db, device_id, "battery_low", sensor_ts=sensor_ts)

    # Payload-reported alarm flag (device-side alarm, e.g. DS1_TEMP_HIGH)
    if payload_data.get("alarm") in (True, "true", "1", 1):
        alarm_type = str(payload_data.get("alarm_type", "device_alarm"))
        _upsert_alert(db, device_id, "device_alarm",
                      f"Device reported alarm: {alarm_type}", "critical", sensor_ts=sensor_ts)
    else:
        _resolve_alert(db, device_id, "device_alarm", sensor_ts=sensor_ts)


def connect_mqtt() -> mqtt_client.Client:
    def on_connect(client, userdata, flags, rc):
        if rc == 0:
            logger.info("Connected to MQTT Broker!")
            client.subscribe(settings.MQTT_TOPIC)
            client.subscribe(settings.MQTT_TOPIC_EXTRA)
        else:
            logger.error(f"Failed to connect, return code {rc}")

    client = mqtt_client.Client(client_id="python-service")
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(settings.MQTT_BROKER, settings.MQTT_PORT)
    return client


def on_message(client, userdata, msg):
    raw_payload = msg.payload.decode()
    logger.info(f"Received message: {msg.topic} -> {raw_payload}")
    db = SessionLocal()
    try:
        payload_data = json.loads(raw_payload)
        sensor_ts = sensor_time(payload_data)

        mqtt_msg = MQTTMessage(
            topic=msg.topic,
            payload=payload_data,
            qos=msg.qos,
            received_at=sensor_ts
        )
        db.add(mqtt_msg)
        db.commit()

        device_id = resolve_device_id(msg.topic, payload_data)
        # Sanitize device_id to prevent SQL injection
        if not device_id or len(device_id) > 64:
            logger.error(f"Invalid device_id: {device_id}")
            return

        device = db.query(Device).filter(Device.device_id == device_id).first()
        is_new_device = False
        serial = (payload_data or {}).get("serial")
        if isinstance(serial, str):
            serial = serial.strip()
        if not device:
            device = Device(
                device_id=device_id,
                name=device_id,
                location=payload_data.get("location", ""),
                type="sensor",
                mqtt_topic=msg.topic,
                serial=serial
            )
            db.add(device)
            db.flush()
            is_new_device = True
        else:
            if payload_data.get("location"):
                device.location = payload_data.get("location")
            if serial:
                device.serial = serial
            device.mqtt_topic = msg.topic
            device.updated_at = sensor_ts

        temperature = payload_data.get("temperature")
        humidity = payload_data.get("humidity")
        pressure = payload_data.get("pressure")
        battery = payload_data.get("battery")
        sensor_1 = payload_data.get("sensor_1") or payload_data.get("ext_t1")
        sensor_2 = payload_data.get("sensor_2") or payload_data.get("ext_t2")
        sensor_3 = payload_data.get("sensor_3") or payload_data.get("ext_t3")
        sensor_4 = payload_data.get("sensor_4") or payload_data.get("ext_t4")
        if temperature is None:
            ext_temps = [t for t in (sensor_1, sensor_2, sensor_3, sensor_4) if t is not None]
            if ext_temps:
                temperature = sum(ext_temps) / len(ext_temps)

        if battery is None and payload_data.get("bat_pct") is not None:
            battery = payload_data["bat_pct"]

        ext_vals = [v for v in (sensor_1, sensor_2, sensor_3, sensor_4) if v is not None]
        sensor_min = min(ext_vals) if ext_vals else None
        sensor_max = max(ext_vals) if ext_vals else None
        threshold_min, threshold_max = _device_thresholds(payload_data)

        measurement = Measurement(
            device_id=device_id,
            temperature=temperature,
            humidity=humidity,
            pressure=pressure,
            battery=battery,
            sensor_1=sensor_1,
            sensor_2=sensor_2,
            sensor_3=sensor_3,
            sensor_4=sensor_4,
            sensor_min=sensor_min,
            sensor_max=sensor_max,
            threshold_min=threshold_min,
            threshold_max=threshold_max,
            received_at=sensor_ts
        )
        db.add(measurement)

        if _history_due(db, device_id, sensor_ts):
            for key, val in [("temperature", temperature), ("humidity", humidity),
                             ("pressure", pressure), ("battery", battery),
                             ("sensor_1", sensor_1), ("sensor_2", sensor_2),
                             ("sensor_3", sensor_3), ("sensor_4", sensor_4)]:
                if val is not None:
                    hist = History(
                        device_id=device_id,
                        topic=msg.topic,
                        payload=payload_data,
                        key_name=key,
                        value=float(val),
                        received_at=sensor_ts
                    )
                    db.add(hist)
        else:
            logger.info(
                f"Skipped history write for {device_id}: sensor reading at "
                f"{sensor_ts:%Y-%m-%d %H:%M:%S} is in a bucket already stored"
            )

        db.commit()
        logger.info(f"Stored measurement for device {device_id}")

        if temperature is not None:
            # Aggregates are recomputed from `measurements` rather than tracked
            # incrementally, so pruning or out-of-band rows cannot leave stale
            # values. min_temp/max_temp hold the alarm limits published by the
            # device (ext_t1..4_min/max), not the observed extremes.
            agg = db.execute(text(
                "SELECT AVG(temperature) AS avg_t, COUNT(temperature) AS cnt, "
                "MAX(received_at) AS last_seen "
                "FROM measurements WHERE device_id = :d"
            ), {"d": device_id}).fetchone()
            latest = db.execute(text(
                "SELECT temperature FROM measurements WHERE device_id = :d "
                "ORDER BY received_at DESC, id DESC LIMIT 1"
            ), {"d": device_id}).fetchone()

            summary = db.query(SensorSummary).filter(SensorSummary.device_id == device_id).first()
            if summary is None:
                summary = SensorSummary(device_id=device_id)
                db.add(summary)
            summary.min_temp = threshold_min
            summary.max_temp = threshold_max
            summary.avg_temp = agg.avg_t
            summary.latest_temp = latest.temperature if latest is not None else temperature
            summary.reading_count = agg.cnt
            summary.last_seen = agg.last_seen
            db.commit()

        if is_new_device:
            logger.info(f"New device discovered: {device_id}")
            # Auto-provision Zabbix host for this device
            try:
                from .zabbix_setup import provision_host
                provision_host(device)
            except Exception as e:
                logger.error(f"Host provisioning failed for {device_id}: {e}")

        # Make sure Zabbix knows this device *before* the values are pushed:
        # trapper items are created on first sight (and whenever a new metric
        # shows up), so nothing is dropped for lack of an item. The call is a
        # cheap no-op once a device is provisioned; failures never block the
        # alerts below.
        seen_metrics = [
            metric for metric, value in (
                ("temperature", temperature), ("humidity", humidity),
                ("pressure", pressure), ("battery", battery),
                ("sensor_1", sensor_1), ("sensor_2", sensor_2),
                ("sensor_3", sensor_3), ("sensor_4", sensor_4),
            ) if value is not None
        ]
        try:
            from .zabbix_setup import provision_device
            provision_device(device_id, seen_metrics)
        except Exception as e:
            logger.error(f"Auto-provisioning failed for {device_id}: {e}")

        if temperature is not None:
            send_to_zabbix(f"sensor.{device_id}.temperature", temperature)
        if humidity is not None:
            send_to_zabbix(f"sensor.{device_id}.humidity", humidity)
        if pressure is not None:
            send_to_zabbix(f"sensor.{device_id}.pressure", pressure)
        if battery is not None:
            send_to_zabbix(f"sensor.{device_id}.battery", battery)
        if sensor_1 is not None:
            send_to_zabbix(f"sensor.{device_id}.sensor_1", sensor_1)
        if sensor_2 is not None:
            send_to_zabbix(f"sensor.{device_id}.sensor_2", sensor_2)
        if sensor_3 is not None:
            send_to_zabbix(f"sensor.{device_id}.sensor_3", sensor_3)
        if sensor_4 is not None:
            send_to_zabbix(f"sensor.{device_id}.sensor_4", sensor_4)
        if device.location:
            send_to_zabbix(f"sensor.{device_id}.location", device.location)

        # --- Alert evaluation ---
        check_alerts(db, device_id, payload_data, {
            "temperature": temperature,
            "sensor_1": sensor_1,
            "sensor_2": sensor_2,
            "sensor_3": sensor_3,
            "sensor_4": sensor_4,
            "battery": battery,
        }, sensor_ts=sensor_ts)
        db.commit()

        # Publish active/resolved alert states so Zabbix Problems stay in sync
        try:
            from .zabbix_setup import push_alert_states
            push_alert_states()
        except Exception as e:
            logger.error(f"Failed to push alert states to Zabbix: {e}")
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON from {msg.topic}: {raw_payload[:100]}")
        logger.error(f"JSON error: {e}")
        db.rollback()
    except Exception as e:
        logger.error(f"Error processing message: {e}")
        db.rollback()
    finally:
        db.close()


def start_mqtt():
    client = connect_mqtt()
    client.loop_start()
    return client
