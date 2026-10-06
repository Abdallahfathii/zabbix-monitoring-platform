from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response
from sqlalchemy.orm import Session
from sqlalchemy import text
from .database import get_db, engine, Base
from .data_retrieval import get_devices_with_stats, get_summary_stats, get_alerts, get_history_data
from .pdf_reports import generate_report_pdf, get_device_pdf_report
from .chart_utils import generate_chart_data_for_device, generate_html_chart_widget
from .models import Device, Measurement, Alert
from .mqtt_client import start_mqtt
from .config import get_settings
from .reports import get_report_data, generate_pdf_report
from .history_report import generate_history_pdf, list_devices_with_history
from .zabbix_sync import get_zabbix_sync
from .zabbix_setup import run_setup as zabbix_run_setup, sync_location_dashboards as sync_zeta_dashboard
from .pdf_report import generate_pdf
from .chart_utils import generate_chart_data_for_device
import logging
import threading
import os
import time

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

settings = get_settings()

# How often the 'zeta' dashboard layout is re-checked for new devices.
ZETA_LAYOUT_SYNC_SECONDS = 300

Base.metadata.create_all(bind=engine)

mqtt_client = None
zabbix_sync = None


def _run_zabbix_setup():
    try:
        logger.info("Running Zabbix setup...")
        zabbix_run_setup()
    except Exception as e:
        logger.error(f"Zabbix setup failed: {e}")


