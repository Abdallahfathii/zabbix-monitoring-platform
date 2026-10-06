import json
import os
import logging
from datetime import datetime, timedelta, timezone
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer

logger = logging.getLogger(__name__)

DATA_DIR = "/data"
# Reports are written to the container temp dir, never to the mounted host folder.
REPORTS_DIR = "/tmp/reports"


def load_records(device_id: str = None, date: str = None,
                 start_time: str = None, end_time: str = None):
    """Load JSON files with optional device, date, and time filters."""
    records = []
    base = DATA_DIR

    if device_id:
        base = os.path.join(DATA_DIR, device_id)
    if not os.path.exists(base):
        return records

    for root, dirs, files in os.walk(base):
        for f in sorted(files):
            if not f.endswith(".json"):
                continue
            path = os.path.join(root, f)
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    r = json.load(fh)
            except Exception:
                continue

            received = r.get("received_at", "")

            # Filter by date (folder name or received_at)
            if date:
                folder_date = os.path.basename(root)
                if folder_date != date and not received.startswith(date):
                    continue

            # Filter by time range (HH:MM or HH:MM:SS)
            if start_time or end_time:
                t = received[11:19]  # HH:MM:SS
                if start_time and t < start_time:
                    continue
                if end_time and t > end_time:
                    continue

            records.append(r)

    records.sort(key=lambda x: x.get("received_at", ""))
    return records


def _stats(vals):
    if not vals:
        return "N/A", "N/A", "N/A"
    return f"{min(vals):.2f}", f"{max(vals):.2f}", f"{sum(vals)/len(vals):.2f}"


def generate_pdf(device_id: str = None, date: str = None,
                 start_time: str = None, end_time: str = None,
                 output_path: str = None) -> str:
    """Generate a PDF report with optional filters. Returns output path."""
    records = load_records(device_id, date, start_time, end_time)
    if not records:
        logger.warning("No records found for the given filters.")
        return None

    if not output_path:
        os.makedirs(REPORTS_DIR, exist_ok=True)
        label = device_id or "all"
        day = date or datetime.now().strftime("%Y-%m-%d")
        output_path = os.path.join(REPORTS_DIR, f"report_{label}_{day}.pdf")

    devices = {}
    for r in records:
        did = r.get("device_id", "unknown")
        devices.setdefault(did, []).append(r)

    host = device_id or (records[0].get("device_id") or "all")
    doc = SimpleDocTemplate(output_path, pagesize=A4, title=host, author=host)
    styles = getSampleStyleSheet()
    elements = []

    title_style = ParagraphStyle("Title", parent=styles["Heading1"],
                                 fontSize=20, spaceAfter=20, alignment=1)
    elements.append(Paragraph("IoT Sensor Report", title_style))

    filter_parts = []
    if device_id:
        filter_parts.append(f"Device: {device_id}")
    if date:
        filter_parts.append(f"Date: {date}")
    if start_time or end_time:
        filter_parts.append(f"Time: {start_time or '00:00:00'} - {end_time or '23:59:59'}")
    filter_text = " | ".join(filter_parts) if filter_parts else "All data"
    elements.append(Paragraph(filter_text, styles["Normal"]))
    elements.append(Paragraph(
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        styles["Normal"]))
    elements.append(Spacer(1, 20))

    for did, recs in devices.items():
        t1, t2, t3, t4, bats, times = [], [], [], [], [], []
        for r in recs:
            p = r.get("payload", {})
            if p.get("sensor_1") is not None: t1.append(p["sensor_1"])
            if p.get("sensor_2") is not None: t2.append(p["sensor_2"])
            if p.get("sensor_3") is not None: t3.append(p["sensor_3"])
            if p.get("sensor_4") is not None: t4.append(p["sensor_4"])
            if p.get("bat_pct") is not None: bats.append(p["bat_pct"])
            times.append(r.get("received_at", ""))

        elements.append(Paragraph(f"Device: {did}", styles["Heading2"]))
        elements.append(Paragraph(
            f"Readings: {len(recs)} | "
            f"Period: {times[0][:19]} to {times[-1][:19]}",
            styles["Normal"]))
        elements.append(Spacer(1, 10))

        m1, x1, a1 = _stats(t1)
        m2, x2, a2 = _stats(t2)
        m3, x3, a3 = _stats(t3)
        m4, x4, a4 = _stats(t4)
        mb, xb, ab = _stats(bats)
        all_t = t1 + t2 + t3 + t4
        ma, xa, aa = _stats(all_t)

        table_data = [
            ["Sensor", "Min", "Max", "Avg"],
            ["Sensor 1 (C)", m1, x1, a1],
            ["Sensor 2 (C)", m2, x2, a2],
            ["Sensor 3 (C)", m3, x3, a3],
            ["Sensor 4 (C)", m4, x4, a4],
            ["All Temps (C)", ma, xa, aa],
            ["Battery (%)", mb, xb, ab],
        ]

        table = Table(table_data, colWidths=[130, 90, 90, 90])
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#4CAF50")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 11),
            ("ALIGN", (1, 0), (-1, -1), "CENTER"),
            ("ALIGN", (0, 0), (0, -1), "LEFT"),
            ("GRID", (0, 0), (-1, -1), 1, colors.grey),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1),
             [colors.white, colors.HexColor("#F5F5F5")]),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, -1), 8),
        ]))
        elements.append(table)
        elements.append(Spacer(1, 20))

    elements.append(Spacer(1, 10))
    elements.append(Paragraph(f"Total records: {len(records)}", styles["Normal"]))

    doc.build(elements)
    logger.info(f"PDF saved: {output_path}")
    return output_path


def generate_daily_report():
    """Generate a PDF for every device that has data (yesterday and today)."""
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    today = datetime.now().strftime("%Y-%m-%d")

    # Find all devices
    devices = []
    if os.path.exists(DATA_DIR):
        for d in os.listdir(DATA_DIR):
            if os.path.isdir(os.path.join(DATA_DIR, d)) and d != "reports":
                devices.append(d)

    generated = []
    for did in devices:
        # Generate for every day that has data
        device_dir = os.path.join(DATA_DIR, did)
        days = sorted([d for d in os.listdir(device_dir)
                       if os.path.isdir(os.path.join(device_dir, d))], reverse=True)
        for day in days:
            day_dir = os.path.join(device_dir, day)
            if os.path.isdir(day_dir) and any(f.endswith(".json") for f in os.listdir(day_dir)):
                path = generate_pdf(device_id=did, date=day)
                if path:
                    generated.append(path)

    if not generated:
        logger.info("Daily report: no data found.")
    return generated
