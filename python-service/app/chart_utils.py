import logging
import json
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from sqlalchemy import text

logger = logging.getLogger(__name__)

# Chart configuration
CHART_WIDTH = 400
CHART_HEIGHT = 250
CHART_COLORS = {
    'min_temp': '#2196F3',      # Blue
    'max_temp': '#F44336',      # Red
    'avg_temp': '#4CAF50',      # Green
    'min_humidity': '#2196F3',
    'max_humidity': '#F44336',
    'avg_humidity': '#4CAF50',
    'min_pressure': '#2196F3',
    'max_pressure': '#F44336',
    'avg_pressure': '#4CAF50',
    'min_battery': '#2196F3',
    'max_battery': '#F44336',
    'avg_battery': '#4CAF50',
}

LINE_WIDTH = 2
GRID_COLOR = '#e0e0e0'
AXIS_COLOR = '#666666'
FONT_SIZE = 10


def get_sensor_stats_for_chart(db: Session, device_id: str, hours: int = 24):
    """Get min/max/avg statistics for sensors over specified time period."""
    # measurements.received_at is stored in the app/DB local timezone (TZ=Africa/Cairo),
    # so the window must be built from local time. utcnow() would sit 3h behind and
    # silently drop the newest readings from every chart.
    db_tz = ZoneInfo("Africa/Cairo")
    end_time = datetime.now(db_tz)
    start_time = end_time - timedelta(hours=hours)

    query = text("""
        SELECT
            DATE_TRUNC('hour', received_at) as hour,
            MIN(temperature) as min_temp,
            MAX(temperature) as max_temp,
            AVG(temperature) as avg_temp,
            MIN(humidity) as min_humidity,
            MAX(humidity) as max_humidity,
            AVG(humidity) as avg_humidity,
            MIN(pressure) as min_pressure,
            MAX(pressure) as max_pressure,
            AVG(pressure) as avg_pressure,
            MIN(battery) as min_battery,
            MAX(battery) as max_battery,
            AVG(battery) as avg_battery
        FROM measurements
        WHERE device_id = :device_id
            AND received_at >= :start_time
            AND received_at <= :end_time
        GROUP BY DATE_TRUNC('hour', received_at)
        ORDER BY hour
    """)

    result = db.execute(query, {
        'device_id': device_id,
        'start_time': start_time,
        'end_time': end_time
    })

    data = []
    for row in result.fetchall():
        data.append({
            'hour': row[0],
            'min_temp': float(row[1]) if row[1] else None,
            'max_temp': float(row[2]) if row[2] else None,
            'avg_temp': float(row[3]) if row[3] else None,
            'min_humidity': float(row[4]) if row[4] else None,
            'max_humidity': float(row[5]) if row[5] else None,
            'avg_humidity': float(row[6]) if row[6] else None,
            'min_pressure': float(row[7]) if row[7] else None,
            'max_pressure': float(row[8]) if row[8] else None,
            'avg_pressure': float(row[9]) if row[9] else None,
            'min_battery': float(row[10]) if row[10] else None,
            'max_battery': float(row[11]) if row[11] else None,
            'avg_battery': float(row[12]) if row[12] else None,
        })

    return data


def generate_chart_data_for_device(device_id: str, db: Session, hours: int = 24):
    """Generate comprehensive chart data for a single device."""
    stats_data = get_sensor_stats_for_chart(db, device_id, hours)

    if not stats_data:
        return None

    # Extract time labels and series data
    chart_data = {
        'labels': [],
        'datasets': []
    }

    for entry in stats_data:
        chart_data['labels'].append(entry['hour'].strftime('%H:%M'))

    # Define chart series
    series_config = [
        ('Temperature', 'min_temp', 'max_temp', 'avg_temp', True),
        ('Humidity', 'min_humidity', 'max_humidity', 'avg_humidity', True),
        ('Pressure', 'min_pressure', 'max_pressure', 'avg_pressure', True),
        ('Battery', 'min_battery', 'max_battery', 'avg_battery', False),
    ]

    for name, min_key, max_key, avg_key, show_in_legend in series_config:
        min_values = [e.get(min_key) for e in stats_data]
        max_values = [e.get(max_key) for e in stats_data]
        avg_values = [e.get(avg_key) for e in stats_data]

        # Create dataset
        dataset = {
            'label': f'{name} Min',
            'data': min_values,
            'borderColor': CHART_COLORS.get(f'{min_key}'),
            'backgroundColor': f"{CHART_COLORS.get(f'{min_key}')}20",  # Semi-transparent
            'borderWidth': LINE_WIDTH,
            'fill': False,
            'pointRadius': 2,
            'pointHoverRadius': 4,
            'tension': 0.1,
            'showInLegend': show_in_legend
        }
        chart_data['datasets'].append(dataset)

        dataset_max = {
            'label': f'{name} Max',
            'data': max_values,
            'borderColor': CHART_COLORS.get(f'{max_key}'),
            'backgroundColor': 'transparent',
            'borderWidth': LINE_WIDTH,
            'borderDash': [5, 5],  # Dashed line for max
            'fill': False,
            'pointRadius': 2,
            'pointHoverRadius': 4,
            'tension': 0.1,
            'showInLegend': show_in_legend
        }
        chart_data['datasets'].append(dataset_max)

        dataset_avg = {
            'label': f'{name} Avg',
            'data': avg_values,
            'borderColor': CHART_COLORS.get(f'{avg_key}'),
            'backgroundColor': 'transparent',
            'borderWidth': 1.5,
            'borderDash': [],  # Solid line for avg
            'fill': False,
            'pointRadius': 0,  # No points for avg
            'tension': 0.1,
            'showInLegend': show_in_legend
        }
        chart_data['datasets'].append(dataset_avg)

    return chart_data


