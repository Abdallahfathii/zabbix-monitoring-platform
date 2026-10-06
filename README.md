# Monitoring Platform

IoT sensor monitoring platform with Zabbix integration. Collects sensor data via MQTT, stores in PostgreSQL, generates reports, and provisions Zabbix dashboards automatically.

## Architecture

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   Sensors   │────▶│  Mosquitto  │────▶│  Python Svc │
│  (MQTT)     │     │  (Broker)   │     │  (FastAPI)  │
└─────────────┘     └─────────────┘     └──────┬──────┘
                                               │
                    ┌─────────────┐            │
                    │   Zabbix    │◀───────────┤
                    │  (Server)   │  Trapper   │
                    └──────┬──────┘            │
                           │                   │
                    ┌──────▼──────┐     ┌──────▼──────┐
                    │  Zabbix Web │     │   Adminer   │
                    │  (Frontend) │     │   (DB UI)   │
                    └─────────────┘     └─────────────┘
                           │                   │
                    ┌──────▼──────┐     ┌──────▼──────┐
                    │ PostgreSQL  │     │ PostgreSQL  │
                    │  (zabbix)   │     │ (monitoring)│
                    └─────────────┘     └─────────────┘
```

## Services

| Service | Port | Description |
|---------|------|-------------|
| **Zabbix Web** | 8080 | Zabbix frontend (Admin/zabbix) |
| **Zabbix Server** | 10051 | Zabbix trapper for sensor data |
| **Python Service** | 8000 | FastAPI: MQTT→DB, reports, dashboards |
| **Mosquitto** | 1883/9001 | MQTT broker (WS on 9001) |
| **PostgreSQL** | 5432 | Database (zabbix + monitoring DBs) |
| **Adminer** | 8081 | Database web UI |

## Quick Start

```bash
# 1. Configure environment
cp .env.example .env  # Edit if needed

# 2. Start all services
docker compose up -d

# 3. Access services
# Zabbix:     http://localhost:8080 (Admin/zabbix)
# FastAPI:    http://localhost:8000 (dashboard, /report, /api/live)
# Adminer:    http://localhost:8081 (server: postgres, db: monitoring)
```

## Python Service API

| Endpoint | Description |
|----------|-------------|
| `GET /` | Live sensor dashboard (auto-refresh) |
| `GET /api/live` | JSON: latest readings, alerts, freshness |
| `GET /api/charts/{device_id}?hours=24` | Chart.js payload for device |
| `GET /devices` | All registered devices with probe ranges |
| `GET /devices/{id}/measurements` | Recent measurements for device |
| `GET /sensor-summary` | Aggregated stats per device |
| `GET /alerts` | Active alerts |
| `GET /report` | PDF report generator UI |
| `GET /report/pdf?device=X&start=Y&end=Z` | Generate history PDF |
| `GET /reports/pdf` | Daily summary PDF |
| `GET /health` | Health check |

## Data Flow

1. **Sensors** publish JSON to `sensors/#` or `zeta/#` topics
2. **Mosquitto** routes messages to Python service
3. **Python service** (`mqtt_client.py`):
   - Parses payload, resolves device ID (supports serial + topic aliases)
   - Stores in `measurements`, `mqtt_messages`, `history` (15-min buckets)
   - Updates `sensor_summary` aggregates
   - Evaluates thresholds → creates/resolves `alerts`
   - Pushes values to **Zabbix** via trapper protocol (port 10051)
   - Auto-provisions Zabbix items/triggers/dashboard for new devices
4. **Zabbix** displays data in "zeta" dashboard + "IoT Sensor Dashboard"

## Database Schema (monitoring DB)

- `devices` — registered sensors
- `measurements` — raw sensor readings (temp, humidity, pressure, battery, 4 probes)
- `mqtt_messages` — raw MQTT payloads for audit
- `history` — downsampled readings (15-min buckets for charts)
- `alerts` — threshold violations (active/resolved)
- `sensor_summary` — per-device aggregates (min/max/avg/latest/count)

## Testing

```bash
cd python-service
pip install -r requirements.txt
pytest tests/ -v
```

## Project Structure

