import json
import logging
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from sqlalchemy import text, func
from .models import Device, Measurement, Alert, History, SensorSummary
from .config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

def get_devices_with_stats(db: Session):
    """Return devices with their latest readings and summary statistics."""
    devices = db.query(Device).all()
    device_stats = {}
    for device in devices:
        latest = db.query(Measurement)\
            .filter(Measurement.device_id == device.device_id)\
            .order_by(Measurement.received_at.desc()).first()
        if latest:
            device_stats[device.device_id] = {
                'device': {
                    'id': device.device_id,
                    'name': device.name,
                    'location': device.location,
                    'status': device.status
                },
                'latest': {
                    'temperature': latest.temperature,
                    'humidity': latest.humidity,
                    'pressure': latest.pressure,
                    'battery': latest.battery,
                    'sensor_1': latest.sensor_1,
                    'sensor_2': latest.sensor_2,
                    'sensor_3': latest.sensor_3,
                    'sensor_4': latest.sensor_4,
                    'sensor_min': latest.sensor_min,
                    'sensor_max': latest.sensor_max,
                    'threshold_min': latest.threshold_min,
                    'threshold_max': latest.threshold_max,
                    'timestamp': latest.received_at.isoformat()
                }
            }
    return device_stats

def get_summary_stats(db: Session):
    """Return aggregated statistics across all devices."""
    # Temperature stats
    temp_stats = db.query(
        func.min(Measurement.temperature),
        func.max(Measurement.temperature),
        func.avg(Measurement.temperature),
        func.count(Measurement.id)
    ).filter(Measurement.temperature.isnot(None)).first()

    # Humidity stats
    humid_stats = db.query(
        func.min(Measurement.humidity),
        func.max(Measurement.humidity),
        func.avg(Measurement.humidity)
    ).filter(Measurement.humidity.isnot(None)).first()

    # Probe envelope and limits
    probe_stats = db.query(
        func.min(Measurement.sensor_min),
        func.max(Measurement.sensor_max),
        func.min(Measurement.threshold_min),
        func.max(Measurement.threshold_max),
    ).first()

    return {
        'total_devices': db.query(Device).count(),
        'total_measurements': temp_stats[3] if temp_stats[3] else 0,
        'temperature': {
            'min': round(temp_stats[0], 2) if temp_stats[0] else None,
            'max': round(temp_stats[1], 2) if temp_stats[1] else None,
            'avg': round(temp_stats[2], 2) if temp_stats[2] else None
        },
        'humidity': {
            'min': round(humid_stats[0], 2) if humid_stats[0] else None,
            'max': round(humid_stats[1], 2) if humid_stats[1] else None,
            'avg': round(humid_stats[2], 2) if humid_stats[2] else None
        },
        'sensor_range': {
            'min': round(probe_stats[0], 2) if probe_stats[0] is not None else None,
            'max': round(probe_stats[1], 2) if probe_stats[1] is not None else None,
        },
        'limits': {
            'min': round(probe_stats[2], 2) if probe_stats[2] is not None else None,
            'max': round(probe_stats[3], 2) if probe_stats[3] is not None else None,
        }
    }

def get_alerts(db: Session, status: str = "active"):
    """Return active alerts with device information."""
    alerts = db.query(Alert).filter(Alert.status == status).all()
    return [
        {
            'device_id': a.device_id,
            'alert_type': a.alert_type,
            'message': a.message,
            'severity': a.severity,
            'created_at': a.created_at.isoformat()
        }
        for a in alerts
    ]

def get_history_data(db: Session, device_id: str, start_date: str, end_date: str):
    """Load history rows for a device and date range."""
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)
    params = {"dev": device_id, "start": start, "end": end}
    result = db.execute(text("""
        SELECT received_at, key_name, value
        FROM history
        WHERE device_id = :dev
          AND received_at >= :start
          AND received_at < :end
        ORDER BY received_at, key_name
    """), params).fetchall()
    by_time = {}
    for ts, key, val in result:
        bucket = by_time.setdefault(ts, {"time": ts})
        bucket[key] = float(val) if val is not None else None
    return list(by_time.values())