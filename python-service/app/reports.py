import os
from datetime import datetime
from sqlalchemy.orm import Session
from sqlalchemy import func
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from .models import Device, Measurement


def _num(value, suffix: str = "") -> str:
    return f"{value}{suffix}" if value is not None else "N/A"


def get_report_data(db: Session) -> dict:
    # Total devices
    total_devices = db.query(func.count(Device.id)).scalar()

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

    # Latest measurements per device
    latest_measurements = []
    devices = db.query(Device).all()
    for device in devices:
        latest = db.query(Measurement)\
            .filter(Measurement.device_id == device.device_id)\
            .order_by(Measurement.received_at.desc())\
            .first()
        if latest:
            latest_measurements.append({
                "device_id": device.device_id,
                "name": device.name,
                "temperature": latest.temperature,
                "humidity": latest.humidity,
                "pressure": latest.pressure,
                "battery": latest.battery,
                "sensor_1": latest.sensor_1,
                "sensor_2": latest.sensor_2,
                "sensor_3": latest.sensor_3,
                "sensor_4": latest.sensor_4,
                # Observed probe envelope + MQTT alarm limits for this reading
                "sensor_min": latest.sensor_min,
                "sensor_max": latest.sensor_max,
                "threshold_min": latest.threshold_min,
                "threshold_max": latest.threshold_max,
                "timestamp": latest.received_at.isoformat()
            })

    # Observed probe envelope across all readings, plus the MQTT alarm limits
    probe_stats = db.query(
        func.min(Measurement.sensor_min),
        func.max(Measurement.sensor_max),
        func.min(Measurement.threshold_min),
        func.max(Measurement.threshold_max),
    ).first()

    return {
        "report_generated": datetime.now().isoformat(),
        "summary": {
            "total_devices": total_devices,
            "total_measurements": temp_stats[3] if temp_stats[3] else 0,
            "temperature": {
                "min": round(temp_stats[0], 2) if temp_stats[0] else None,
                "max": round(temp_stats[1], 2) if temp_stats[1] else None,
                "avg": round(temp_stats[2], 2) if temp_stats[2] else None
            },
            "humidity": {
                "min": round(humid_stats[0], 2) if humid_stats[0] else None,
                "max": round(humid_stats[1], 2) if humid_stats[1] else None,
                "avg": round(humid_stats[2], 2) if humid_stats[2] else None
            },
            "sensor_range": {
                "min": round(probe_stats[0], 2) if probe_stats[0] is not None else None,
                "max": round(probe_stats[1], 2) if probe_stats[1] is not None else None,
            },
            "limits": {
                "min": round(probe_stats[2], 2) if probe_stats[2] is not None else None,
                "max": round(probe_stats[3], 2) if probe_stats[3] is not None else None,
            },
        },
        "devices": latest_measurements
    }


