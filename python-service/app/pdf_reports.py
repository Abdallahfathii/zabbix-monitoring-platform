import os
import logging
from datetime import datetime
from sqlalchemy.orm import Session
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from .data_retrieval import get_devices_with_stats, get_summary_stats

logger = logging.getLogger(__name__)

DATA_DIR = "/data"
REPORTS_DIR = "/tmp/reports"


def generate_report_pdf(filepath: str):
    """Generate comprehensive PDF report with summary and device details."""
    doc = SimpleDocTemplate(filepath, pagesize=A4, title="IoT Monitoring Report", author="Monitoring Platform")
    styles = getSampleStyleSheet()
    elements = []

    # Title Section
    title_style = ParagraphStyle('CustomTitle', parent=styles['Heading1'], fontSize=20, spaceAfter=30)
    elements.append(Paragraph("IoT Monitoring Platform Report", title_style))
    elements.append(Paragraph(f"Generated: {datetime.now().isoformat()}", styles['Normal']))
    elements.append(Spacer(1, 20))

    # Summary Section
    elements.append(Paragraph("Summary Statistics", styles['Heading2']))
    summary = get_summary_stats(doc.session) if hasattr(doc, 'session') else get_summary_stats(Session())
    summary_data = [
        ['Metric', 'Value'],
        ['Total Devices', str(summary['total_devices'])],
        ['Total Measurements', str(summary['total_measurements'])],
        ['Temperature Min', f"{summary['temperature']['min']}°C" if summary['temperature']['min'] else 'N/A'],
        ['Temperature Max', f"{summary['temperature']['max']}°C" if summary['temperature']['max'] else 'N/A'],
        ['Temperature Avg', f"{summary['temperature']['avg']}°C" if summary['temperature']['avg'] else 'N/A'],
        ['Humidity Min', f"{summary['humidity']['min']}%" if summary['humidity']['min'] else 'N/A'],
        ['Humidity Max', f"{summary['humidity']['max']}%" if summary['humidity']['max'] else 'N/A'],
        ['Humidity Avg', f"{summary['humidity']['avg']}%" if summary['humidity']['avg'] else 'N/A'],
        ['Sensor Min', f"{summary['sensor_range']['min']}°C" if summary['sensor_range']['min'] else 'N/A'],
        ['Sensor Max', f"{summary['sensor_range']['max']}°C" if summary['sensor_range']['max'] else 'N/A'],
        ['Alarm Limit Low', f"{summary['limits']['min']}°C" if summary['limits']['min'] else 'N/A'],
        ['Alarm Limit High', f"{summary['limits']['max']}°C" if summary['limits']['max'] else 'N/A'],
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

    # Device Details
    elements.append(Paragraph("Device Details", styles['Heading2']))
    devices_stats = get_devices_with_stats(Session())
    device_data = [['Device ID', 'Name', 'Temp (°C)', 'Humidity (%)', 'Battery (%)', 'Status']]
    for device_id, stats in devices_stats.items():
        device_data.append([
            device_id,
            stats['device']['name'],
            str(stats['latest']['temperature']) if stats['latest']['temperature'] is not None else 'N/A',
            str(stats['latest']['humidity']) if stats['latest']['humidity'] is not None else 'N/A',
            str(stats['latest']['battery']) if stats['latest']['battery'] is not None else 'N/A',
            stats['device']['status']
        ])
    device_table = Table(device_data, colWidths=[65, 75, 55, 60, 55, 55])
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
    logger.info(f"PDF report generated: {filepath}")
    return filepath

def get_device_pdf_report(device_id: str, filepath: str):
    """Generate PDF report for a specific device."""
    from sqlalchemy.orm import Session
    from .models import Device, Measurement

    db = Session()
    try:
        device = db.query(Device).filter(Device.device_id == device_id).first()
        if not device:
            return None

        # Get device statistics
        stats_query = db.execute(text("""
            SELECT
                MIN(temperature) as min_temp, MAX(temperature) as max_temp, AVG(temperature) as avg_temp,
                COUNT(*) as total_measurements
            FROM measurements
            WHERE device_id = :device_id AND temperature IS NOT NULL
        """), {"device_id": device_id})
        temp_stats = stats_query.fetchone()

        # Get latest measurements
        latest = db.query(Measurement)\
            .filter(Measurement.device_id == device_id)\
            .order_by(Measurement.received_at.desc()).first()

        doc = SimpleDocTemplate(filepath, pagesize=A4, title=device.name, author=device.name)
        styles = getSampleStyleSheet()
        elements = []

        title_style = ParagraphStyle('CustomTitle', parent=styles['Heading1'], fontSize=20, spaceAfter=30)
        elements.append(Paragraph(f"Device Report: {device.name}", title_style))
        elements.append(Paragraph(f"Device ID: {device.device_id}", styles['Normal']))
        elements.append(Paragraph(f"Location: {device.location or 'Unknown'}", styles['Normal']))
        elements.append(Paragraph(f"Status: {device.status}", styles['Normal']))
        elements.append(Paragraph(f"Generated: {datetime.now().isoformat()}", styles['Normal']))
        elements.append(Spacer(1, 20))

        # Statistics
        elements.append(Paragraph("Temperature Statistics", styles['Heading2']))
        stats_data = [
            ['Metric', 'Value'],
            ['Total Measurements', str(temp_stats[3] if temp_stats[3] else 0)],
            ['Min Temperature', f"{temp_stats[0]:.2f}°C" if temp_stats[0] else 'N/A'],
            ['Max Temperature', f"{temp_stats[1]:.2f}°C" if temp_stats[1] else 'N/A'],
            ['Average Temperature', f"{temp_stats[2]:.2f}°C" if temp_stats[2] else 'N/A']
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
        elements.append(Spacer(1, 20))

        # Latest Reading
        if latest:
            elements.append(Paragraph("Latest Reading", styles['Heading2']))
            latest_data = [
                ['Parameter', 'Value'],
                ['Temperature', f"{latest.temperature}°C" if latest.temperature is not None else 'N/A'],
                ['Humidity', f"{latest.humidity}%" if latest.humidity is not None else 'N/A'],
                ['Pressure', f"{latest.pressure}hPa" if latest.pressure is not None else 'N/A'],
                ['Battery', f"{latest.battery}%" if latest.battery is not None else 'N/A'],
                ['Sensor 1', f"{latest.sensor_1}°C" if latest.sensor_1 is not None else 'N/A'],
                ['Sensor 2', f"{latest.sensor_2}°C" if latest.sensor_2 is not None else 'N/A'],
                ['Sensor 3', f"{latest.sensor_3}°C" if latest.sensor_3 is not None else 'N/A'],
                ['Sensor 4', f"{latest.sensor_4}°C" if latest.sensor_4 is not None else 'N/A'],
                ['Timestamp', latest.received_at.isoformat()]
            ]
            table = Table(latest_data, colWidths=[120, 180])
            table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
                ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, 0), 10),
                ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
                ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
                ('GRID', (0, 0), (-1, -1), 1, colors.black)
            ]))
            elements.append(table)

        doc.build(elements)
        logger.info(f"Device PDF generated: {filepath}")
        return filepath
    finally:
        db.close()
