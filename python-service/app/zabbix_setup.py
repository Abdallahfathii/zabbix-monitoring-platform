import logging
import threading
import time
from datetime import datetime, timedelta

import requests
from .config import get_settings

logger = logging.getLogger(__name__)

ZABBIX_URL = "http://zabbix-web:8080/api_jsonrpc.php"
ZABBIX_USER = "Admin"
ZABBIX_PASSWORD = "zabbix"

# Metric definitions: card label, Zabbix item suffix, units and chart colour.
METRIC_ORDER = (
    "temperature", "humidity", "pressure", "battery",
    "sensor_1", "sensor_2", "sensor_3", "sensor_4",
)
PROBE_METRICS = ("sensor_1", "sensor_2", "sensor_3", "sensor_4")

METRIC_DEFS = {
    "location":   {"label": "Location",           "field": "Location",   "units": "",    "color": "546E7A"},
    "temperature": {"label": "Temperature",        "field": "Temperature", "units": "°C",  "color": "E53935"},
    "humidity":    {"label": "Humidity",           "field": "Humidity",    "units": "%",   "color": "1E88E5"},
    "pressure":    {"label": "Pressure",           "field": "Pressure",    "units": "hPa", "color": "00ACC1"},
    "battery":     {"label": "Battery",            "field": "Battery",     "units": "%",   "color": "8D6E63"},
    "sensor_1":    {"label": "Probe 1 temperature", "field": "Sensor 1",   "units": "°C",  "color": "43A047"},
    "sensor_2":    {"label": "Probe 2 temperature", "field": "Sensor 2",   "units": "°C",  "color": "FB8C00"},
    "sensor_3":    {"label": "Probe 3 temperature", "field": "Sensor 3",   "units": "°C",  "color": "FDD835"},
    "sensor_4":    {"label": "Probe 4 temperature", "field": "Sensor 4",   "units": "°C",  "color": "8E24AA"},
}

# Dashboard grid: Zabbix 7.0 dashboards are 72 columns wide, one row cell = 70px.
GRID_COLUMNS = 72
# Left hand column holding the "Data Logger" chart of every device.
CHART_WIDTH = 24
# Height of one band of value cards (chart spans all bands of its device).
BAND_HEIGHT = 3

# Card styling (matches the reference dashboard: big indigo value, red label).
CARD_VALUE_COLOR = "2E2E7A"
CARD_DESC_COLOR = "FF0000"
CARD_UP_COLOR = "00A651"
CARD_DOWN_COLOR = "FF4C4C"
# Charts and cards read the last 7 days, same window as the reference layout.
WINDOW_FROM = "now-7d"

# Location dashboard constants
LOCATION_DASHBOARD_NAME = "IoT Sensors by Location"
LOCATION_DASHBOARD_UNASSIGNED_PAGE = "Unassigned"

# Severity from monitoring.alerts -> Zabbix trigger priority
SEVERITY_PRIORITY = {
    "critical": 4, "disaster": 4, "error": 3, "high": 3,
    "warning": 2, "warn": 2, "info": 1, "low": 1,
}

# Thresholds used when a device never published its own limits
# (same defaults the MQTT alert engine applies).
DEFAULT_TEMP_MIN = 2.0
DEFAULT_TEMP_MAX = 8.0
DEFAULT_BATTERY_MIN = 20.0

# alert item key -> trapper item already exists in Zabbix
_ensured_alert_keys = set()


def _safe_key_part(value) -> str:
    """Turn any device id / alert type into a Zabbix key-safe fragment."""
    return "".join(
        c if c.isalnum() or c in "._" else "_" for c in (str(value) or "unknown")
    )


def alert_item_key(alert_type: str, device_id: str = None) -> str:
    """Zabbix item key for one device + alert type (key-safe characters only).

    Device scoped (iot.alert.<device>.<type>) so two devices raising the same
    alert type get one problem each instead of overwriting one another."""
    parts = ["iot.alert"]
    if device_id:
        parts.append(_safe_key_part(device_id))
    parts.append(_safe_key_part(alert_type))
    return ".".join(parts)


def load_alerts():
    """Latest row per (device_id, alert type) + the set of those keys active now."""
    from .database import SessionLocal
    from .models import Alert

    db = SessionLocal()
    try:
        rows = db.query(Alert).order_by(Alert.created_at.asc()).all()
    finally:
        db.close()

    newest = {}
    active = set()
    for row in rows:
        key = (row.device_id or "", row.alert_type)
        newest[key] = row
        if row.status == "active":
            active.add(key)
    return newest, active


def expected_description(alert) -> str:
    """Problem text shown on the Zabbix Problems page for one alert row."""
    message = " ".join((alert.message or alert.alert_type).split())
    description = (
        f"{alert.device_id}: {message}" if alert.device_id else message
    )
    return description[:255]


def device_metric_map():
    """{device_id: [metric, ...]} for every device reported within the
    DEVICE_DISCOVERY_DAYS window (see config.Settings)."""
    from .config import get_settings
    from .database import SessionLocal
    from .models import Measurement

    db = SessionLocal()
    try:
        cutoff = datetime.now() - timedelta(days=get_settings().DEVICE_DISCOVERY_DAYS)
        rows = db.query(Measurement).filter(Measurement.received_at >= cutoff).all()
    finally:
        db.close()

    found = {}
    for row in rows:
        metrics = found.setdefault(row.device_id, set())
        for metric in METRIC_ORDER:
            if getattr(row, metric) is not None:
                metrics.add(metric)

    return {
        device_id: [m for m in METRIC_ORDER if m in metrics]
        for device_id, metrics in sorted(found.items())
        if metrics
    }


def device_thresholds(device_id):
    """(min, max) temperature limits of one device as stored in sensor_summary
    (the ext_t*_min/max values the device publishes), with the defaults as fallback."""
    from .database import SessionLocal
    from .models import SensorSummary

    t_min, t_max = DEFAULT_TEMP_MIN, DEFAULT_TEMP_MAX
    db = SessionLocal()
    try:
        row = db.query(SensorSummary).filter(
            SensorSummary.device_id == device_id
        ).first()
        if row is not None:
            if row.min_temp is not None:
                t_min = float(row.min_temp)
            if row.max_temp is not None:
                t_max = float(row.max_temp)
    finally:
        db.close()
    return t_min, t_max


def device_location(device_id):
    """Last location reported by one device, empty string when unknown."""
    from .database import SessionLocal
    from .models import Device

    db = SessionLocal()
    try:
        row = db.query(Device.location).filter(Device.device_id == device_id).first()
        return ((row[0] or "").strip() if row else "")
    finally:
        db.close()


# alert item key -> (description, priority) already mirrored into Zabbix
_last_descriptions = {}
# one logged-in API client per process
_client_cache = None


def _api_client(refresh=False):
    """Reuse a logged-in Zabbix API client instead of logging in per call."""
    global _client_cache
    if refresh or _client_cache is None:
        client = ZabbixAPIClient()
        if not client.login():
            return None
        _client_cache = client
    return _client_cache


def push_alert_states():
    """Publish 1/0 per device+alert type so Zabbix opens/closes the matching
    problems, refreshing the problem text whenever the database message changed."""
    newest, active = load_alerts()
    if not newest:
        return

    expected = {}
    active_keys = set()
    for (device_id, alert_type), alert in newest.items():
        item_key = alert_item_key(alert_type, device_id)
        expected[item_key] = (
            expected_description(alert),
            SEVERITY_PRIORITY.get((alert.severity or "warning").lower(), 2)
        )
        if (device_id, alert_type) in active:
            active_keys.add(item_key)

    missing = set(expected) - _ensured_alert_keys
    changed = [
        key for key, state in expected.items()
        if _last_descriptions.get(key) != state
    ]

    if missing or changed:
        client = _api_client()
        if client:
            host = client.get_zabbix_server_host()
            if host:
                client.ensure_alert_problems(host["hostid"])

    from .mqtt_client import send_to_zabbix
    for item_key in expected:
        send_to_zabbix(item_key, 1 if item_key in active_keys else 0)