def generate_pdf_report(data: dict, filepath: str):
    host = (data.get("devices") or [{}])[0].get("device_id") or "monitoring"
    doc = SimpleDocTemplate(filepath, pagesize=A4, title=host, author=host)
    styles = getSampleStyleSheet()
    elements = []

    title_style = ParagraphStyle('CustomTitle', parent=styles['Heading1'], fontSize=20, spaceAfter=30)
    elements.append(Paragraph("IoT Monitoring Report", title_style))
    elements.append(Paragraph(f"Generated: {data['report_generated']}", styles['Normal']))
    elements.append(Spacer(1, 20))

    elements.append(Paragraph("Summary", styles['Heading2']))
    summary = data['summary']

    summary_data = [
        ['Metric', 'Value'],
        ['Total Devices', str(summary['total_devices'])],
        ['Total Measurements', str(summary['total_measurements'])],
        ['Temperature Min', f"{summary['temperature']['min']} C" if summary['temperature']['min'] else 'N/A'],
        ['Temperature Max', f"{summary['temperature']['max']} C" if summary['temperature']['max'] else 'N/A'],
        ['Temperature Avg', f"{summary['temperature']['avg']} C" if summary['temperature']['avg'] else 'N/A'],
        ['Humidity Min', f"{summary['humidity']['min']}%" if summary['humidity']['min'] else 'N/A'],
        ['Humidity Max', f"{summary['humidity']['max']}%" if summary['humidity']['max'] else 'N/A'],
        ['Humidity Avg', f"{summary['humidity']['avg']}%" if summary['humidity']['avg'] else 'N/A'],
        ['Sensor Min (observed)', _num((summary.get('sensor_range') or {}).get('min'), ' C')],
        ['Sensor Max (observed)', _num((summary.get('sensor_range') or {}).get('max'), ' C')],
        ['Alarm Limit Low (MQTT)', _num((summary.get('limits') or {}).get('min'), ' C')],
        ['Alarm Limit High (MQTT)', _num((summary.get('limits') or {}).get('max'), ' C')],
    ]

    table = Table(summary_data, colWidths=[150, 150])
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 12),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
        ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
        ('GRID', (0, 0), (-1, -1), 1, colors.black)
    ]))
    elements.append(table)
    elements.append(Spacer(1, 30))

    if data['devices']:
        elements.append(Paragraph("Device Details", styles['Heading2']))
        device_data = [['Device ID', 'Name', 'Temp (C)', 'Humidity (%)', 'Battery (%)',
                        'Sensor Min', 'Sensor Max']]
        for d in data['devices']:
            device_data.append([
                d['device_id'],
                d['name'],
                str(d['temperature']) if d['temperature'] is not None else 'N/A',
                str(d['humidity']) if d['humidity'] is not None else 'N/A',
                str(d['battery']) if d['battery'] is not None else 'N/A',
                _num(d.get('sensor_min')),
                _num(d.get('sensor_max')),
            ])

        device_table = Table(device_data, colWidths=[65, 75, 55, 60, 55, 45, 46])
        device_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 10),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
            ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
            ('GRID', (0, 0), (-1, -1), 1, colors.black)
        ]))
        elements.append(device_table)

    doc.build(elements)
    return filepath


def get_device_report_data(db: Session, device_id: str) -> dict:
    # Get device info
    device = db.query(Device).filter(Device.device_id == device_id).first()
    if not device:
        return {"error": f"Device {device_id} not found"}

    # Temperature stats for this device
    temp_stats = db.query(
        func.min(Measurement.temperature),
        func.max(Measurement.temperature),
        func.avg(Measurement.temperature),
        func.count(Measurement.id)
    ).filter(
        Measurement.device_id == device_id,
        Measurement.temperature.isnot(None)
    ).first()

    # Humidity stats for this device
    humid_stats = db.query(
        func.min(Measurement.humidity),
        func.max(Measurement.humidity),
        func.avg(Measurement.humidity)
    ).filter(
        Measurement.device_id == device_id,
        Measurement.humidity.isnot(None)
    ).first()

    # All measurements for this device
    measurements = db.query(Measurement)\
        .filter(Measurement.device_id == device_id)\
        .order_by(Measurement.received_at.desc())\
        .all()

    probe_stats = db.query(
        func.min(Measurement.sensor_min),
        func.max(Measurement.sensor_max),
        func.min(Measurement.threshold_min),
        func.max(Measurement.threshold_max),
    ).filter(Measurement.device_id == device_id).first()

    return {
        "device": {
            "device_id": device.device_id,
            "name": device.name,
            "location": device.location,
            "status": device.status
        },
        "statistics": {
            "total_measurements": temp_stats[3] if temp_stats[3] else 0,
            "temperature": {
                "min": round(temp_stats[0], 2) if temp_stats[0] else None,
                "max": round(temp_stats[1], 2) if temp_stats[1] else None,
                "avg": round(temp_stats[2], 2) if temp_stats[2] else None
            },
            "humidity": {
                "min": round(humid_stats[0], 2) if humid_stats[0] else None,
                "max": round(humid_stats[1], 2) if humid_stats[1] else None,
                "avg": round(humid_stats[2], 2) if humid_stats[2] else None
            },
            "sensor_range": {
                "min": round(probe_stats[0], 2) if probe_stats[0] is not None else None,
                "max": round(probe_stats[1], 2) if probe_stats[1] is not None else None,
            },
            "limits": {
                "min": round(probe_stats[2], 2) if probe_stats[2] is not None else None,
                "max": round(probe_stats[3], 2) if probe_stats[3] is not None else None,
            },
        },
        "measurements": [
            {
                "temperature": m.temperature,
                "humidity": m.humidity,
                "pressure": m.pressure,
                "battery": m.battery,
                "sensor_1": m.sensor_1,
                "sensor_2": m.sensor_2,
                "sensor_3": m.sensor_3,
                "sensor_4": m.sensor_4,
                "sensor_min": m.sensor_min,
                "sensor_max": m.sensor_max,
                "threshold_min": m.threshold_min,
                "threshold_max": m.threshold_max,
                "timestamp": m.received_at.isoformat()
            }
            for m in measurements
        ]
    }