def _location_dashboard_loop():
    """Keep the location dashboards in step with the devices that report data."""
    while True:
        time.sleep(ZETA_LAYOUT_SYNC_SECONDS)
        try:
            sync_zeta_dashboard()
        except Exception as e:
            logger.error(f"location dashboard sync failed: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global mqtt_client, zabbix_sync
    logger.info("Starting MQTT client...")
    mqtt_client = start_mqtt()
    logger.info("Starting Zabbix sync... (disabled to keep monitoring DB clean)")
    # zabbix_sync = get_zabbix_sync()
    # zabbix_sync.start()
    logger.info("Starting Zabbix setup (items + dashboard)...")
    threading.Thread(target=_run_zabbix_setup, daemon=True).start()
    threading.Thread(target=_location_dashboard_loop, daemon=True).start()
    yield
    if mqtt_client:
        mqtt_client.loop_stop()
        mqtt_client.disconnect()
    if zabbix_sync:
        zabbix_sync.stop()


app = FastAPI(
    title="IoT Monitoring Platform",
    version="1.0.0",
    lifespan=lifespan
)


@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.get("/devices")
def get_devices(db: Session = Depends(get_db)):
    devices = db.query(Device).all()
    ranges = {
        r[0]: r for r in db.execute(text("""
            SELECT device_id,
                   MIN(sensor_min) AS observed_min,
                   MAX(sensor_max) AS observed_max,
                   MIN(threshold_min) AS limit_min,
                   MAX(threshold_max) AS limit_max
            FROM measurements
            GROUP BY device_id
        """)).fetchall()
    }
    out = []
    for d in devices:
        r = ranges.get(d.device_id)
        out.append({
            "device_id": d.device_id,
            "name": d.name,
            "status": d.status,
            "mqtt_topic": d.mqtt_topic,
            "serial": d.serial,
            # Observed probe envelope across every stored reading
            "sensor_min": r[1] if r else None,
            "sensor_max": r[2] if r else None,
            # Alarm limits published by the device over MQTT (2.0 / 8.0)
            "threshold_min": r[3] if r else None,
            "threshold_max": r[4] if r else None,
        })
    return out


@app.get("/devices/{device_id}/measurements")
def get_measurements(device_id: str, limit: int = 100, db: Session = Depends(get_db)):
    measurements = db.query(Measurement)\
        .filter(Measurement.device_id == device_id)\
        .order_by(Measurement.received_at.desc())\
        .limit(limit).all()
    return [
        {"temperature": m.temperature, "humidity": m.humidity,
         "pressure": m.pressure, "battery": m.battery,
         "ext_t1": m.sensor_1, "ext_t2": m.sensor_2,
         "ext_t3": m.sensor_3, "ext_t4": m.sensor_4,
         "sensor_min": m.sensor_min, "sensor_max": m.sensor_max,
         "threshold_min": m.threshold_min, "threshold_max": m.threshold_max,
         "received_at": m.received_at.isoformat()}
        for m in measurements
    ]


@app.get("/alerts")
def get_alerts(status: str = "active", db: Session = Depends(get_db)):
    alerts = db.query(Alert).filter(Alert.status == status).all()
    return [
        {"device_id": a.device_id, "alert_type": a.alert_type,
         "message": a.message, "severity": a.severity,
         "created_at": a.created_at.isoformat()}
        for a in alerts
    ]


@app.get("/sensor-summary")
def get_sensor_summary(db: Session = Depends(get_db)):
    result = db.execute(text("""
        SELECT device_id, min_temp, max_temp, avg_temp, latest_temp, reading_count
        FROM sensor_summary ORDER BY device_id
    """))
    return [
        {"device_id": r[0], "min_temp": r[1], "max_temp": r[2],
         "avg_temp": r[3], "latest_temp": r[4], "reading_count": r[5]}
        for r in result.fetchall()
    ]


@app.get("/sensor/{device_id}/latest")
def get_sensor_latest(device_id: str, db: Session = Depends(get_db)):
    r = db.execute(text("SELECT latest_temp FROM sensor_summary WHERE device_id = :d"), {"d": device_id}).fetchone()
    return r[0] if r else 0


@app.get("/sensor/{device_id}/min")
def get_sensor_min(device_id: str, db: Session = Depends(get_db)):
    r = db.execute(text("SELECT min_temp FROM sensor_summary WHERE device_id = :d"), {"d": device_id}).fetchone()
    return r[0] if r else 0


@app.get("/sensor/{device_id}/max")
def get_sensor_max(device_id: str, db: Session = Depends(get_db)):
    r = db.execute(text("SELECT max_temp FROM sensor_summary WHERE device_id = :d"), {"d": device_id}).fetchone()
    return r[0] if r else 0


@app.get("/sensor/{device_id}/field/{field}")
def get_sensor_field(device_id: str, field: str, db: Session = Depends(get_db)):
    allowed = {"temperature", "humidity", "pressure", "battery", "sensor_1", "sensor_2", "sensor_3", "sensor_4"}
    if field not in allowed:
        return 0
    r = db.execute(
        text(f"SELECT {field} FROM measurements WHERE device_id = :d ORDER BY id DESC LIMIT 1"),
        {"d": device_id}
    ).fetchone()
    return r[0] if r and r[0] is not None else 0


@app.get("/all-sensors")
def get_all_sensors(db: Session = Depends(get_db)):
    result = db.execute(text("""
        SELECT device_id, min_temp, max_temp, avg_temp, latest_temp, reading_count
        FROM sensor_summary ORDER BY device_id
    """))
    lines = []
    for r in result.fetchall():
        lines.append(f"{r[0]}: latest={r[4]}Â°C, min={r[1]}Â°C, max={r[2]}Â°C, avg={r[3]}Â°C, readings={r[5]}")
    return "\n".join(lines) if lines else "No sensors yet"


@app.get("/dashboard-text")
def get_dashboard_text(db: Session = Depends(get_db)):
    total_devices = db.query(Device).count()
    total_measurements = db.query(Measurement).count()
    active_alerts = db.query(Alert).filter(Alert.status == "active").count()
    result = db.execute(text("SELECT device_id, latest_temp FROM sensor_summary ORDER BY device_id"))
    sensors = [f"{r[0]}: {r[1]}Â°C" for r in result.fetchall()]
    lines = [
        f"Devices: {total_devices}",
        f"Measurements: {total_measurements}",
        f"Active Alerts: {active_alerts}",
        "Sensors: " + (", ".join(sensors) if sensors else "none"),
    ]
    return "\n".join(lines)


@app.get("/reports/stats")
def get_stats(db: Session = Depends(get_db)):
    return get_report_data(db)["summary"]


@app.get("/reports/json")
def get_json_report(db: Session = Depends(get_db)):
    return get_report_data(db)


@app.get("/reports/pdf")
def get_pdf_report(db: Session = Depends(get_db)):
    data = get_report_data(db)
    generate_pdf_report(data, "/tmp/report.pdf")
    return FileResponse("/tmp/report.pdf", media_type="application/pdf", filename="iot_report.pdf")


@app.post("/zabbix/sync")
def trigger_zabbix_sync():
    if zabbix_sync:
        zabbix_sync.sync_to_database()
        return {"status": "sync completed"}
    return {"status": "not running"}


@app.get("/zabbix/status")
def zabbix_sync_status():
    return {
        "running": zabbix_sync.running if zabbix_sync else False,
        "auth": "active" if zabbix_sync and zabbix_sync.auth_token else "none"
    }


@app.post("/zabbix/hosts/sync")
def sync_zabbix_hosts(force: bool = False):
    """Sync all devices from monitoring DB to Zabbix hosts."""
    try:
        from .zabbix_setup import sync_all_hosts
        result = sync_all_hosts(force=force)
        return result
    except Exception as e:
        logger.error(f"Zabbix host sync failed: {e}")
        return {"error": str(e), "total": 0, "synced": 0, "failed": 0, "skipped": 0}


@app.post("/zabbix/hosts/sync/{device_id}")
def sync_single_zabbix_host(device_id: str, force: bool = False, db: Session = Depends(get_db)):
    """Re-provision a single device's Zabbix host."""
    from .models import Device
    device = db.query(Device).filter(Device.device_id == device_id).first()
    if not device:
        return {"error": f"Device {device_id} not found", "synced": False}
    try:
        from .zabbix_setup import provision_host
        success = provision_host(device, force=force)
        return {"device_id": device_id, "synced": success}
    except Exception as e:
        logger.error(f"Zabbix host sync for {device_id} failed: {e}")
        return {"error": str(e), "device_id": device_id, "synced": False}


@app.get("/zabbix/hosts/status")
def zabbix_hosts_status(db: Session = Depends(get_db)):
    """List all devices with their Zabbix host sync status."""
    from .models import Device
    devices = db.query(Device).all()
    return [
        {
            "device_id": d.device_id,
            "name": d.name,
            "location": d.location,
            "ip_address": d.ip_address,
            "mqtt_topic": d.mqtt_topic,
            "serial": d.serial,
            "zabbix_hostid": d.zabbix_hostid,
            "zabbix_synced_at": d.zabbix_synced_at.isoformat() if d.zabbix_synced_at else None,
            "zabbix_sync_status": d.zabbix_sync_status,
        }
        for d in devices
    ]


@app.get("/reports/pdf/daily")
def get_daily_pdf(
    device: str = None,
    date: str = None,
    start: str = None,
    end: str = None
):
    path = generate_pdf(device_id=device, date=date,
                        start_time=start, end_time=end)
    if path:
        return FileResponse(path, media_type="application/pdf",
                            filename=os.path.basename(path))
    return {"error": "No data found for the given filters"}


REPORT_FORM_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Zetalogger Report Generator</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: Segoe UI, Arial, sans-serif; background: #0f172a; color: #e2e8f0; min-height: 100vh; padding: 24px; }
  .wrap { max-width: 640px; margin: 40px auto; }
  .card { background: #1e293b; border: 1px solid #334155; border-radius: 12px; padding: 28px; }
  h1 { font-size: 22px; margin-bottom: 4px; color: #38bdf8; }
  .sub { color: #94a3b8; font-size: 13px; margin-bottom: 24px; }
  label { display: block; font-size: 13px; color: #94a3b8; margin: 14px 0 6px; }
  select, input[type=date] {
    width: 100%; padding: 10px 12px; border-radius: 8px; border: 1px solid #475569;
    background: #0f172a; color: #e2e8f0; font-size: 15px;
  }
  select:focus, input:focus { outline: none; border-color: #38bdf8; }
  .row { display: flex; gap: 14px; }
  .row > div { flex: 1; }
  button {
    margin-top: 24px; width: 100%; padding: 12px; border: none; border-radius: 8px;
    background: #0ea5e9; color: #fff; font-size: 16px; font-weight: 600; cursor: pointer;
  }
  button:hover { background: #0284c7; }
  button:disabled { background: #475569; cursor: not-allowed; }
  .err { margin-top: 14px; color: #f87171; font-size: 14px; display: none; }
  .ok { margin-top: 14px; color: #4ade80; font-size: 14px; display: none; }
  a.back { display: inline-block; margin-top: 18px; color: #38bdf8; font-size: 13px; text-decoration: none; }
</style>
</head>
<body>
<div class="wrap">
  <div class="card">
    <h1>Zetalogger Monitoring Report</h1>
    <div class="sub">Select device and date range &mdash; data source: monitoring.history</div>

    <label for="device">Device</label>
    <select id="device"><option value="">Loading devices...</option></select>

    <div class="row">
      <div>
        <label for="start">Start Date</label>
        <input type="date" id="start">
      </div>
      <div>
        <label for="end">End Date</label>
        <input type="date" id="end">
      </div>
    </div>

    <label for="printed">Printed By</label>
    <input type="text" id="printed" value="Admin" style="width:100%;padding:10px 12px;border-radius:8px;border:1px solid #475569;background:#0f172a;color:#e2e8f0;font-size:15px;">

    <button id="btn" onclick="generate()">Generate PDF Report</button>
    <div class="err" id="err"></div>
    <div class="ok" id="ok"></div>
    <a class="back" href="http://localhost:8080">&larr; Back to Zabbix</a>
  </div>
</div>
<script>
async function loadDevices() {
  try {
    const r = await fetch('/report/devices');
    const devices = await r.json();
    const sel = document.getElementById('device');
    if (!devices.length) {
      sel.innerHTML = '<option value="">No devices with history yet</option>';
      return;
    }
    sel.innerHTML = devices.map(d => `<option value="${d}">${d}</option>`).join('');
  } catch (e) {
    document.getElementById('device').innerHTML = '<option value="">Failed to load</option>';
  }
}
function today() {
  const d = new Date();
  return d.toISOString().slice(0, 10);
}
function weekAgo() {
  const d = new Date(); d.setDate(d.getDate() - 7);
  return d.toISOString().slice(0, 10);
}
async function generate() {
  const device = document.getElementById('device').value;
  const start = document.getElementById('start').value;
  const end = document.getElementById('end').value;
  const printed = encodeURIComponent(document.getElementById('printed').value || 'Admin');
  const err = document.getElementById('err');
  const ok = document.getElementById('ok');
  err.style.display = 'none'; ok.style.display = 'none';
  if (!device) { err.textContent = 'Please select a device.'; err.style.display = 'block'; return; }
  if (!start || !end) { err.textContent = 'Please choose start and end dates.'; err.style.display = 'block'; return; }
  if (start > end) { err.textContent = 'Start date must be before end date.'; err.style.display = 'block'; return; }
  const btn = document.getElementById('btn');
  btn.disabled = true; btn.textContent = 'Opening PDF...';
  try {
    const url = `/report/pdf?device=${encodeURIComponent(device)}&start=${start}&end=${end}&printed_by=${printed}`;
    const w = window.open(url, '_blank');
    if (!w) {
      throw new Error('Pop-up blocked. Please allow pop-ups for this site and try again.');
    }
    ok.textContent = 'Report opened in a new tab.';
    ok.style.display = 'block';
  } catch (e) {
    err.textContent = e.message || 'Failed to open report.';
    err.style.display = 'block';
  } finally {
    btn.disabled = false; btn.textContent = 'Generate PDF Report';
  }
}
loadDevices();
document.getElementById('start').value = weekAgo();
document.getElementById('end').value = today();
</script>
</body>
</html>"""


@app.get("/report", response_class=HTMLResponse)
def report_form():
    return REPORT_FORM_HTML


@app.get("/report/devices")
def report_devices(db: Session = Depends(get_db)):
    return list_devices_with_history(db)


@app.get("/report/link", response_class=PlainTextResponse)
def report_link():
    """Plain-text URL for Zabbix plain-text widget / item."""
    return "Open Report Generator: http://python-service:8000/report"


@app.get("/report/pdf")
def report_pdf(
    device: str = Query(..., min_length=1),
    start: str = Query(..., description="YYYY-MM-DD"),
    end: str = Query(..., description="YYYY-MM-DD"),
    printed_by: str = Query("Admin"),
    db: Session = Depends(get_db),
):
    from datetime import datetime as _dt
    try:
        _dt.strptime(start, "%Y-%m-%d")
        _dt.strptime(end, "%Y-%m-%d")
    except ValueError:
        return JSONResponse(status_code=400, content={"error": "Dates must be YYYY-MM-DD"})
    if start > end:
        return JSONResponse(status_code=400, content={"error": "Start date must be before end date"})

    pdf = generate_history_pdf(db, device, start, end, printed_by=printed_by)
    if not pdf:
        return JSONResponse(status_code=404, content={
            "error": f"No history data for device '{device}' between {start} and {end}"
        })
    pdf_bytes, filename = pdf
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


# ============================================
# LIVE DASHBOARD (refreshes every second)
# ============================================

def _iso(dt):
    return dt.isoformat() if dt else None


@app.get("/api/live")
def api_live(db: Session = Depends(get_db)):
    """Latest reading per sensor on the sensor clock, plus freshness and alerts."""
    latest_rows = db.execute(text("""
        SELECT DISTINCT ON (m.device_id)
               m.device_id, d.name, m.temperature, m.battery,
               m.sensor_1, m.sensor_2, m.sensor_3, m.sensor_4, m.received_at,
               m.sensor_min, m.sensor_max, m.threshold_min, m.threshold_max
        FROM measurements m
        LEFT JOIN devices d ON d.device_id = m.device_id
        ORDER BY m.device_id, m.received_at DESC, m.id DESC
    """)).fetchall()

    summary = {}
    for r in db.execute(text("""
        SELECT device_id, min_temp, max_temp, avg_temp, latest_temp,
               reading_count, last_seen
        FROM sensor_summary
    """)).fetchall():
        summary[r[0]] = {
            "min_temp": r[1], "max_temp": r[2], "avg_temp": r[3],
            "latest_temp": r[4], "reading_count": r[5], "last_seen": _iso(r[6]),
        }

    fresh = db.execute(text("""
        SELECT now(), max(uploaded_at),
               EXTRACT(EPOCH FROM (now() - max(uploaded_at)))
        FROM mqtt_messages
    """)).fetchone()

    alert_rows = db.execute(text("""
        SELECT device_id, alert_type, message, severity, created_at
        FROM alerts WHERE status = 'active' ORDER BY created_at DESC
    """)).fetchall()

    recent = [
        {"device_id": r[0], "temperature": r[1], "battery": r[2],
         "sensor_1": r[3], "sensor_2": r[4], "sensor_3": r[5], "sensor_4": r[6],
         "sensor_time": _iso(r[7]),
         "sensor_min": r[8], "sensor_max": r[9]}
        for r in db.execute(text("""
            SELECT device_id, temperature, battery, sensor_1, sensor_2,
                   sensor_3, sensor_4, received_at, sensor_min, sensor_max
            FROM measurements
            ORDER BY received_at DESC, id DESC
            LIMIT 12
        """)).fetchall()
    ]

    alerts = [
        {"device_id": r[0], "alert_type": r[1], "message": r[2],
         "severity": r[3], "created_at": _iso(r[4])}
        for r in alert_rows
    ]
    alerts_by_device = {}
    for a in alerts:
        alerts_by_device.setdefault(a["device_id"], []).append(a)

    devices = []
    for r in latest_rows:
        s = summary.get(r[0], {})
        devices.append({
            "device_id": r[0],
            "name": r[1] or r[0],
            "temperature": r[2], "battery": r[3],
            "sensor_1": r[4], "sensor_2": r[5],
            "sensor_3": r[6], "sensor_4": r[7],
            "sensor_time": _iso(r[8]),
            "sensor_min": r[9], "sensor_max": r[10],
            "threshold_min": r[11], "threshold_max": r[12],
            "min_temp": s.get("min_temp"), "max_temp": s.get("max_temp"),
            "avg_temp": s.get("avg_temp"), "latest_temp": s.get("latest_temp"),
            "reading_count": s.get("reading_count"),
            "last_seen": s.get("last_seen"),
            "alerts": alerts_by_device.get(r[0], []),
        })

    return {
        "server_time": _iso(fresh[0]) if fresh else None,
        "last_upload": _iso(fresh[1]) if fresh else None,
        "upload_age_s": round(fresh[2]) if fresh and fresh[2] is not None else None,
        "devices": devices,
        "alerts": alerts,
        "recent": recent,
    }


@app.get("/", response_class=HTMLResponse)
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard_page():
    return DASHBOARD_HTML


# ============================================
# CHART DATA (Chart.js payloads, 5-minute cache)
# ============================================

_CHART_CACHE = {}
_CHART_TTL_S = 300


@app.get("/api/charts/{device_id}")
def get_device_charts(
    device_id: str,
    hours: int = Query(24, ge=1, le=24 * 7),
    db: Session = Depends(get_db),
):
    """Chart.js payload for one device: hourly min/max/avg series + MQTT limits."""
    if not device_id or len(device_id) > 64:
        return JSONResponse(status_code=400, content={"error": "Invalid device_id"})

    key = (device_id, hours)
    now = time.monotonic()
    cached = _CHART_CACHE.get(key)
    if cached and now - cached[0] < _CHART_TTL_S:
        return cached[1]

    has_data = db.execute(
        text("SELECT 1 FROM measurements WHERE device_id = :d LIMIT 1"),
        {"d": device_id},
    ).fetchone()
    if not has_data:
        return JSONResponse(status_code=404,
                            content={"error": f"No data for device '{device_id}'"})

    series = generate_chart_data_for_device(device_id, db, hours)
    if not series:
        return JSONResponse(status_code=404, content={
            "error": f"No readings for device '{device_id}' in the last {hours}h"
        })

    limits = db.execute(
        text("SELECT min_temp, max_temp FROM sensor_summary WHERE device_id = :d"),
        {"d": device_id},
    ).fetchone()

    payload = {
        "device_id": device_id,
        "hours": hours,
        "threshold_min": limits[0] if limits else None,
        "threshold_max": limits[1] if limits else None,
        **series,
    }
    _CHART_CACHE[key] = (now, payload)
    return payload


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Live Sensor Dashboard</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: Segoe UI, Arial, sans-serif; background: #0f172a; color: #e2e8f0; margin: 0; padding: 22px; }
  .wrap { max-width: 1180px; margin: 0 auto; }
  header { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; }
  h1 { font-size: 24px; margin: 0; color: #38bdf8; }
  h2 { font-size: 15px; margin: 0 0 10px; color: #94a3b8; font-weight: 600; text-transform: uppercase; letter-spacing: .5px; }
  .sub { color: #64748b; font-size: 13px; margin-top: 4px; }
  .status { display: flex; align-items: center; gap: 8px; background: #1e293b; border: 1px solid #334155;
            padding: 8px 14px; border-radius: 999px; font-size: 13px; }
  .dot { width: 10px; height: 10px; border-radius: 50%; background: #475569; }
  .dot.on { background: #22c55e; box-shadow: 0 0 0 0 rgba(34,197,94,.6); animation: pulse 1s infinite; }
  .dot.off { background: #ef4444; }
  @keyframes pulse { 70% { box-shadow: 0 0 0 8px rgba(34,197,94,0); } 100% { box-shadow: 0 0 0 0 rgba(34,197,94,0); } }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(330px, 1fr)); gap: 16px; margin-top: 20px; }
  .card { background: #1e293b; border: 1px solid #334155; border-radius: 12px; padding: 18px; }
  .card .top { display: flex; justify-content: space-between; align-items: baseline; }
  .card .name { font-size: 17px; font-weight: 600; }
  .card .id { color: #64748b; font-size: 12px; }
  .big { font-size: 44px; font-weight: 700; color: #f8fafc; margin: 10px 0 2px; }
  .big span { font-size: 18px; color: #94a3b8; }
  .row { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 12px; }
  .chip { background: #0f172a; border: 1px solid #334155; border-radius: 8px; padding: 7px 10px; font-size: 12px; min-width: 74px; }
  .chip b { display: block; font-size: 15px; color: #e2e8f0; font-weight: 600; }
  .chip i { color: #64748b; font-style: normal; }
  .time { margin-top: 14px; font-size: 12px; color: #94a3b8; border-top: 1px solid #334155; padding-top: 10px; }
  .time b { color: #38bdf8; font-weight: 600; }
  .stats { display: flex; justify-content: space-between; font-size: 12px; color: #94a3b8; margin-top: 8px; }
  .alerts { margin-top: 18px; display: none; }
  .alert { background: #451a1a; border: 1px solid #7f1d1d; color: #fecaca; border-radius: 8px;
           padding: 9px 12px; font-size: 13px; margin-bottom: 7px; }
  .alert.warn { background: #422e06; border-color: #854d0e; color: #fde68a; }
  .panel { background: #1e293b; border: 1px solid #334155; border-radius: 12px; padding: 18px; margin-top: 18px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; color: #64748b; font-weight: 600; padding: 7px 8px; border-bottom: 1px solid #334155; }
  td { padding: 7px 8px; border-bottom: 1px solid #1e293b; }
  tr:first-child td { color: #f8fafc; }
  .links { margin-top: 16px; font-size: 13px; }
  .links a { color: #38bdf8; text-decoration: none; margin-right: 16px; }
  .chart-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(420px, 1fr)); gap: 16px; }
  .chart-box { position: relative; height: 260px; }
  .chart-note { color: #64748b; font-size: 12px; margin-top: 6px; }

  /* Sensor value status indicators */
  .sensor-value-normal {
    color: #e2e8f0; /* slate-200 - normal state */
  }
  .sensor-value-warning {
    color: #fbbf24; /* amber-400 - warning state */
    font-weight: 600;
  }
  .sensor-value-critical {
    color: #ef4444; /* red-500 - critical state */
    font-weight: 600;
    animation: sensorPulse 1.5s infinite;
  }

  /* Pulse animation for critical alerts */
  @keyframes sensorPulse {
    0% { opacity: 1; }
    50% { opacity: 0.7; }
    100% { opacity: 1; }
  }
</style>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
</head>
<body>
<div class="wrap">
  <header>
    <div>
      <h1>Live Sensor Dashboard</h1>
      <div class="sub">Sensor clock &mdash; auto-refresh every 1 second</div>
    </div>
    <div class="status"><span class="dot" id="dot"></span><span id="live">connecting...</span></div>
  </header>

  <div class="alerts" id="alerts"></div>
  <div class="grid" id="grid"></div>

  <div class="panel">
    <h2>Recent readings</h2>
    <table>
      <thead><tr><th>Reading time (sensor)</th><th>Device</th><th>Temp</th><th>Battery</th>
        <th>S1</th><th>S2</th><th>S3</th><th>S4</th></tr></thead>
      <tbody id="recent"><tr><td colspan="8">Loading...</td></tr></tbody>
    </table>
  </div>

  <div class="panel" id="charts-panel" style="display:none">
    <h2>Trends &mdash; hourly min / max / avg (last 24h)</h2>
    <div class="chart-grid" id="charts"><span class="chart-note">Loading charts...</span></div>
  </div>

  <div class="links">
    <a href="/report">&larr; Report generator</a>
    <a href="http://localhost:8080">Zabbix</a>
    <a href="http://localhost:8081">Adminer</a>
  </div>
</div>

<script>
const F = v => (v === null || v === undefined) ? '-' : Number(v).toFixed(1);
const T = s => s ? s.replace('T', ' ').slice(0, 19) : '-';
let age = null;

// Warning threshold configuration: 10% of range from each limit
const WARNING_PERCENTAGE = 0.10;

// Determine sensor status based on value vs limits
function getSensorStatus(value, minLimit, maxLimit) {
  if (value === null || value === undefined) return 'normal';

  const range = maxLimit - minLimit;
  const warningMin = minLimit + (range * WARNING_PERCENTAGE);
  const warningMax = maxLimit - (range * WARNING_PERCENTAGE);

  if (value < minLimit || value > maxLimit) {
    return 'critical'; // RED - Outside limits
  }
  if (value < warningMin || value > warningMax) {
    return 'warning';  // ORANGE - Approaching limits
  }
  return 'normal';       // GREEN - Within safe range
}

// Get CSS class for sensor value based on status
function getStatusClass(value, minLimit, maxLimit) {
  const status = getSensorStatus(value, minLimit, maxLimit);
  return `sensor-value-${status}`;
}

function card(d) {
  const alertBadge = d.alerts && d.alerts.length
    ? `<span style="color:#f87171;font-size:12px">${d.alerts.length} alert(s)</span>` : '';
  return `<div class="card">
    <div class="top"><div class="name">${d.name}</div><div class="id">${d.device_id}</div></div>
    <div class="big">${F(d.temperature)}<span>&deg;C</span></div>
    <div class="row">
      <div class="chip"><i>Battery</i><b class="${getStatusClass(d.battery, 0, 100)}">${F(d.battery)}%</b></div>
      <div class="chip"><i>Sensor 1</i><b class="${getStatusClass(d.sensor_1, d.threshold_min, d.threshold_max)}">${F(d.sensor_1)}</b></div>
      <div class="chip"><i>Sensor 2</i><b class="${getStatusClass(d.sensor_2, d.threshold_min, d.threshold_max)}">${F(d.sensor_2)}</b></div>
      <div class="chip"><i>Sensor 3</i><b class="${getStatusClass(d.sensor_3, d.threshold_min, d.threshold_max)}">${F(d.sensor_3)}</b></div>
      <div class="chip"><i>Sensor 4</i><b class="${getStatusClass(d.sensor_4, d.threshold_min, d.threshold_max)}">${F(d.sensor_4)}</b></div>
    </div>
    <div class="stats">
      <span>min ${F(d.min_temp)} &nbsp; avg ${F(d.avg_temp)} &nbsp; max ${F(d.max_temp)}</span>
      <span>${d.reading_count || 0} readings</span>
    </div>
    <div class="time">Reading time: <b>${T(d.sensor_time)}</b> &nbsp; ${alertBadge}</div>
  </div>`;
}

function render(data) {
  document.getElementById('grid').innerHTML =
    (data.devices || []).map(card).join('') || '<div class="card">No sensors yet</div>';

  const box = document.getElementById('alerts');
  if (data.alerts && data.alerts.length) {
    box.style.display = 'block';
    box.innerHTML = data.alerts.map(a =>
      `<div class="alert ${a.severity === 'warning' ? 'warn' : ''}">${a.device_id}: ${a.message}</div>`).join('');
  } else { box.style.display = 'none'; box.innerHTML = ''; }

  document.getElementById('recent').innerHTML =
    (data.recent || []).map(r => {
      const tempClass = getStatusClass(r.temperature, r.sensor_min, r.sensor_max);
      const batClass = getStatusClass(r.battery, 0, 100);
      return `<tr>
        <td>${T(r.sensor_time)}</td><td>${r.device_id}</td>
        <td class="${tempClass}">${F(r.temperature)}</td>
        <td class="${batClass}">${F(r.battery)}</td>
        <td>${F(r.sensor_1)}</td><td>${F(r.sensor_2)}</td>
        <td>${F(r.sensor_3)}</td><td>${F(r.sensor_4)}</td></tr>`;
    }).join('') || '<tr><td colspan="8">No readings yet</td></tr>';

  age = (data.upload_age_s === null || data.upload_age_s === undefined) ? null : data.upload_age_s;
  paint();
}

function paint() {
  const dot = document.getElementById('dot'), live = document.getElementById('live');
  if (age === null) { dot.className = 'dot off'; live.textContent = 'no data'; return; }
  dot.className = 'dot on';
  live.textContent = `last upload ${age}s ago`;
}

async function load() {
  try {
    const r = await fetch('/api/live', { cache: 'no-store' });
    if (!r.ok) throw new Error(r.status);
    render(await r.json());
  } catch (e) {
    document.getElementById('dot').className = 'dot off';
    document.getElementById('live').textContent = 'disconnected - retrying';
  }
}

let chartsBooted = false;

async function loadCharts() {
  if (chartsBooted) return;
  const box = document.getElementById('charts');
  try {
    const devs = await (await fetch('/devices', { cache: 'no-store' })).json();
    if (!devs.length) { box.innerHTML = '<span class="chart-note">No devices yet.</span>'; return; }
    if (typeof Chart === 'undefined') {
      box.innerHTML = '<span class="chart-note">Chart.js CDN unreachable - charts unavailable.</span>';
      return;
    }
    chartsBooted = true;
    box.innerHTML = '';
    document.getElementById('charts-panel').style.display = 'block';

    for (const d of devs) {
      const box2 = document.createElement('div');
      box2.className = 'chart-box';
      const canvas = document.createElement('canvas');
      box2.appendChild(canvas);
      box.appendChild(box2);
      try {
        const cr = await fetch(`/api/charts/${encodeURIComponent(d.device_id)}?hours=24`, { cache: 'no-store' });
        if (!cr.ok) { box2.innerHTML = '<span class="chart-note">No chart data</span>'; continue; }
        const p = await cr.json();
        new Chart(canvas.getContext('2d'), {
          type: 'line',
          data: { labels: p.labels, datasets: p.datasets },
          options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: { mode: 'index', intersect: false },
            plugins: {
              title: { display: true, text: `${d.name} - limits ${p.threshold_min} / ${p.threshold_max}`, color: '#e2e8f0' },
              legend: { position: 'top', labels: { color: '#94a3b8', boxWidth: 12, font: { size: 10 } } },
              tooltip: { backgroundColor: 'rgba(0,0,0,0.8)' }
            },
            scales: {
              x: { grid: { color: '#1e293b' }, ticks: { color: '#64748b', maxTicksLimit: 8 } },
              y: { grid: { color: '#1e293b' }, ticks: { color: '#64748b' } }
            }
          }
        });
      } catch (e) {
        box2.innerHTML = '<span class="chart-note">Chart failed to render</span>';
      }
    }
  } catch (e) {
    box.innerHTML = '<span class="chart-note">Failed to load charts.</span>';
  }
}

setInterval(() => { if (age !== null) { age += 1; paint(); } }, 1000);
setInterval(load, 1000);
load();
loadCharts();
</script>
</body>
</html>"""