class ZabbixAPIClient:
    def __init__(self):
        self.url = ZABBIX_URL
        self.auth = None
        self.request_id = 0

    def _call(self, method, params):
        self.request_id += 1
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": self.request_id
        }
        if self.auth:
            payload["auth"] = self.auth
        resp = requests.post(self.url, json=payload, timeout=15)
        return resp.json()

    def login(self):
        result = self._call("user.login", {
            "username": ZABBIX_USER,
            "password": ZABBIX_PASSWORD
        })
        if "result" in result:
            self.auth = result["result"]
            logger.info("Zabbix API login OK")
            return True
        logger.error(f"Login failed: {result}")
        return False

    def get_zabbix_server_host(self):
        result = self._call("host.get", {
            "output": ["hostid", "host", "name"],
            "filter": {"host": ["Zabbix server"]}
        })
        hosts = result.get("result", [])
        if hosts:
            return hosts[0]
        return None

    def item_exists(self, host_id, key):
        result = self._call("item.get", {
            "output": ["itemid"],
            "hostids": host_id,
            "filter": {"key_": key}
        })
        return result.get("result", [])

    def delete_item(self, item_id):
        result = self._call("item.delete", [item_id])
        return "result" in result

    def cleanup_old_items(self, host_id):
        old_keys = [
            "iot.api.health", "iot.api.devices.count", "iot.api.alerts.count",
            "iot.api.db.measurements", "iot.api.db.devices", "iot.api.db.alerts",
            "iot.api.sensor.summary", "iot.api.full.dashboard",
            # iot.api.all.sensors is intentionally kept: it's recreated below
            # with a stable itemid so the dashboard widget keeps pointing at it.
        ]
        for key in old_keys:
            items = self.item_exists(host_id, key)
            for item in items:
                self.delete_item(item["itemid"])
                logger.info(f"Deleted old item: {key}")

        # Clean stale MQTT trapper items whose device no longer reports.
        # Devices still present are kept, so their history survives restarts.
        live_devices = set(device_metric_map())
        result = self._call("item.get", {
            "output": ["itemid", "key_"],
            "hostids": host_id,
            "search": {"key_": "sensor."},
            "searchWildcardsEnabled": True
        })
        if live_devices:
            for item in result.get("result", []):
                key = item["key_"]
                if not key.startswith("sensor.") or key.count(".") < 2:
                    continue
                if key.split(".")[1] in live_devices:
                    continue
                self.delete_item(item["itemid"])
                logger.info(f"Deleted stale trapper item: {key}")
        else:
            logger.warning("No live devices found; skipping trapper item cleanup")

        # Clean old per-sensor API items (sensor-001..004 etc)
        result = self._call("item.get", {
            "output": ["itemid", "key_"],
            "hostids": host_id,
            "search": {"key_": "iot.sensor."},
            "searchWildcardsEnabled": True
        })
        for item in result.get("result", []):
            if "sensor-00" in item["key_"] or "device1" in item["key_"] or "device2" in item["key_"] or "device3" in item["key_"] or "D0" in item["key_"] or "ZTHT_001" in item["key_"]:
                self.delete_item(item["itemid"])
                logger.info(f"Deleted old sensor item: {item['key_']}")

    # ============================================================
    # HOST PROVISIONING METHODS
    # ============================================================

    def host_exists(self, host_name: str) -> dict | None:
        """Check if a host exists by host name (technical name). Returns host dict or None."""
        result = self._call("host.get", {
            "output": ["hostid", "host", "name", "status", "interfaces", "groups", "templates", "macros", "inventory"],
            "filter": {"host": [host_name]},
            "selectInterfaces": ["interfaceid", "ip", "dns", "port", "type", "main", "useip"],
            "selectGroups": ["groupid", "name"],
            "selectParentTemplates": ["templateid", "name"],
            "selectMacros": ["macro", "value"],
            "selectInventory": ["location", "contact", "name", "alias", "os", "hardware", "software", "serialno_a", "serialno_b", "tag", "type", "type_full", "url", "vendor", "model", "date_hw_purchase", "date_hw_install", "date_hw_expiry", "site_rack", "site_notes", "chassis", "macaddress_a", "macaddress_b", "inventory_mode"]
        })
        hosts = result.get("result", [])
        return hosts[0] if hosts else None

    def host_get_groups(self, group_names: list[str]) -> list[dict]:
        """Resolve group names to groupids. Creates groups that don't exist."""
        if not group_names:
            return []
        # First try to get existing groups
        result = self._call("hostgroup.get", {
            "output": ["groupid", "name"],
            "filter": {"name": group_names}
        })
        existing = {g["name"]: g for g in result.get("result", [])}
        group_ids = []
        for name in group_names:
            if name in existing:
                group_ids.append({"groupid": existing[name]["groupid"]})
            else:
                # Create the group
                create_result = self._call("hostgroup.create", {"name": name})
                if "result" in create_result and create_result["result"].get("groupids"):
                    new_gid = create_result["result"]["groupids"][0]
                    group_ids.append({"groupid": new_gid})
                    logger.info(f"Created host group: {name} (groupid={new_gid})")
                else:
                    logger.error(f"Failed to create host group: {name}: {create_result.get('error')}")
        return group_ids

    def host_get_templates(self, template_names: list[str]) -> list[dict]:
        """Resolve template names to templateids."""
        if not template_names:
            return []
        result = self._call("template.get", {
            "output": ["templateid", "name"],
            "filter": {"name": template_names}
        })
        templates = result.get("result", [])
        found = {}
        for t in templates:
            found[t["name"]] = {"templateid": t["templateid"]}
        missing = [n for n in template_names if n not in found]
        if missing:
            logger.warning(f"Templates not found in Zabbix: {missing}")
        return [found[n] for n in template_names if n in found]

    def host_create_interface(self, host_id: str, ip: str = "", dns: str = "", port: int = 10050, interface_type: int = 1, main: int = 1, useip: int = 1) -> bool:
        """Create or update host interface."""
        if not ip and not dns:
            logger.warning("Cannot create interface: no IP or DNS provided")
            return False
        # Check existing interfaces
        result = self._call("hostinterface.get", {
            "output": ["interfaceid", "ip", "dns", "port", "type"],
            "hostids": host_id
        })
        existing = result.get("result", [])
        for iface in existing:
            if iface.get("type") == interface_type:
                # Update existing interface
                update_params = {
                    "interfaceid": iface["interfaceid"],
                    "ip": ip,
                    "dns": dns,
                    "port": str(port),
                    "type": interface_type,
                    "main": main,
                    "useip": useip
                }
                res = self._call("hostinterface.update", update_params)
                if "result" in res:
                    logger.info(f"Updated host interface for host {host_id}")
                    return True
                logger.error(f"Failed to update interface: {res.get('error')}")
                return False
        # Create new interface
        params = {
            "hostid": host_id,
            "ip": ip,
            "dns": dns,
            "port": str(port),
            "type": interface_type,
            "main": main,
            "useip": useip
        }
        res = self._call("hostinterface.create", params)
        if "result" in res:
            logger.info(f"Created host interface for host {host_id} (type={interface_type}, ip={ip}, dns={dns})")
            return True
        logger.error(f"Failed to create interface: {res.get('error')}")
        return False

    def host_create(self, host_params: dict) -> str | None:
        """Create a new Zabbix host. Returns hostid or None."""
        result = self._call("host.create", host_params)
        if "result" in result and result["result"].get("hostids"):
            hostid = result["result"]["hostids"][0]
            logger.info(f"Created Zabbix host: {host_params.get('host')} (hostid={hostid})")
            return hostid
        logger.error(f"Failed to create host {host_params.get('host')}: {result.get('error')}")
        return None

    def host_update(self, hostid: str, host_params: dict) -> bool:
        """Update an existing Zabbix host."""
        params = {"hostid": hostid, **host_params}
        result = self._call("host.update", params)
        if "result" in result:
            logger.info(f"Updated Zabbix host: {host_params.get('host', hostid)} (hostid={hostid})")
            return True
        logger.error(f"Failed to update host {hostid}: {result.get('error')}")
        return False

    def ensure_host(self, device, config) -> str | None:
        """Create or update Zabbix host from Device model. Returns hostid or None."""
        host_name = device.device_id
        visible_name = device.name or device.device_id

        # Resolve groups
        group_names = device.host_groups or config.ZABBIX_DEFAULT_GROUPS.split(",")
        group_names = [g.strip() for g in group_names if g.strip()]
        groups = self.host_get_groups(group_names)

        # Resolve templates
        template_names = device.templates or config.ZABBIX_DEFAULT_TEMPLATES.split(",")
        template_names = [t.strip() for t in template_names if t.strip()]
        templates = self.host_get_templates(template_names)

        # Build macros
        macros = device.macros or {}
        macros.setdefault("{$DEVICE_ID}", device.device_id)
        if device.mqtt_topic:
            macros.setdefault("{$MQTT_TOPIC}", device.mqtt_topic)
        if device.serial:
            macros.setdefault("{$SERIAL}", device.serial)
        # Add threshold macros from sensor_summary if available
        from .database import SessionLocal
        from .models import SensorSummary
        db = SessionLocal()
        try:
            summary = db.query(SensorSummary).filter(SensorSummary.device_id == device.device_id).first()
            if summary:
                if summary.min_temp is not None:
                    macros.setdefault("{$THRESHOLD_MIN}", str(summary.min_temp))
                if summary.max_temp is not None:
                    macros.setdefault("{$THRESHOLD_MAX}", str(summary.max_temp))
        finally:
            db.close()
        macro_list = [{"macro": k, "value": str(v)} for k, v in macros.items()]

        # Build inventory
        inventory = device.inventory or {}
        if device.location:
            inventory.setdefault("location", device.location)
        if device.mqtt_topic:
            inventory.setdefault("site_notes", f"MQTT Topic: {device.mqtt_topic}")
        if device.serial:
            inventory.setdefault("serialno_a", device.serial)
        inventory.setdefault("type", device.type or "sensor")
        inventory.setdefault("name", device.name)
        inventory.setdefault("alias", device.device_id)
        inventory_mode = 1  # 0=disabled, 1=manual, 2=auto

        # Build host params
        host_params = {
            "host": host_name,
            "name": visible_name,
            "status": 0 if (device.status or "online") == "online" else 1,  # 0=monitored, 1=not monitored
            "groups": groups,
            "templates": templates,
            "macros": macro_list,
            "inventory_mode": inventory_mode,
            "inventory": inventory,
        }

        existing = self.host_exists(host_name)
        if existing:
            hostid = existing["hostid"]
            # Update host
            if self.host_update(hostid, host_params):
                # Update interface
                ip = device.ip_address or ""
                dns = device.dns_name or ""
                port = device.port or config.ZABBIX_DEFAULT_PORT
                iface_type = device.interface_type or config.ZABBIX_DEFAULT_INTERFACE_TYPE
                self.host_create_interface(hostid, ip=ip, dns=dns, port=port, interface_type=iface_type)
                return hostid
            return None
        else:
            # Create new host
            hostid = self.host_create(host_params)
            if hostid:
                # Create interface
                ip = device.ip_address or ""
                dns = device.dns_name or ""
                port = device.port or config.ZABBIX_DEFAULT_PORT
                iface_type = device.interface_type or config.ZABBIX_DEFAULT_INTERFACE_TYPE
                self.host_create_interface(hostid, ip=ip, dns=dns, port=port, interface_type=iface_type)
            return hostid

    def ensure_item_delay(self, item_id, delay):
        """Keep an already-created HTTP item polling at the required interval."""
        detail = self._call("item.get", {
            "output": ["itemid", "delay"],
            "itemids": item_id
        })
        rows = detail.get("result", [])
        if not rows:
            return
        current = rows[0].get("delay")
        if current == delay:
            return
        res = self._call("item.update", {"itemid": item_id, "delay": delay})
        if "result" in res:
            logger.info(f"Item delay updated: {item_id} {current} -> {delay}")
        else:
            logger.error(f"Failed to update delay for item {item_id}: {res.get('error')}")

    def create_item(self, host_id, name, key, url, delay="1m", value_type=3):
        existing = self.item_exists(host_id, key)
        if existing:
            item_id = existing[0]["itemid"]
            self.ensure_item_delay(item_id, delay)
            logger.info(f"Item already exists: {name}")
            return item_id

        params = {
            "name": name,
            "key_": key,
            "type": 19,
            "value_type": value_type,
            "delay": delay,
            "history": "7d",
            "trends": "0" if value_type == 4 else "30d",
            "hostid": host_id,
            "url": url,
            "timeout": "10s",
            "verify_peer": 0,
            "verify_host": 0,
            "follow_redirects": 0,
            "post_type": 0,
            "tags": [{"tag": "source", "value": "iot-api"}]
        }

        result = self._call("item.create", params)
        if "result" in result:
            item_id = result["result"].get("itemids", [None])[0]
            logger.info(f"Created item: {name} (id={item_id})")
            return item_id
        logger.error(f"Failed to create item {name}: {result.get('error')}")
        return None

    def ensure_item_units(self, item_id, units):
        """Give an item its display units (°C, %, hPa) so cards show e.g. 24.38°C."""
        detail = self._call("item.get", {
            "output": ["itemid", "units"],
            "itemids": item_id
        })
        rows = detail.get("result", [])
        if not rows or rows[0].get("units") == units:
            return
        res = self._call("item.update", {"itemid": item_id, "units": units})
        if "result" in res:
            logger.info(f"Item units updated: {item_id} -> {units}")
        else:
            logger.error(f"Failed to update units for item {item_id}: {res.get('error')}")

    def create_trapper_item(self, host_id, name, key, value_type=0, units=""):
        existing = self.item_exists(host_id, key)
        if existing:
            item_id = existing[0]["itemid"]
            if units:
                self.ensure_item_units(item_id, units)
            logger.info(f"Trapper item already exists: {name}")
            return item_id

        params = {
            "name": name,
            "key_": key,
            "type": 2,                      # Zabbix trapper
            "value_type": value_type,
            "units": units,
            "history": "7d",
            "trends": "0" if value_type in (1, 4) else "30d",
            "hostid": host_id,
            "tags": [{"tag": "source", "value": "mqtt-trapper"}]
        }

        result = self._call("item.create", params)
        if "result" in result:
            item_id = result["result"].get("itemids", [None])[0]
            logger.info(f"Created trapper item: {name} (id={item_id})")
            return item_id
        logger.error(f"Failed to create trapper item {name}: {result.get('error')}")
        return None

    def create_trigger(self, host_id, name, expression, priority=3):
        """Create a trigger if it doesn't already exist on the given host. priority: 3=High, 4=Disaster."""
        # Search for triggers with this name that use items from the given host
        existing = self._call("trigger.get", {
            "output": ["triggerid", "description"],
            "filter": {"description": name},
            "selectItems": ["itemid", "hostid"],
            "expandExpression": 1
        })
        for trigger in existing.get("result", []):
            # Check if any item in the trigger belongs to the target host
            for item in trigger.get("items", []):
                if item.get("hostid") == host_id:
                    logger.info(f"Trigger already exists on host {host_id}: {name}")
                    return trigger["triggerid"]

        params = {
            "description": name,
            "expression": expression,
            "priority": priority,
            "status": 0,
            "type": 0,
            "tags": [{"tag": "source", "value": "iot-alert"}]
        }
        result = self._call("trigger.create", params)
        if "result" in result:
            tid = result["result"].get("triggerids", [None])[0]
            logger.info(f"Created trigger on host {host_id}: {name} (id={tid})")
            return tid
        logger.error(f"Failed to create trigger {name} on host {host_id}: {result.get('error')}")
        return None

    def ensure_alert_problems(self, host_id):
        """Mirror every alert type from monitoring.alerts into Zabbix:
        one trapper item + one trigger, so database alerts show up as
        problems on the Zabbix Problems page."""
        newest, active = load_alerts()
        if not newest:
            logger.info("No database alerts to mirror into Zabbix")
            return

        from .mqtt_client import send_to_zabbix

        # Drop the legacy device-less keys (iot.alert.<type>) so their stale
        # triggers cannot keep problems open next to the per-device ones.
        # Plain "search" (no wildcards flag) is a substring match.
        legacy = self._call("item.get", {
            "output": ["itemid", "key_"],
            "hostids": host_id,
            "search": {"key_": "iot.alert."}
        })
        for item in legacy.get("result", []):
            if item["key_"].startswith("iot.alert.") and item["key_"].count(".") == 2:
                self.delete_item(item["itemid"])
                logger.info(f"Deleted legacy alert item: {item['key_']}")

        for (device_id, alert_type), alert in newest.items():
            key = alert_item_key(alert_type, device_id)
            label = f"{device_id}: {alert_type}" if device_id else alert_type
            item_id = self.create_trapper_item(
                host_id, f"DB alert: {label}", key, 0
            )
            if not item_id:
                continue

            description = expected_description(alert)
            priority = SEVERITY_PRIORITY.get(
                (alert.severity or "warning").lower(), 2
            )

            self.ensure_alert_trigger(item_id, alert_type, key, description, priority)
            _ensured_alert_keys.add(key)
            _last_descriptions[key] = (description, priority)
            send_to_zabbix(key, 1 if (device_id, alert_type) in active else 0)

        logger.info(
            "Mirrored %d database alert(s) into Zabbix (%d active)",
            len(newest), len(active)
        )

    def ensure_alert_trigger(self, item_id, alert_type, key, description, priority):
        """Create or refresh the trigger that turns one alert type into a problem.
        Identified by the origin tag (trigger.get returns expressions as macros,
        so they cannot be matched on the item key)."""
        existing = self._call("trigger.get", {
            "output": ["triggerid", "description", "priority"],
            "selectTags": "extend",
            "itemids": [item_id],
        })
        rows = sorted(
            (
                t for t in existing.get("result", [])
                if any(
                    tg.get("tag") == "origin" and tg.get("value") == "monitoring-db"
                    for tg in (t.get("tags") or [])
                )
            ),
            key=lambda t: int(t["triggerid"])
        )
        if rows:
            for duplicate in rows[1:]:
                res = self._call("trigger.delete", [duplicate["triggerid"]])
                if "result" in res:
                    logger.info(
                        f"Removed duplicate DB alert trigger "
                        f"{duplicate['triggerid']} for {alert_type}"
                    )
                else:
                    logger.error(f"Failed to delete duplicate: {res.get('error')}")

            trigger = rows[0]
            updates = {"triggerid": trigger["triggerid"]}
            if trigger.get("description") != description:
                updates["description"] = description
            if int(trigger.get("priority", 0)) != priority:
                updates["priority"] = priority
            if len(updates) > 1:
                res = self._call("trigger.update", updates)
                if "result" in res:
                    logger.info(f"Updated DB alert trigger: {description}")
                else:
                    logger.error(f"Failed to update trigger: {res.get('error')}")
            return trigger["triggerid"]

        result = self._call("trigger.create", {
            "description": description,
            "expression": f"last(/Zabbix server/{key})=1",
            "priority": priority,
            "status": 0,
            "type": 0,
            "tags": [
                {"tag": "source", "value": "iot-alert"},
                {"tag": "origin", "value": "monitoring-db"},
                {"tag": "alert_type", "value": alert_type}
            ]
        })
        if "result" in result:
            tid = result["result"].get("triggerids", [None])[0]
            logger.info(f"Created DB alert trigger: {description} (id={tid})")
            return tid
        logger.error(f"Failed to create DB alert trigger: {result.get('error')}")
        return None

    def ensure_device_api_items(self, host_id, device_id, metrics=None,
                                api_base="http://python-service:8000"):
        """HTTP-poll items of one device: latest/min/max summary plus one item
        per reported field. The endpoints answer 0 for unknown values, so the
        items are safe to create for any device."""
        specs = [
            (f"IoT: {device_id} Latest", f"iot.sensor.{device_id}.latest",
             f"{api_base}/sensor/{device_id}/latest"),
            (f"IoT: {device_id} Min", f"iot.sensor.{device_id}.min",
             f"{api_base}/sensor/{device_id}/min"),
            (f"IoT: {device_id} Max", f"iot.sensor.{device_id}.max",
             f"{api_base}/sensor/{device_id}/max"),
        ]
        for metric in metrics or METRIC_ORDER:
            if metric not in METRIC_DEFS:
                continue
            specs.append((
                f"IoT: {device_id} {METRIC_DEFS[metric]['label']}",
                f"iot.field.{device_id}.{metric}",
                f"{api_base}/sensor/{device_id}/field/{metric}",
            ))

        created = []
        for name, key, url in specs:
            item_id = self.create_item(host_id, name, key, url, "10s", 0)
            if item_id:
                created.append(item_id)
        return created

    def ensure_device_triggers(self, host_id, device_id, metrics=None):
        """Threshold triggers for one device. Limits come from sensor_summary
        (payload-published ext_t*_min/max), defaulting to 2..8 °C / battery < 20%."""
        metrics = [m for m in (metrics or METRIC_ORDER) if m in METRIC_DEFS]
        t_min, t_max = device_thresholds(device_id)
        host = self.get_zabbix_server_host()
        host_name = (host or {}).get("name") or "Zabbix server"

        keys_needed = [f"iot.sensor.{device_id}.latest"]
        keys_needed += [
            f"iot.field.{device_id}.{m}"
            for m in metrics if m in PROBE_METRICS or m == "battery"
        ]

        item_map = {}
        for key in keys_needed:
            rows = self.item_exists(host_id, key)
            if rows:
                item_map[key] = rows[0]["itemid"]

        triggers = []
        latest_key = f"iot.sensor.{device_id}.latest"
        if latest_key in item_map:
            triggers.append((
                f"{device_id}: Temperature too high",
                f"last(/{host_name}/{latest_key})>{t_max:g}",
                4
            ))
            triggers.append((
                f"{device_id}: Temperature too low",
                f"last(/{host_name}/{latest_key})<{t_min:g}",
                4
            ))

        for number, metric in enumerate(PROBE_METRICS, start=1):
            if metric not in metrics:
                continue
            key = f"iot.field.{device_id}.{metric}"
            if key in item_map:
                triggers.append((
                    f"{device_id}: Sensor {number} temperature too high",
                    f"last(/{host_name}/{key})>{t_max:g}",
                    3
                ))
                triggers.append((
                    f"{device_id}: Sensor {number} temperature too low",
                    f"last(/{host_name}/{key})<{t_min:g}",
                    3
                ))

        battery_key = f"iot.field.{device_id}.battery"
        if battery_key in item_map:
            triggers.append((
                f"{device_id}: Battery low",
                f"last(/{host_name}/{battery_key})<{DEFAULT_BATTERY_MIN:g}",
                2
            ))

        for name, expr, prio in triggers:
            self.create_trigger(host_id, name, expr, prio)
        return triggers

    def ensure_device_triggers_on_device_host(self, host_id, device_id, metrics=None):
        """Threshold triggers on the device's own host using MQTT trapper items.
        Limits come from sensor_summary (payload-published ext_t*_min/max)."""
        metrics = [m for m in (metrics or METRIC_ORDER) if m in METRIC_DEFS]
        t_min, t_max = device_thresholds(device_id)

        # Use trapper item keys that exist on the device host
        trapper_keys = {
            "temperature": f"sensor.{device_id}.temperature",
            "humidity": f"sensor.{device_id}.humidity",
            "pressure": f"sensor.{device_id}.pressure",
            "battery": f"sensor.{device_id}.battery",
            "sensor_1": f"sensor.{device_id}.sensor_1",
            "sensor_2": f"sensor.{device_id}.sensor_2",
            "sensor_3": f"sensor.{device_id}.sensor_3",
            "sensor_4": f"sensor.{device_id}.sensor_4",
        }

        item_map = {}
        for metric, key in trapper_keys.items():
            if metric in metrics:
                rows = self.item_exists(host_id, key)
                if rows:
                    item_map[metric] = rows[0]["itemid"]

        triggers = []
        # Temperature trigger (uses temperature trapper item)
        if "temperature" in item_map:
            key = trapper_keys["temperature"]
            triggers.append((
                f"{device_id}: Temperature too high",
                f"last(/{device_id}/{key})>{t_max:g}",
                4
            ))
            triggers.append((
                f"{device_id}: Temperature too low",
                f"last(/{device_id}/{key})<{t_min:g}",
                4
            ))

        # Probe triggers
        for number, metric in enumerate(PROBE_METRICS, start=1):
            if metric not in metrics:
                continue
            if metric in item_map:
                key = trapper_keys[metric]
                triggers.append((
                    f"{device_id}: Sensor {number} temperature too high",
                    f"last(/{device_id}/{key})>{t_max:g}",
                    3
                ))
                triggers.append((
                    f"{device_id}: Sensor {number} temperature too low",
                    f"last(/{device_id}/{key})<{t_min:g}",
                    3
                ))

        # Battery trigger
        if "battery" in item_map:
            key = trapper_keys["battery"]
            triggers.append((
                f"{device_id}: Battery low",
                f"last(/{device_id}/{key})<{DEFAULT_BATTERY_MIN:g}",
                2
            ))

        for name, expr, prio in triggers:
            self.create_trigger(host_id, name, expr, prio)
        return triggers

    def setup_triggers(self, host_id):
        """Threshold triggers for every device that reports data."""
        total = 0
        for device_id, metrics in sorted(device_metric_map().items()):
            total += len(self.ensure_device_triggers(host_id, device_id, metrics))
        logger.info(f"Trigger setup complete ({total} checked)")

    def setup_dashboard(self):
        if not self.login():
            return False

        host = self.get_zabbix_server_host()
        if not host:
            logger.error("Zabbix server host not found")
            return False

        host_id = host["hostid"]
        api_base = "http://python-service:8000"

        self.cleanup_old_items(host_id)

        items_config = [
            ("IoT: API Health", "iot.api.health", f"{api_base}/health", "10s", 4),
            ("IoT: Devices Count", "iot.api.devices.count", f"{api_base}/devices", "10s", 4),
            ("IoT: Active Alerts", "iot.api.alerts.count", f"{api_base}/alerts", "10s", 4),
            ("IoT: DB Measurements", "iot.api.db.measurements", f"{api_base}/reports/stats", "10s", 4),
            ("IoT: DB Devices", "iot.api.db.devices", f"{api_base}/reports/stats", "10s", 4),
            ("IoT: DB Alerts", "iot.api.db.alerts", f"{api_base}/reports/stats", "10s", 4),
            ("IoT: Sensor Summary", "iot.api.sensor.summary", f"{api_base}/sensor-summary", "10s", 4),
            ("IoT: All Sensors Table", "iot.api.all.sensors", f"{api_base}/all-sensors", "10s", 4),
            ("IoT: Full Dashboard", "iot.api.full.dashboard", f"{api_base}/dashboard-text", "10s", 4),
            ("IoT: Report Link", "iot.api.report.link", f"{api_base}/report/link", "10s", 4),
        ]

        created_items = []
        for name, key, url, delay, vtype in items_config:
            item_id = self.create_item(host_id, name, key, url, delay, vtype)
            if item_id:
                created_items.append(item_id)

        # Per-device items (summary + per-field) polled from the monitoring DB.
        # The device list comes from the data, so a restart provisions every
        # device that ever reported - not just the original one.
        discovered = device_metric_map()
        for device_id, metrics in sorted(discovered.items()):
            for item_id in self.ensure_device_api_items(host_id, device_id, metrics, api_base):
                created_items.append(item_id)

        # Create trapper items (type=2) for MQTT sensor data pushed via zabbix_sender protocol
        for device_id, metrics in sorted(discovered.items()):
            for item_id, _name in self.metric_items(host_id, device_id, metrics).values():
                created_items.append(item_id)

        logger.info(f"Created {len(created_items)} items on Zabbix server host")

        # Setup triggers for threshold alarms
        self.setup_triggers(host_id)

        # Mirror monitoring.alerts rows onto the Zabbix Problems page
        self.ensure_alert_problems(host_id)

        # Create a dashboard with the all-sensors widget
        all_sensors_item_id = None
        for item_id in created_items:
            check = self._call("item.get", {
                "output": ["itemid", "key_"],
                "itemids": item_id
            })
            if check.get("result") and check["result"][0]["key_"] == "iot.api.all.sensors":
                all_sensors_item_id = item_id
                break

        if all_sensors_item_id:
            self.create_iot_dashboard(host_id, all_sensors_item_id)

        # Build/refresh the location-based dashboards
        self.ensure_location_dashboards(host_id, host["host"])

        logger.info("Dashboard setup complete!")
        return True

    def ensure_zeta_layout(self, host_id, host_name="Zabbix server"):
        """Rebuild the 'zeta' dashboard like the reference layout:
        one chart column per data logger next to big value cards."""
        device_metrics = device_metric_map()
        if not device_metrics:
            logger.info("No recent sensor data; zeta layout untouched")
            return False

        item_maps = {}
        for device_id, metrics in device_metrics.items():
            items = self.metric_items(host_id, device_id, metrics)
            if items:
                item_maps[device_id] = items
        if not item_maps:
            logger.info("No Zabbix items for the discovered devices; zeta layout untouched")
            return False

        page = self._zeta_page()
        if not page:
            return False

        desired = self._layout_widgets(
            device_metrics, item_maps, page.get("widgets", []), host_name
        )

        # Serialise compare+update: a provisioning refresh and the periodic
        # layout loop must not rewrite the page at the same time.
        with _dashboard_lock:
            merged = self._merge_widgets(page.get("widgets", []), desired)
            if self._same_widgets(page.get("widgets", []), merged):
                logger.info("zeta dashboard already matches the sensor layout")
                return True

            result = self._call("dashboard.update", {
                "dashboardid": page["dashboardid"],
                "pages": [{
                    "dashboard_pageid": page["dashboard_pageid"],
                    "name": page.get("name") or ZETASHBOARD_PAGE,
                    "widgets": merged,
                }]
            })
        if "result" in result:
            logger.info(f"zeta dashboard updated ({len(merged)} widgets)")
            return True
        logger.error(f"Failed to update zeta dashboard: {result.get('error')}")
        return False

    def _zeta_page(self):
        """Dashboard 'zeta' + its first page (created when it does not exist yet)."""
        dashes = self._call("dashboard.get", {
            "output": ["dashboardid", "name"],
            "selectPages": "extend",
            "filter": {"name": ZETASHBOARD_NAME}
        })
        if dashes.get("result"):
            dash = dashes["result"][0]
            pages = dash.get("pages") or []
            if pages:
                pages[0]["dashboardid"] = dash["dashboardid"]
                return pages[0]
            logger.error("Dashboard 'zeta' has no page")
            return None

        logger.info("Dashboard 'zeta' not found; creating it")
        result = self._call("dashboard.create", {
            "name": ZETASHBOARD_NAME,
            "display_period": 60,
            "auto_start": 1,
            "pages": [{"name": ZETASHBOARD_PAGE, "widgets": []}]
        })
        if "result" not in result:
            logger.error(f"Failed to create dashboard 'zeta': {result.get('error')}")
            return None
        return self._zeta_page()

    def ensure_location_dashboards(self, host_id, host_name="Zabbix server"):
        """Build/refresh the 'IoT Sensors by Location' dashboard with pages per location.
        
        Devices are grouped by their location field. Each location gets a page.
        Devices without location go to the 'Unassigned' page.
        """
        device_metrics = device_metric_map()
        if not device_metrics:
            logger.info("No recent sensor data; location dashboards untouched")
            return False

        # Get device locations
        device_locations = {}
        for device_id in device_metrics.keys():
            loc = device_location(device_id) or LOCATION_DASHBOARD_UNASSIGNED_PAGE
            device_locations[device_id] = loc

        # Get item maps for all devices
        item_maps = {}
        for device_id, metrics in device_metrics.items():
            items = self.metric_items(host_id, device_id, metrics)
            if items:
                item_maps[device_id] = items
        if not item_maps:
            logger.info("No Zabbix items for the discovered devices; location dashboards untouched")
            return False

        # Group devices by location
        location_groups = {}
        for device_id, loc in device_locations.items():
            if device_id in item_maps:
                location_groups.setdefault(loc, []).append(device_id)

        # Get or create the location dashboard
        dashboard = self._get_or_create_location_dashboard()
        if not dashboard:
            return False
        dashboard_id = dashboard["dashboardid"]

        # Get existing pages
        existing_pages = dashboard.get("pages") or []
        page_map = {p.get("name"): p for p in existing_pages}

        # Build desired pages for each location
        desired_pages = []
        for location_name in sorted(location_groups.keys()):
            device_ids = location_groups[location_name]
            page_widgets = self._build_location_page_widgets(
                location_name, device_ids, item_maps, host_name
            )
            if not page_widgets:
                continue
            
            page = page_map.get(location_name)
            if page:
                # Update existing page
                desired_pages.append({
                    "dashboard_pageid": page["dashboard_pageid"],
                    "name": location_name,
                    "widgets": page_widgets,
                })
            else:
                # New page - will be created
                desired_pages.append({
                    "name": location_name,
                    "widgets": page_widgets,
                })

        # Also handle Unassigned page if there are devices without location
        if LOCATION_DASHBOARD_UNASSIGNED_PAGE in location_groups:
            device_ids = location_groups[LOCATION_DASHBOARD_UNASSIGNED_PAGE]
            page_widgets = self._build_location_page_widgets(
                LOCATION_DASHBOARD_UNASSIGNED_PAGE, device_ids, item_maps, host_name
            )
            if page_widgets:
                page = page_map.get(LOCATION_DASHBOARD_UNASSIGNED_PAGE)
                if page:
                    desired_pages.append({
                        "dashboard_pageid": page["dashboard_pageid"],
                        "name": LOCATION_DASHBOARD_UNASSIGNED_PAGE,
                        "widgets": page_widgets,
                    })
                else:
                    desired_pages.append({
                        "name": LOCATION_DASHBOARD_UNASSIGNED_PAGE,
                        "widgets": page_widgets,
                    })

        # Update dashboard with desired pages
        with _dashboard_lock:
            result = self._call("dashboard.update", {
                "dashboardid": dashboard_id,
                "pages": desired_pages,
            })
        if "result" in result:
            logger.info(f"Location dashboards updated ({len(desired_pages)} pages)")
            return True
        logger.error(f"Failed to update location dashboards: {result.get('error')}")
        return False

    def _get_or_create_location_dashboard(self):
        """Get or create the 'IoT Sensors by Location' dashboard."""
        dashes = self._call("dashboard.get", {
            "output": ["dashboardid", "name"],
            "selectPages": "extend",
            "filter": {"name": LOCATION_DASHBOARD_NAME}
        })
        if dashes.get("result"):
            dash = dashes["result"][0]
            return dash
        
        logger.info(f"Dashboard '{LOCATION_DASHBOARD_NAME}' not found; creating it")
        # Create with at least one page (Unassigned) to satisfy Zabbix API
        result = self._call("dashboard.create", {
            "name": LOCATION_DASHBOARD_NAME,
            "display_period": 60,
            "auto_start": 1,
            "pages": [{
                "name": LOCATION_DASHBOARD_UNASSIGNED_PAGE,
                "widgets": []
            }]
        })
        if "result" not in result:
            logger.error(f"Failed to create dashboard '{LOCATION_DASHBOARD_NAME}': {result.get('error')}")
            return None
        return self._get_or_create_location_dashboard()

    def _build_location_page_widgets(self, location_name, device_ids, item_maps, host_name):
        """Build widget list for a location page."""
        if not device_ids:
            return []
        
        # Filter metrics that have items
        valid_devices = {}
        for device_id in device_ids:
            metrics = [m for m in item_maps.get(device_id, {}).keys() if m in item_maps.get(device_id, {})]
            if metrics:
                valid_devices[device_id] = metrics
        
        if not valid_devices:
            return []

        widgets = []
        y = 0
        CHART_WIDTH = 24
        BAND_HEIGHT = 3
        GRID_COLUMNS = 72
        WINDOW_FROM = "now-7d"

        for device_id in sorted(valid_devices):
            metrics = valid_devices[device_id]
            if not metrics:
                continue

            has_location = "location" in item_maps.get(device_id, {})
            display_metrics = [m for m in metrics if m != "location"]
            if not display_metrics and not has_location:
                continue

            unit_metrics = [m for m in display_metrics if m not in PROBE_METRICS]
            probe_metrics = [m for m in display_metrics if m in PROBE_METRICS]
            bands = [["location"]] if has_location else []
            bands += [unit_metrics[i:i + 2] for i in range(0, len(unit_metrics), 2)]
            bands += [probe_metrics[i:i + 4] for i in range(0, len(probe_metrics), 4)]
            if not bands:
                continue

            series = probe_metrics or [
                m for m in ("temperature", "humidity") if m in display_metrics
            ]
            chart_x = CHART_WIDTH if series else 0
            region_w = GRID_COLUMNS - chart_x

            for band_index, band in enumerate(bands):
                band_y = y + band_index * BAND_HEIGHT
                card_width = region_w // len(band) if band else region_w
                for index, metric in enumerate(band):
                    item_id, _ = item_maps[device_id][metric]
                    label = METRIC_DEFS[metric]["label"]
                    widgets.append(self._card_widget(
                        f"{device_id}: {label}", item_id, label,
                        chart_x + index * card_width, band_y, card_width
                    ))

            if series:
                widgets.append(self._chart_widget(
                    f"Data Logger {device_id}", host_name,
                    [(m, item_maps[device_id][m]) for m in series],
                    0, y, CHART_WIDTH, BAND_HEIGHT * len(bands)
                ))

            y += BAND_HEIGHT * len(bands)

        # Add clock at bottom
        clock = {
            "type": "clock",
            "name": "Local Time",
            "view_mode": "0",
            "fields": [
                {"type": "0", "name": "clock_type", "value": "1"},
                {"type": "0", "name": "show.0", "value": "2"},
                {"type": "0", "name": "show.1", "value": "3"},
                {"type": "0", "name": "time_format", "value": "1"},
            ],
        }
        widgets.append(self._place(clock, 48, y, GRID_COLUMNS - 48, BAND_HEIGHT))

        # Keep widget IDs for existing widgets on this page
        # (We can't easily track per-page here, so we rely on Zabbix's merge logic)
        return widgets

    def metric_items(self, host_id, device_id, metrics):
        """{metric: (itemid, item name)} of the MQTT trapper items of one device,
        creating the missing ones (and their units) on the way."""
        items = {}
        for metric in metrics:
            spec = METRIC_DEFS[metric]
            item_id = self.create_trapper_item(
                host_id,
                f"MQTT: {device_id} {spec['field']}",
                f"sensor.{device_id}.{metric}",
                0,
                units=spec["units"],
            )
            if item_id:
                items[metric] = (item_id, f"MQTT: {device_id} {spec['field']}")

        location = device_location(device_id)
        if location:
            location_key = f"sensor.{device_id}.location"
            location_id = self.create_trapper_item(
                host_id, f"MQTT: {device_id} Location", location_key, 1
            )
            if location_id:
                items["location"] = (location_id, f"MQTT: {device_id} Location")
                from .mqtt_client import send_to_zabbix
                send_to_zabbix(location_key, location)
        return items

    def _layout_widgets(self, device_metrics, item_maps, existing, host_name):
        """Complete widget list of the 'zeta' page: a chart column per data logger
        next to its big value cards, with the clock kept below."""
        by_key = {(w.get("type"), w.get("name") or ""): w for w in existing}
        widgets = []
        y = 0

        for device_id in sorted(item_maps):
            metrics = [
                m for m in device_metrics.get(device_id, []) if m in item_maps[device_id]
            ]
            # Location is not a measurement column, so it only shows up here
            # when metric_items() actually created the item for the device.
            has_location = "location" in item_maps[device_id]
            metrics = [m for m in metrics if m != "location"]
            if not metrics and not has_location:
                continue

            unit_metrics = [m for m in metrics if m not in PROBE_METRICS]
            probe_metrics = [m for m in metrics if m in PROBE_METRICS]
            # One full-width Location band as the column header, then two wide
            # cards per band (Temperature/Humidity/Pressure/Battery), four probe
            # cards per band - same rhythm as the reference layout.
            bands = [["location"]] if has_location else []
            bands += [unit_metrics[i:i + 2] for i in range(0, len(unit_metrics), 2)]
            bands += [probe_metrics[i:i + 4] for i in range(0, len(probe_metrics), 4)]
            if not bands:
                continue

            # Probes are charted on their own; loggers without probes chart
            # temperature (+ humidity when the device reports it).
            series = probe_metrics or [
                m for m in ("temperature", "humidity") if m in metrics
            ]
            chart_x = CHART_WIDTH if series else 0
            region_w = GRID_COLUMNS - chart_x

            for band_index, band in enumerate(bands):
                band_y = y + band_index * BAND_HEIGHT
                card_width = region_w // len(band)
                for index, metric in enumerate(band):
                    item_id, _ = item_maps[device_id][metric]
                    label = METRIC_DEFS[metric]["label"]
                    widgets.append(self._card_widget(
                        f"{device_id}: {label}", item_id, label,
                        chart_x + index * card_width, band_y, card_width
                    ))

            if series:
                widgets.append(self._chart_widget(
                    f"Data Logger {device_id}", host_name,
                    [(m, item_maps[device_id][m]) for m in series],
                    0, y, CHART_WIDTH, BAND_HEIGHT * len(bands)
                ))

            y += BAND_HEIGHT * len(bands)

        clock = by_key.get(("clock", "Local Time")) or next(
            (w for w in existing if w.get("type") == "clock"), None
        ) or {
            # Fresh page: give it the standard clock instead of nothing
            # (field names/types copied from a stock clock widget).
            "type": "clock",
            "name": "Local Time",
            "view_mode": "0",
            "fields": [
                {"type": "0", "name": "clock_type", "value": "1"},
                {"type": "0", "name": "show.0", "value": "2"},
                {"type": "0", "name": "show.1", "value": "3"},
                {"type": "0", "name": "time_format", "value": "1"},
            ],
        }
        widgets.append(self._place(clock, 48, y, GRID_COLUMNS - 48, BAND_HEIGHT))

        # Keep the ids of widgets that are already on the page, so a layout
        # refresh updates them instead of dropping and recreating them.
        known_ids = {
            (w.get("type"), w.get("name") or ""): w.get("widgetid")
            for w in existing if w.get("widgetid")
        }
        for widget in widgets:
            if not widget.get("widgetid"):
                widget_id = known_ids.get((widget.get("type"), widget.get("name") or ""))
                if widget_id:
                    widget["widgetid"] = widget_id

        return widgets

    @staticmethod
    def _place(widget, x, y, width, height):
        """Reposition an existing widget, keeping its id, view mode and fields."""
        placed = {
            "type": widget.get("type"),
            "name": widget.get("name", ""),
            "x": x,
            "y": y,
            "width": width,
            "height": height,
            "view_mode": widget.get("view_mode", "0"),
            "fields": [
                {
                    "type": str(f.get("type")),
                    "name": str(f.get("name")),
                    "value": str(f.get("value", "")),
                }
                for f in (widget.get("fields") or [])
            ],
        }
        # A widget that does not exist yet must omit widgetid entirely:
        # dashboard.update treats a null id as an invalid parameter.
        if widget.get("widgetid"):
            placed["widgetid"] = widget["widgetid"]
        return placed

    @staticmethod
    def _card_widget(name, item_id, label, x, y, width):
        """'Item value' widget styled like the reference dashboard: timestamp on top,
        big indigo value with trend arrow, red metric label at the bottom."""
        return {
            "type": "item",
            "name": name,
            "x": x,
            "y": y,
            "width": width,
            "height": BAND_HEIGHT,
            "view_mode": "0",
            "fields": [
                {"type": "4", "name": "itemid.0", "value": str(item_id)},
                {"type": "1", "name": "description", "value": label},
                {"type": "0", "name": "desc_bold", "value": "1"},
                {"type": "1", "name": "desc_color", "value": CARD_DESC_COLOR},
                {"type": "1", "name": "value_color", "value": CARD_VALUE_COLOR},
                {"type": "1", "name": "up_color", "value": CARD_UP_COLOR},
                {"type": "1", "name": "down_color", "value": CARD_DOWN_COLOR},
                {"type": "0", "name": "decimal_places", "value": "2"},
                {"type": "1", "name": "time_period.from", "value": WINDOW_FROM},
                {"type": "1", "name": "time_period.to", "value": "now"},
            ],
        }

    @staticmethod
    def _chart_widget(name, host_name, series, x, y, width, height):
        """'Graph' widget with one coloured line per metric and a 7 day window."""
        fields = []
        for index, (metric, (_item_id, item_name)) in enumerate(series):
            fields += [
                {"type": "1", "name": f"ds.{index}.hosts.0", "value": host_name},
                {"type": "1", "name": f"ds.{index}.items.0", "value": item_name},
                {"type": "1", "name": f"ds.{index}.color", "value": METRIC_DEFS[metric]["color"]},
                {"type": "0", "name": f"ds.{index}.fill", "value": "0"},
                {"type": "0", "name": f"ds.{index}.width", "value": "2"},
                {"type": "0", "name": f"ds.{index}.transparency", "value": "0"},
            ]
        fields += [
            {"type": "0", "name": "source", "value": "0"},
            {"type": "0", "name": "lefty", "value": "1"},
            {"type": "0", "name": "righty", "value": "0"},
            {"type": "0", "name": "axisx", "value": "1"},
            {"type": "0", "name": "legend", "value": "1"},
            {"type": "1", "name": "time_period.from", "value": WINDOW_FROM},
            {"type": "1", "name": "time_period.to", "value": "now"},
        ]
        return {
            "type": "svggraph",
            "name": name,
            "x": x,
            "y": y,
            "width": width,
            "height": height,
            "view_mode": "0",
            "fields": fields,
        }

    def _merge_widgets(self, existing, desired):
        """Page widgets combined with the generated layout.

        A widget that is already on the page keeps the position, size, colours
        and fields the user chose, so the periodic sync never undoes a manual
        edit - only widgets the layout needs and the page does not have yet are
        appended. A widget the user deleted stays deleted. Nothing is ever
        removed, so a card can only disappear by hand."""
        def key(widget):
            return (str(widget.get("type")), str(widget.get("name") or ""))

        present = {key(w) for w in existing}
        managed = {key(w) for w in desired}

        # Something we placed before is gone now => the user removed it on
        # purpose. Coming back onto the page by hand cancels that.
        _deleted_by_user.update(_installed_widgets - present)
        _deleted_by_user.difference_update(present)

        merged = [
            self._place(
                w, int(w.get("x", 0)), int(w.get("y", 0)),
                int(w.get("width", 0)), int(w.get("height", 0)),
            )
            for w in existing
        ]
        added = 0
        for widget in desired:
            widget_key = key(widget)
            if widget_key in present or widget_key in _deleted_by_user:
                continue
            merged.append(widget)
            added += 1

        _installed_widgets.clear()
        _installed_widgets.update({key(w) for w in merged} & managed)

        if added:
            logger.info(f"zeta dashboard: added {added} widget(s) for new sensor data")
        return merged

    @staticmethod
    def _same_widgets(existing, desired):
        """True when the page already holds exactly the wanted widget set."""
        def norm(widget):
            fields = sorted(
                (str(f.get("type")), str(f.get("name")), str(f.get("value", "")))
                for f in (widget.get("fields") or [])
            )
            return (
                str(widget.get("type")),
                str(widget.get("name") or ""),
                int(widget.get("x", 0)),
                int(widget.get("y", 0)),
                int(widget.get("width", 0)),
                int(widget.get("height", 0)),
                str(widget.get("view_mode", "0")),
                fields,
            )

        return sorted(norm(w) for w in existing) == sorted(norm(w) for w in desired)


    def create_iot_dashboard(self, host_id, all_sensors_item_id):
        existing = self._call("dashboard.get", {
            "output": ["dashboardid"],
            "filter": {"name": "IoT Sensor Dashboard"}
        })
        if existing.get("result"):
            logger.info("Dashboard already exists: IoT Sensor Dashboard")
            return existing["result"][0]["dashboardid"]

        result = self._call("dashboard.create", {
            "name": "IoT Sensor Dashboard",
            "display_period": 60,
            "auto_start": 1,
            "pages": [{
                "name": "All Sensors",
                "widgets": [
                    {
                        # "plaintext" is not a real Zabbix widget type (renders
                        # as "Page not found"); embed the endpoint instead.
                        "type": "url",
                        "name": "All Sensors Table",
                        "x": 0,
                        "y": 0,
                        "width": 12,
                        "height": 6,
                        "view_mode": "0",
"fields": [
                             {"type": "1", "name": "url",
                              "value": "http://python-service:8000/all-sensors"},
                        ],
                    }
                ]
            }]
        })
        logger.info(f"Dashboard create result: {result}")
        if "result" in result:
            did = result["result"].get("dashboardids", [None])[0]
            logger.info(f"Created dashboard: IoT Sensor Dashboard (id={did})")
            return did
        logger.error(f"Failed to create dashboard: {result.get('error')}")
        return None


