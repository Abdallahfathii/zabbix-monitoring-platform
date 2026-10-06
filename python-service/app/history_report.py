import logging
import io
import math
import os
from datetime import datetime, timedelta
from collections import OrderedDict

from sqlalchemy import text
from sqlalchemy.orm import Session
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, PageBreak, Image
)
from reportlab.graphics.shapes import Drawing, Line, String, Rect
from reportlab.graphics.charts.lineplots import LinePlot
from reportlab.graphics.charts.legends import Legend

logger = logging.getLogger(__name__)

TEMP_MIN_LIMIT = 2.0
TEMP_MAX_LIMIT = 8.0
BATTERY_MIN_LIMIT = 20.0
BATTERY_MAX_LIMIT = 100.0

# MKT constants
MKT_DH = 83144.0  # J/mol
MKT_R = 8.3144

TEAL = colors.HexColor("#1F5C6B")
RED = colors.HexColor("#C00000")
GREEN = colors.HexColor("#2E7D32")
LIGHT_RED = colors.HexColor("#FDECEA")
LIGHT_GRAY = colors.HexColor("#F5F5F5")

# Company logo shown in the top-left corner of the report
LOGO_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "assets", "zeta_logo.png"
)
LOGO_RATIO = 1483 / 1061
LOGO_WIDTH = 42 * mm
HEADER_GAP = 6  # points, gap between logo and the header text


def mean_kinetic_temperature(temps_c):
    if not temps_c:
        return None
    s = sum(math.exp(-MKT_DH / (MKT_R * (t + 273.15))) for t in temps_c) / len(temps_c)
    return (MKT_DH / MKT_R) / (-math.log(s)) - 273.15


def _fmt_duration(delta: timedelta) -> str:
    total = int(delta.total_seconds())
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    if days:
        return f"{days} days {hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


HISTORY_SAMPLE_MINUTES = 15


def load_history_rows(db: Session, device_id: str, start_date: str, end_date: str,
                      sample_minutes: int = HISTORY_SAMPLE_MINUTES):
    """Return pivoted rows: [{time, temperature, battery, sensor_1..4}, ...] for date range.

    Readings are downsampled to one complete reading per `sample_minutes`
    (first reading inside each 15-minute bucket). Pass sample_minutes=0 for raw rows.
    """
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)

    params = {"dev": device_id, "start": start, "end": end}
    if sample_minutes and sample_minutes > 0:
        result = db.execute(text(f"""
            SELECT received_at, key_name, value
            FROM history
            WHERE device_id = :dev
              AND received_at >= :start
              AND received_at < :end
              AND received_at IN (
                  SELECT min(received_at)
                  FROM history
                  WHERE device_id = :dev
                    AND received_at >= :start
                    AND received_at < :end
                  GROUP BY date_trunc('hour', received_at)
                         + interval '1 minute' * :step
                               * floor(extract(minute from received_at) / :step)
              )
            ORDER BY received_at, key_name
        """), {**params, "step": sample_minutes}).fetchall()
    else:
        result = db.execute(text("""
            SELECT received_at, key_name, value
            FROM history
            WHERE device_id = :dev
              AND received_at >= :start
              AND received_at < :end
            ORDER BY received_at, key_name
        """), params).fetchall()

    by_time = OrderedDict()
    for ts, key, val in result:
        bucket = by_time.setdefault(ts, {
            "time": ts,
            "temperature": None,
            "battery": None,
            "sensor_1": None,
            "sensor_2": None,
            "sensor_3": None,
            "sensor_4": None,
        })
        if key in bucket:
            bucket[key] = float(val) if val is not None else None

    return list(by_time.values())


def get_device_limits(db: Session, device_id: str):
    """Alarm limits published by the device over MQTT, with the module defaults
    as a fallback for devices that have never reported them."""
    row = db.execute(text(
        "SELECT min_temp, max_temp FROM sensor_summary WHERE device_id = :d"
    ), {"d": device_id}).fetchone()
    t_min = row[0] if row and row[0] is not None else TEMP_MIN_LIMIT
    t_max = row[1] if row and row[1] is not None else TEMP_MAX_LIMIT
    return t_min, t_max