```
monitoring-platform/
├── docker-compose.yml          # All services
├── .env                        # Environment variables (not in git)
├── .gitignore
├── database/
│   └── migrations/
│       ├── 000_create_monitoring_db.sh
│       ├── 001_init.sql
│       └── 002_monitoring_tables.sql
├── mosquitto/
│   ├── config/mosquitto.conf
│   ├── data/
│   └── log/
├── python-service/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── pyproject.toml
│   ├── pytest.ini
│   ├── check_zabbix.py         # Diagnostic (read-only)
│   ├── fix_zabbix.py           # One-time repair (deprecated)
│   ├── app/
│   │   ├── __init__.py
│   │   ├── main.py             # FastAPI app + endpoints
│   │   ├── config.py           # Pydantic settings
│   │   ├── database.py         # SQLAlchemy engine/session
│   │   ├── models.py           # SQLAlchemy models
│   │   ├── mqtt_client.py      # MQTT subscriber + Zabbix trapper
│   │   ├── zabbix_setup.py     # Zabbix provisioning (items/triggers/dashboard)
│   │   ├── zabbix_sync.py      # Periodic Zabbix↔DB sync
│   │   ├── reports.py          # Report data aggregation
│   │   ├── history_report.py   # PDF history reports
│   │   ├── pdf_report.py       # Daily summary PDF
│   │   └── chart_utils.py      # Chart.js data generation
│   └── tests/
│       ├── conftest.py
│       ├── test_api.py
│       ├── test_chart_utils.py
│       ├── test_device_alias.py
│       ├── test_history_report.py
│       ├── test_thresholds.py
│       └── test_zeta_merge.py
├── scripts/
│   ├── report.py               # CLI: console report from JSON files
│   ├── report_pdf.py           # CLI: PDF report from JSON files
│   ├── sensor_sim.ps1          # PowerShell MQTT sensor simulator
│   ├── test_json_pub.py        # Python MQTT pub/sub test
│   ├── test_mqtt.sh            # Shell MQTT test (uses docker exec)
│   ├── test_mqtt_flow.py       # Python MQTT flow test
│   └── test_payload.json       # Sample payload
└── zabbix-patches/
    └── CMenuHelper.php         # Adds "Sensor PDF Report" to Zabbix menu
```

## Configuration

Key environment variables (`.env`):

```bash
POSTGRES_DB=zabbix              # Zabbix database name
POSTGRES_USER=zabbix
POSTGRES_PASSWORD=zabbix_secret_2024
MONITORING_DB=monitoring        # IoT data database
TZ=Africa/Cairo                 # Timezone for timestamps
```

Python service settings (`app/config.py`):

```python
DEVICE_DISCOVERY_DAYS = 90      # How far back to provision Zabbix items
MQTT_TOPIC = "sensors/#"        # Main sensor topic
MQTT_TOPIC_EXTRA = "zeta/#"     # Zeta logger topic
ZABBIX_TRAPPER_HOST = "zabbix-server"
ZABBIX_TRAPPER_PORT = 10051
```

## Zabbix Integration

- **Auto-provisioning**: New devices get trapper items (`sensor.{id}.{metric}`), HTTP API items (`iot.field.{id}.{metric}`), and threshold triggers automatically
- **Dashboard "zeta"**: Per-device value cards + charts, refreshed every 5 min
- **Alert mirroring**: Database alerts → Zabbix Problems page (1/0 trapper items)
- **Menu patch**: `CMenuHelper.php` adds "Reports → Sensor PDF Report" linking to `/report`

## Scripts

| Script | Purpose |
|--------|---------|
| `scripts/sensor_sim.ps1` | Simulate sensor publishing to MQTT |
| `scripts/test_mqtt.sh` | Quick MQTT test via docker exec |
| `scripts/test_mqtt_flow.py` | Full pub/sub test with paho |
| `scripts/report.py` | Console report from JSON files (legacy) |
| `scripts/report_pdf.py` | PDF report from JSON files (legacy) |

> **Note**: `scripts/report*.py` read from `../data/*.json` (outside repo). They are legacy tools not integrated with the current pipeline.

## Troubleshooting

| Issue | Fix |
|-------|-----|
| Zabbix items not created | Check `python-service` logs: `docker compose logs python-service` |
| MQTT not connecting | Verify `mosquitto` health: `docker compose logs mosquitto` |
| DB migration errors | Ensure `database/migrations/002_monitoring_tables.sql` matches `app/models.py` |
| Dashboard not updating | Check `sync_zeta_dashboard` logs, verify Zabbix API reachable |
| Tests fail | Run with live PostgreSQL: `POSTGRES_HOST=localhost pytest tests/` |

## License

Internal use only.