def run_setup():
    client = ZabbixAPIClient()
    return client.setup_dashboard()


def sync_location_dashboards():
    """Refresh the location-based dashboards (cheap enough to run periodically,
    so a device that starts reporting later gets its cards without a restart)."""
    client = ZabbixAPIClient()
    if not client.login():
        return False
    host = client.get_zabbix_server_host()
    if not host:
        logger.error("Zabbix server host not found")
        return False
    return client.ensure_location_dashboards(host["hostid"], host["host"])


# device_id -> set of metrics already provisioned in Zabbix
_provisioned = {}
# device_id -> monotonic time of the last provisioning attempt (retry throttle)
_last_attempt = {}
_provision_lock = threading.Lock()
# Serialises dashboard rewrites (provisioning vs. the periodic layout loop).
_dashboard_lock = threading.Lock()

# Wait before retrying a device whose provisioning failed (Zabbix down, ...).
PROVISION_RETRY_SECONDS = 30


def provision_device(device_id, metrics=None, force=False):
    """Build everything Zabbix needs for one device: MQTT trapper items (with
    units), API-poll items and threshold triggers - then refresh the dashboard.

    Runs once per device (and again only when its metric set grows), so the
    MQTT handler can call it on every message without costing API calls."""
    wanted = {m for m in (metrics or []) if m in METRIC_DEFS}

    with _provision_lock:
        done = _provisioned.get(device_id)
        if done is not None and wanted <= done and not force:
            return False

        now = time.monotonic()
        if not force and now - _last_attempt.get(device_id, 0.0) < PROVISION_RETRY_SECONDS:
            return False
        _last_attempt[device_id] = now

        try:
            client = _api_client()
            if not client:
                return False
            
            # Get the device's own Zabbix host (not the Zabbix server host)
            device_host = client.host_exists(device_id)
            if not device_host:
                logger.error(f"Device host not found for {device_id}, run provision_host first")
                return False
            device_hostid = device_host["hostid"]

            merged = sorted(set(done or ()) | wanted, key=METRIC_ORDER.index)
            client.metric_items(device_hostid, device_id, merged)
            client.ensure_device_api_items(device_hostid, device_id, merged)
            client.ensure_device_triggers_on_device_host(device_hostid, device_id, merged)
            _provisioned[device_id] = set(merged)
        except Exception as e:
            logger.error(f"Provisioning {device_id} failed: {e}")
            return False

    logger.info(
        f"Provisioned {device_id} in Zabbix "
        f"({len(_provisioned[device_id])} metric(s))"
    )
    # Dashboard refresh happens outside the lock so the MQTT loop is never held.
    threading.Thread(target=lambda: sync_location_dashboards(), daemon=True).start()
    return True