def get_device_info(db: Session, device_id: str) -> dict:
    row = db.execute(text(
        "SELECT name, location FROM devices WHERE device_id = :d"
    ), {"d": device_id}).fetchone()
    return {
        "device_id": device_id,
        "name": row[0] if row else device_id,
        "location": row[1] if row and row[1] else "-",
    }


def _stats(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return None, None, None, 0
    return min(vals), max(vals), sum(vals) / len(vals), len(vals)


def _excursion_analysis(points, min_limit, max_limit):
    """points: list of (datetime, value). Returns alarm details dict."""
    high_events = 0
    low_events = 0
    excursion_pts = 0
    peak_breach = None   # highest value above max
    lowest_breach = None  # lowest value below min
    in_high = False
    in_low = False
    first_exc = None
    last_exc = None
    prev_exc = None
    duration_sec = 0.0

    for ts, v in points:
        if v is None:
            continue
        is_high = v > max_limit
        is_low = v < min_limit
        if is_high or is_low:
            excursion_pts += 1
            if first_exc is None:
                first_exc = ts
            last_exc = ts
            if prev_exc is not None:
                duration_sec += (ts - prev_exc).total_seconds()
            else:
                # estimate one interval later from first gap if available
                pass
            prev_exc = ts

            if is_high:
                peak_breach = v if peak_breach is None else max(peak_breach, v)
                if not in_high:
                    high_events += 1
                    in_high = True
                in_low = False
            if is_low:
                lowest_breach = v if lowest_breach is None else min(lowest_breach, v)
                if not in_low:
                    low_events += 1
                    in_low = True
                in_high = False
        else:
            in_high = False
            in_low = False

    # Add final gap estimate: if only 1 point, use 0 duration
    est_duration = ""
    if first_exc and last_exc and duration_sec == 0 and first_exc != last_exc:
        duration_sec = (last_exc - first_exc).total_seconds()
    if first_exc and last_exc:
        est_duration = _fmt_duration(timedelta(seconds=duration_sec))
    elif first_exc:
        est_duration = "00:00:00"

    return {
        "high_events": high_events,
        "low_events": low_events,
        "excursion_points": excursion_pts,
        "est_duration": est_duration or "-",
        "peak_breach": peak_breach,
        "lowest_breach": lowest_breach,
    }


def _median_interval(rows):
    times = [r["time"] for r in rows]
    if len(times) < 2:
        return None
    deltas = [(times[i + 1] - times[i]).total_seconds() for i in range(len(times) - 1)]
    deltas = [d for d in deltas if d > 0]
    if not deltas:
        return None
    deltas.sort()
    mid = len(deltas) // 2
    if len(deltas) % 2 == 0:
        return (deltas[mid - 1] + deltas[mid]) / 2
    return deltas[mid]


def _fmt_interval(seconds) -> str:
    if seconds is None:
        return "-"
    m, s = divmod(int(round(seconds)), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m} min"
    return f"{s}s"


def _build_trend_chart(rows, temp_min, temp_max):
    """Build Drawing with Temperature + Battery trend lines and temp limit lines."""
    width, height = 480, 220
    d = Drawing(width, height)

    t0 = rows[0]["time"]
    # x as hours since start for nicer axis
    xs_temp, ys_temp = [], []
    xs_bat, ys_bat = [], []
    for r in rows:
        x = (r["time"] - t0).total_seconds() / 3600.0
        if r["temperature"] is not None:
            xs_temp.append(x)
            ys_temp.append(r["temperature"])
        if r["battery"] is not None:
            xs_bat.append(x)
            ys_bat.append(r["battery"])

    series = []
    if xs_temp:
        series.append(list(zip(xs_temp, ys_temp)))
    if xs_bat:
        series.append(list(zip(xs_bat, ys_bat)))
    # limit lines as constant series
    if xs_temp:
        series.append([(xs_temp[0], temp_min), (xs_temp[-1], temp_min)])
        series.append([(xs_temp[0], temp_max), (xs_temp[-1], temp_max)])

    if not series:
        d.add(String(10, height // 2, "No data for chart", fontSize=12, fillColor=colors.grey))
        return d

    plot = LinePlot()
    plot.x = 55
    plot.y = 35
    plot.width = width - 75
    plot.height = height - 75
    plot.data = series

    # y-range: include limits and data
    all_y = ys_temp + ys_bat + [temp_min, temp_max]
    plot.yValueAxis.valueMin = min(all_y) - 1
    plot.yValueAxis.valueMax = max(all_y) + 1
    plot.yValueAxis.valueStep = max(1, (max(all_y) - min(all_y)) / 5) if all_y else 1
    plot.yValueAxis.labelTextFormat = "%.1f"
    plot.yValueAxis.gridStrokeColor = colors.lightgrey
    plot.yValueAxis.gridStrokeWidth = 0.5

    x_max = max(xs_temp + xs_bat) if (xs_temp or xs_bat) else 1
    plot.xValueAxis.valueMin = 0
    plot.xValueAxis.valueMax = max(x_max, 0.1)
    plot.xValueAxis.labelTextFormat = lambda v: f"{t0 + timedelta(hours=v):%d/%m %H:%M}"
    plot.xValueAxis.gridStrokeColor = colors.lightgrey
    plot.xValueAxis.gridStrokeWidth = 0.5
    plot.xValueAxis.labels.angle = 0
    plot.xValueAxis.labels.fontSize = 7

    # style series
    n = len(series)
    if n >= 1:
        plot.lines[0].strokeColor = colors.red
        plot.lines[0].strokeWidth = 1.5
    if n >= 2:
        plot.lines[1].strokeColor = colors.HexColor("#2196F3")
        plot.lines[1].strokeWidth = 1.5
    if n >= 3:
        plot.lines[2].strokeColor = colors.red
        plot.lines[2].strokeWidth = 0.8
        plot.lines[2].strokeDashArray = [2, 2]
    if n >= 4:
        plot.lines[3].strokeColor = colors.red
        plot.lines[3].strokeWidth = 0.8
        plot.lines[3].strokeDashArray = [2, 2]

    d.add(plot)

    # legend
    legend = Legend()
    legend.x = width - 150
    legend.y = height - 20
    legend.fontSize = 8
    legend.colorNamePairs = [
        (colors.red, "Temperature"),
        (colors.HexColor("#2196F3"), "Battery"),
    ]
    d.add(legend)

    # axis labels
    d.add(String(15, height / 2, "Value", fontSize=9, fillColor=colors.black, angle=90))
    return d


def _header_table(device_info, start_date, end_date, points, interval_s, time_range_text):
    trip = ""
    if points >= 2 and interval_s:
        trip = _fmt_duration(timedelta(seconds=interval_s * (points - 1)))
    elif points == 1:
        trip = "00:00:00"

    data = [
        ["Device Host", device_info["device_id"], "Location", device_info["location"], "Trip Duration", trip or "-"],
        ["Logging Interval", _fmt_interval(interval_s), "Logged Points", str(points), "Time Range", time_range_text],
        ["Monitored Probes", "Temperature, Battery, Sensor 1-4", "", "", "", ""],
    ]
    # merge last row cells visually via col widths
    t = Table(data, colWidths=[70, 85, 65, 85, 70, 130])
    t.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("BACKGROUND", (0, 0), (0, -1), LIGHT_GRAY),
        ("BACKGROUND", (2, 0), (2, -1), LIGHT_GRAY),
        ("BACKGROUND", (4, 0), (4, -1), LIGHT_GRAY),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTNAME", (2, 0), (2, -1), "Helvetica-Bold"),
        ("FONTNAME", (4, 0), (4, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("SPAN", (1, 2), (5, 2)),
    ]))
    return t


def _report_header(printed_by, title_style, normal):
    """Logo in the top-left corner, report title with the meta line under it."""
    safe_name = (printed_by or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    meta_txt = f"Printed By: {safe_name} | Report Time: {datetime.now():%Y-%m-%d %H:%M:%S}"

    right = [
        Paragraph("Zetalogger Monitoring Report", title_style),
        Paragraph(meta_txt, normal),
    ]
    if not os.path.exists(LOGO_PATH):
        logger.warning("Report logo not found at %s", LOGO_PATH)
        return right

    logo = Image(
        LOGO_PATH, width=LOGO_WIDTH,
        height=LOGO_WIDTH / LOGO_RATIO, mask="auto"
    )
    return [Table(
        [[logo, right]],
        colWidths=[LOGO_WIDTH, 180 * mm - LOGO_WIDTH],
        style=TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (0, 0), 0),
            ("RIGHTPADDING", (0, 0), (0, 0), 0),
            ("LEFTPADDING", (1, 0), (1, 0), HEADER_GAP),
            ("RIGHTPADDING", (1, 0), (1, 0), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ])
    )]


def _summary_table(items_stats):
    """items_stats: list of dicts with item, mn, mx, avg, min_lim, max_lim, points, alarm"""
    header = ["Item", "Max", "Min", "Average", "Min Limit", "Max Limit", "Points", "Alarm Status"]
    data = [header]
    for s in items_stats:
        alarm = "ALARM" if s["alarm"] else "OK"
        data.append([
            s["item"],
            f"{s['mx']:.1f}" if s["mx"] is not None else "-",
            f"{s['mn']:.1f}" if s["mn"] is not None else "-",
            f"{s['avg']:.1f}" if s["avg"] is not None else "-",
            f"{s['min_lim']:.1f}" if s["min_lim"] is not None else "-",
            f"{s['max_lim']:.1f}" if s["max_lim"] is not None else "-",
            str(s["points"]),
            alarm,
        ])

    t = Table(data, colWidths=[75, 50, 50, 55, 55, 55, 45, 65])
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), TEAL),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("ALIGN", (1, 0), (-1, -1), "CENTER"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT_GRAY]),
    ]
    for i, s in enumerate(items_stats, start=1):
        if s["alarm"]:
            style.append(("TEXTCOLOR", (7, i), (7, i), RED))
            style.append(("FONTNAME", (7, i), (7, i), "Helvetica-Bold"))
        else:
            style.append(("TEXTCOLOR", (7, i), (7, i), GREEN))
            style.append(("FONTNAME", (7, i), (7, i), "Helvetica-Bold"))
    t.setStyle(TableStyle(style))
    return t