def generate_device_pdf_report(data: dict, filepath: str):
    host = (data.get("device") or {}).get("device_id") or "monitoring"
    doc = SimpleDocTemplate(filepath, pagesize=A4, title=host, author=host)
    styles = getSampleStyleSheet()
    elements = []

    # Title
    title_style = ParagraphStyle('CustomTitle', parent=styles['Heading1'], fontSize=20, spaceAfter=30)
    elements.append(Paragraph(f"Device Report: {data['device']['name']}", title_style))
    elements.append(Paragraph(f"Device ID: {data['device']['device_id']}", styles['Normal']))
    elements.append(Paragraph(f"Generated: {datetime.now().isoformat()}", styles['Normal']))
    elements.append(Spacer(1, 20))

    # Statistics Section
    elements.append(Paragraph("Temperature Statistics", styles['Heading2']))
    stats = data['statistics']

    stats_data = [
        ['Metric', 'Value'],
        ['Total Measurements', str(stats['total_measurements'])],
        ['Temperature Min', f"{stats['temperature']['min']}°C" if stats['temperature']['min'] else 'N/A'],
        ['Temperature Max', f"{stats['temperature']['max']}°C" if stats['temperature']['max'] else 'N/A'],
        ['Temperature Avg', f"{stats['temperature']['avg']}°C" if stats['temperature']['avg'] else 'N/A'],
        ['Humidity Min', f"{stats['humidity']['min']}%" if stats['humidity']['min'] else 'N/A'],
        ['Humidity Max', f"{stats['humidity']['max']}%" if stats['humidity']['max'] else 'N/A'],
        ['Humidity Avg', f"{stats['humidity']['avg']}%" if stats['humidity']['avg'] else 'N/A'],
        ['Sensor Min (observed)', _num((stats.get('sensor_range') or {}).get('min'), '°C')],
        ['Sensor Max (observed)', _num((stats.get('sensor_range') or {}).get('max'), '°C')],
        ['Alarm Limit Low (MQTT)', _num((stats.get('limits') or {}).get('min'), '°C')],
        ['Alarm Limit High (MQTT)', _num((stats.get('limits') or {}).get('max'), '°C')],
    ]

    table = Table(stats_data, colWidths=[150, 150])
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 12),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
        ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
        ('GRID', (0, 0), (-1, -1), 1, colors.black)
    ]))
    elements.append(table)
    elements.append(Spacer(1, 30))

    # Recent Measurements
    if data['measurements']:
        elements.append(Paragraph("Recent Measurements", styles['Heading2']))
        measure_data = [['Temp (°C)', 'Humidity (%)', 'Pressure', 'Battery (%)',
                         'Sensor Min', 'Sensor Max', 'Timestamp']]
        for m in data['measurements'][:20]:  # Last 20 measurements
            measure_data.append([
                str(m['temperature']) if m['temperature'] is not None else 'N/A',
                str(m['humidity']) if m['humidity'] is not None else 'N/A',
                str(m['pressure']) if m['pressure'] is not None else 'N/A',
                str(m['battery']) if m['battery'] is not None else 'N/A',
                _num(m.get('sensor_min')),
                _num(m.get('sensor_max')),
                m['timestamp'][:19] if m['timestamp'] else 'N/A'
            ])

        measure_table = Table(measure_data, colWidths=[55, 60, 55, 55, 45, 45, 90])
        measure_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 9),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
            ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
            ('GRID', (0, 0), (-1, -1), 1, colors.black)
        ]))
        elements.append(measure_table)

    doc.build(elements)
    return filepath