def generate_html_chart_widget(chart_data, device_id, chart_type='line'):
    """Generate HTML for chart widget using Chart.js."""
    if not chart_data:
        return f'<div class="chart-container"><p>No data available for {device_id}</p></div>'

    # Prepare Chart.js configuration
    chart_config = {
        'type': chart_type,
        'data': {
            'labels': chart_data['labels'],
            'datasets': chart_data['datasets']
        },
        'options': {
            'responsive': True,
            'maintainAspectRatio': False,
            'interaction': {
                'mode': 'index',
                'intersect': False
            },
            'plugins': {
                'title': {
                    'display': True,
                    'text': f'Sensor Statistics - {device_id} (Last 24h)',
                    'font': {
                        'size': 14,
                        'weight': 'bold'
                    }
                },
                'legend': {
                    'display': True,
                    'position': 'top',
                    'labels': {
                        'font': {
                            'size': 11
                        },
                        'padding': 10
                    }
                },
                'tooltip': {
                    'backgroundColor': 'rgba(0, 0, 0, 0.8)',
                    'titleFontSize': 12,
                    'bodyFontSize': 11,
                    'padding': 10,
                    'cornerRadius': 6
                }
            },
            'scales': {
                'x': {
                    'title': {
                        'display': True,
                        'text': 'Time (Hour:Minute)',
                        'font': {
                            'size': 12,
                            'weight': 'bold'
                        }
                    },
                    'grid': {
                        'color': GRID_COLOR,
                        'drawBorder': False
                    },
                    'ticks': {
                        'font': {
                            'size': 10
                        },
                        'maxRotation': 45,
                        'minRotation': 45
                    }
                },
                'y': {
                    'title': {
                        'display': True,
                        'text': 'Value',
                        'font': {
                            'size': 12,
                            'weight': 'bold'
                        }
                    },
                    'grid': {
                        'color': GRID_COLOR,
                        'drawBorder': False
                    },
                    'ticks': {
                        'font': {
                            'size': 10
                        }
                    }
                }
            },
            'elements': {
                'line': {
                    'tension': 0.4
                }
            }
        }
    }

    # Convert config to JSON string
    import json
    config_json = json.dumps(chart_config, default=str)

    html = f"""
    <div class="chart-container" style="position: relative; height: {CHART_HEIGHT}px; width: 100%;">
        <canvas id="chart-{device_id}"></canvas>
        <script>
            // Wait for DOM to be ready
            document.addEventListener('DOMContentLoaded', function() {{
                const ctx = document.getElementById('chart-{device_id}').getContext('2d');

                // Chart.js configuration
                const config = {config_json};

                // Initialize chart
                new Chart(ctx, config);
            }});
        </script>
    </div>
    """
    return html


def get_all_devices_chart_summary(db: Session, hours: int = 24):
    """Get chart summary data for all devices."""
    # Get all devices with recent activity
    devices_query = text("""
        SELECT DISTINCT device_id
        FROM measurements
        WHERE received_at >= :start_time
        ORDER BY device_id
    """)

    start_time = datetime.now() - timedelta(hours=hours)
    result = db.execute(devices_query, {'start_time': start_time})
    devices = [row[0] for row in result.fetchall()]

    summary = {}
    for device_id in devices:
        try:
            chart_data = generate_chart_data_for_device(device_id, db, hours)
            if chart_data:
                summary[device_id] = chart_data
        except Exception as e:
            logger.error(f"Failed to generate chart data for device {device_id}: {e}")
            continue

    return summary