def _alarm_details_table(details_rows):
    header = ["Item", "High Events", "Low Events", "Excursion Points", "Est. Duration", "Peak Breach", "Lowest Breach"]
    data = [header]
    for d in details_rows:
        data.append([
            d["item"],
            str(d["high_events"]),
            str(d["low_events"]),
            str(d["excursion_points"]),
            d["est_duration"],
            f"{d['peak_breach']:.1f}" if d["peak_breach"] is not None else "-",
            f"{d['lowest_breach']:.1f}" if d["lowest_breach"] is not None else "-",
        ])
    t = Table(data, colWidths=[75, 60, 55, 75, 70, 60, 65])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), RED),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("ALIGN", (1, 0), (-1, -1), "CENTER"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT_RED]),
    ]))
    return t


def _alarmed_data_table(excursion_rows, temp_min=TEMP_MIN_LIMIT, temp_max=TEMP_MAX_LIMIT):
    """Rows where any monitored value breached limits. Columns: Date Time Temperature Battery"""
    header = ["Date", "Time", "Temperature", "Battery"]
    data = [header]
    styles_extra = []
    for i, r in enumerate(excursion_rows, start=1):
        t_str = r["time"].strftime("%d/%m/%Y")
        time_str = r["time"].strftime("%H:%M:%S")
        temp = r["temperature"]
        bat = r["battery"]
        temp_s = f"{temp:.1f}" if temp is not None else "-"
        bat_s = f"{bat:.1f}" if bat is not None else "-"
        data.append([t_str, time_str, temp_s, bat_s])
        if temp is not None and (temp > temp_max or temp < temp_min):
            styles_extra.append(("TEXTCOLOR", (2, i), (2, i), RED))
            styles_extra.append(("FONTNAME", (2, i), (2, i), "Helvetica-Bold"))
        if bat is not None and bat < BATTERY_MIN_LIMIT:
            styles_extra.append(("TEXTCOLOR", (3, i), (3, i), RED))
            styles_extra.append(("FONTNAME", (3, i), (3, i), "Helvetica-Bold"))

    t = Table(data, colWidths=[80, 70, 80, 70])
    base = [
        ("BACKGROUND", (0, 0), (-1, 0), RED),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("ALIGN", (1, 0), (-1, -1), "CENTER"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]
    t.setStyle(TableStyle(base + styles_extra))
    return t


def _mkt_table(mkt_val):
    data = [
        ["Item", "MKT (°C)"],
        ["Temperature", f"{mkt_val:.1f}" if mkt_val is not None else "-"],
    ]
    t = Table(data, colWidths=[120, 80])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), TEAL),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("BACKGROUND", (0, 1), (-1, 1), LIGHT_GRAY),
    ]))
    # center the table by wrapping - caller adds spacer
    return t