# ============================================================
# HOST PROVISIONING FUNCTIONS
# ============================================================

_host_provision_lock = threading.Lock()
_host_provisioned = set()
_host_last_attempt = {}
HOST_PROVISION_RETRY_SECONDS = 30


def provision_host(device, force=False) -> bool:
    """Create or update Zabbix host from Device model with all properties.

    Runs once per device (and again only when forced), so the MQTT handler
    can call it on every new device discovery without costing API calls."""
    device_id = device.device_id
    config = get_settings()

    with _host_provision_lock:
        if not force and device_id in _host_provisioned:
            return False

        now = time.monotonic()
        if not force and now - _host_last_attempt.get(device_id, 0.0) < HOST_PROVISION_RETRY_SECONDS:
            return False
        _host_last_attempt[device_id] = now

        try:
            client = _api_client()
            if not client:
                return False
            host = client.get_zabbix_server_host()
            if not host:
                client = _api_client(refresh=True)
                host = client.get_zabbix_server_host() if client else None
            if not host:
                logger.error(f"Cannot provision host for {device_id}: Zabbix server host not found")
                return False
            zabbix_server_hostid = host["hostid"]

            # Ensure the device has its own Zabbix host (not the Zabbix server host)
            # First check if host already exists for this device
            existing_host = client.host_exists(device_id)
            if existing_host:
                device_hostid = existing_host["hostid"]
            else:
                # Create a new host for this device
                # Note: ensure_host will create the host and return its hostid
                pass

            hostid = client.ensure_host(device, config)
            if not hostid:
                logger.error(f"Failed to provision host for {device_id}")
                return False

            # Update device with zabbix_hostid and sync status
            from .database import SessionLocal
            from sqlalchemy import text
            db = SessionLocal()
            try:
                db.execute(
                    text("UPDATE devices SET zabbix_hostid = :hid, zabbix_synced_at = NOW(), zabbix_sync_status = 'synced' WHERE device_id = :did"),
                    {"hid": hostid, "did": device_id}
                )
                db.commit()
            finally:
                db.close()

            _host_provisioned.add(device_id)
            logger.info(f"Provisioned Zabbix host for device {device_id} (hostid={hostid})")
        except Exception as e:
            logger.error(f"Host provisioning for {device_id} failed: {e}")
            # Update device with failed status
            try:
                from .database import SessionLocal
                from sqlalchemy import text
                db = SessionLocal()
                try:
                    db.execute(
                        text("UPDATE devices SET zabbix_sync_status = 'failed' WHERE device_id = :did"),
                        {"did": device_id}
                    )
                    db.commit()
                finally:
                    db.close()
            except Exception:
                pass
            return False

    return True


def sync_all_hosts(force=False) -> dict:
    """Sync all devices from monitoring DB to Zabbix hosts.

    Returns dict with counts: {"total": N, "synced": N, "failed": N, "skipped": N}"""
    from .database import SessionLocal
    from .models import Device

    db = SessionLocal()
    try:
        devices = db.query(Device).all()
    finally:
        db.close()

    results = {"total": len(devices), "synced": 0, "failed": 0, "skipped": 0}
    for device in devices:
        try:
            if provision_host(device, force=force):
                results["synced"] += 1
            else:
                results["skipped"] += 1
        except Exception as e:
            logger.error(f"Failed to sync host for {device.device_id}: {e}")
            results["failed"] += 1
    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_setup()