def _data_table_two_col(rows):
    """Full data table in two side-by-side column groups like the sample."""
    left_n = (len(rows) + 1) // 2
    left = rows[:left_n]
    right = rows[left_n:]

    header = ["Date", "Time", "Temperature", "Battery", "Date", "Time", "Temperature", "Battery"]
    data = [header]
    max_len = max(len(left), len(right))
    for i in range(max_len):
        row = []
        if i < len(left):
            r = left[i]
            row.extend([
                r["time"].strftime("%d/%m/%Y"),
                r["time"].strftime("%H:%M:%S"),
                f"{r['temperature']:.1f}" if r["temperature"] is not None else "-",
                f"{r['battery']:.1f}" if r["battery"] is not None else "-",
            ])
        else:
            row.extend(["", "", "", ""])
        if i < len(right):
            r = right[i]
            row.extend([
                r["time"].strftime("%d/%m/%Y"),
                r["time"].strftime("%H:%M:%S"),
                f"{r['temperature']:.1f}" if r["temperature"] is not None else "-",
                f"{r['battery']:.1f}" if r["battery"] is not None else "-",
            ])
        else:
            row.extend(["", "", "", ""])
        data.append(row)

    col_w = [58, 50, 58, 45, 58, 50, 58, 45]
    t = Table(data, colWidths=col_w, repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (3, 0), TEAL),
        ("BACKGROUND", (4, 0), (7, 0), TEAL),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 7),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LINEAFTER", (3, 0), (3, -1), 1.5, colors.black),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT_GRAY]),
    ]
    t.setStyle(TableStyle(style))
    return t


def generate_history_pdf(db: Session, device_id: str, start_date: str, end_date: str,
                         printed_by: str = "Admin") -> str:
    """Generate Zeta-style PDF report from monitoring.history for a date range."""
    rows = load_history_rows(db, device_id, start_date, end_date)
    if not rows:
        logger.warning("No history for %s %s..%s", device_id, start_date, end_date)
        return None

    device_info = get_device_info(db, device_id)
    # Limits the device publishes over MQTT, not the module-level constants.
    temp_min_limit, temp_max_limit = get_device_limits(db, device_id)
    interval_s = _median_interval(rows)
    points = len(rows)
    t_first = rows[0]["time"]
    t_last = rows[-1]["time"]
    time_range = f"{t_first:%d/%m/%Y %H:%M} - {t_last:%d/%m/%Y %H:%M}"

    temps = [r["temperature"] for r in rows if r["temperature"] is not None]
    bats = [r["battery"] for r in rows if r["battery"] is not None]
    s1 = [r["sensor_1"] for r in rows if r["sensor_1"] is not None]
    s2 = [r["sensor_2"] for r in rows if r["sensor_2"] is not None]
    s3 = [r["sensor_3"] for r in rows if r["sensor_3"] is not None]
    s4 = [r["sensor_4"] for r in rows if r["sensor_4"] is not None]

    # Per-item stats + alarm flags
    def item_stat(name, vals, min_lim, max_lim):
        mn, mx, avg, pts = _stats(vals)
        alarm = False
        if min_lim is not None and any(v < min_lim for v in vals if v is not None):
            alarm = True
        if max_lim is not None and any(v > max_lim for v in vals if v is not None):
            alarm = True
        return {
            "item": name, "mn": mn, "mx": mx, "avg": avg,
            "min_lim": min_lim, "max_lim": max_lim, "points": pts, "alarm": alarm,
        }

    items_stats = [
        item_stat("Temperature", temps, temp_min_limit, temp_max_limit),
        item_stat("Battery", bats, BATTERY_MIN_LIMIT, BATTERY_MAX_LIMIT),
        item_stat("Sensor 1", s1, temp_min_limit, temp_max_limit),
        item_stat("Sensor 2", s2, temp_min_limit, temp_max_limit),
        item_stat("Sensor 3", s3, temp_min_limit, temp_max_limit),
        item_stat("Sensor 4", s4, temp_min_limit, temp_max_limit),
    ]

    # Alarm details for Temperature + Battery (main probes like sample)
    temp_points = [(r["time"], r["temperature"]) for r in rows]
    bat_points = [(r["time"], r["battery"]) for r in rows]
    temp_alarm = _excursion_analysis(temp_points, temp_min_limit, temp_max_limit)
    bat_alarm = _excursion_analysis(bat_points, BATTERY_MIN_LIMIT, BATTERY_MAX_LIMIT)
    details_rows = [
        {"item": "Temperature", **temp_alarm},
        {"item": "Battery", **bat_alarm},
    ]

    # Excursion rows: any breach of temp or battery limits
    excursion_rows = []
    for r in rows:
        t_v = r["temperature"]
        b_v = r["battery"]
        temp_bad = t_v is not None and (t_v > temp_max_limit or t_v < temp_min_limit)
        bat_bad = b_v is not None and b_v < BATTERY_MIN_LIMIT
        if temp_bad or bat_bad:
            excursion_rows.append(r)

    mkt_val = mean_kinetic_temperature(temps)

    d1 = datetime.strptime(start_date, "%Y-%m-%d")
    d2 = datetime.strptime(end_date, "%Y-%m-%d")
    label = device_info["name"].replace(" ", "_")
    filename = (f"{label} {d1.day}-{d1.month} - {d2.day}-{d2.month} "
                f"{d2.year}_Report.pdf")

    # The PDF is built in memory - nothing is written to disk.
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        leftMargin=15 * mm, rightMargin=15 * mm,
        topMargin=12 * mm, bottomMargin=12 * mm,
        title=device_id,
        author=device_id,
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "RTitle", parent=styles["Heading1"], fontSize=16, spaceAfter=4,
        textColor=TEAL, alignment=0,
    )
    h2_style = ParagraphStyle(
        "RH2", parent=styles["Heading2"], fontSize=13, spaceBefore=14,
        spaceAfter=6, textColor=colors.black,
    )
    normal = styles["Normal"]
    normal.fontSize = 9

    elements = []

    # Header: logo top-left, report title and meta next to it
    elements.extend(_report_header(printed_by, title_style, normal))
    elements.append(Spacer(1, 10))

    # Device Information
    elements.append(Paragraph("Device Information", h2_style))
    elements.append(_header_table(
        device_info, start_date, end_date, points, interval_s, time_range
    ))
    elements.append(Spacer(1, 12))

    # Trend Chart
    elements.append(Paragraph("Trend Chart", h2_style))
    elements.append(_build_trend_chart(rows, temp_min_limit, temp_max_limit))
    elements.append(Spacer(1, 8))

    # Logging Summary
    elements.append(Paragraph("Logging Summary", h2_style))
    elements.append(_summary_table(items_stats))
    elements.append(Spacer(1, 12))

    # Alarm Details Summary
    elements.append(Paragraph("Alarm Details Summary", h2_style))
    elements.append(_alarm_details_table(details_rows))
    elements.append(Spacer(1, 12))

    # Alarmed Data Table
    elements.append(Paragraph("Alarmed Data Table - Excursions Only", h2_style))
    if excursion_rows:
        # limit to reasonable display; full list if small
        shown = excursion_rows[:50]
        elements.append(_alarmed_data_table(shown, temp_min_limit, temp_max_limit))
        if len(excursion_rows) > 50:
            elements.append(Paragraph(
                f"... and {len(excursion_rows) - 50} more excursion points",
                normal
            ))
    else:
        elements.append(Paragraph("No excursions in the selected period.", normal))
    elements.append(Spacer(1, 12))

    # MKT
    elements.append(Paragraph("Mean Kinetic Temperature", h2_style))
    # center-ish: left align like sample (sample is centered but left is fine on A4)
    elements.append(_mkt_table(mkt_val))

    # Data Table on new page
    elements.append(PageBreak())
    elements.append(Paragraph("Data Table", h2_style))
    elements.append(_data_table_two_col(rows))
    elements.append(Spacer(1, 8))
    elements.append(Paragraph(
        f"Total readings: {points} &nbsp;|&nbsp; Device: {device_id} "
        f"&nbsp;|&nbsp; Range: {start_date} to {end_date}",
        normal
    ))

    doc.build(elements)
    logger.info("History PDF generated in memory: %s (%s points)", filename, points)
    return buffer.getvalue(), filename


def list_devices_with_history(db: Session):
    result = db.execute(text(
        "SELECT DISTINCT device_id FROM history ORDER BY device_id"
    ))
    return [r[0] for r in result.fetchall()]
