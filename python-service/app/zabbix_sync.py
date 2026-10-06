import json
import logging
import time
import threading
import requests
from datetime import datetime, timezone
from sqlalchemy.orm import Session
from .database import SessionLocal
from .models import Device, Measurement, Alert
from .config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


class ZabbixSync:
    def __init__(self):
        self.base_url = settings.ZABBIX_API_URL
        self.auth_token = None
        self.running = False
        self.sync_interval = settings.ZABBIX_SYNC_INTERVAL

    def login(self):
        try:
            payload = {
                "jsonrpc": "2.0",
                "method": "user.login",
                "params": {
                    "username": settings.ZABBIX_USER,
                    "password": settings.ZABBIX_PASSWORD
                },
                "id": 1
            }
            resp = requests.post(self.base_url, json=payload, timeout=10)
            result = resp.json()
            if "result" in result:
                self.auth_token = result["result"]
                logger.info("Zabbix API login successful")
                return True
            else:
                logger.error(f"Zabbix login failed: {result.get('error', 'unknown')}")
                return False
        except Exception as e:
            logger.error(f"Zabbix login error: {e}")
            return False

    def get_hosts(self):
        if not self.auth_token or not self.login():
            return []
        try:
            payload = {
                "jsonrpc": "2.0",
                "method": "host.get",
                "params": {
                    "output": ["hostid", "host", "name"],
                    "selectInterfaces": ["ip"]
                },
                "auth": self.auth_token,
                "id": 2
            }
            resp = requests.post(self.base_url, json=payload, timeout=10)
            result = resp.json()
            return result.get("result", [])
        except Exception as e:
            logger.error(f"Error fetching hosts: {e}")
            return []

    def get_items(self, host_id):
        if not self.auth_token or not self.login():
            return []
        try:
            payload = {
                "jsonrpc": "2.0",
                "method": "item.get",
                "params": {
                    "output": ["itemid", "name", "key_", "lastvalue", "lastclock"],
                    "hostids": host_id,
                    "monitored": True
                },
                "auth": self.auth_token,
                "id": 3
            }
            resp = requests.post(self.base_url, json=payload, timeout=10)
            result = resp.json()
            return result.get("result", [])
        except Exception as e:
            logger.error(f"Error fetching items for host {host_id}: {e}")
            return []

    def get_history(self, item_id, limit=10):
        if not self.auth_token or not self.login():
            return []
        try:
            payload = {
                "jsonrpc": "2.0",
                "method": "history.get",
                "params": {
                    "output": ["clock", "value", "ns"],
                    "itemids": item_id,
                    "sortfield": "clock",
                    "sortorder": "DESC",
                    "limit": limit
                },
                "auth": self.auth_token,
                "id": 4
            }
            resp = requests.post(self.base_url, json=payload, timeout=10)
            result = resp.json()
            return result.get("result", [])
        except Exception as e:
            logger.error(f"Error fetching history for item {item_id}: {e}")
            return []

    def get_triggers(self):
        if not self.auth_token or not self.login():
            return []
        try:
            payload = {
                "jsonrpc": "2.0",
                "method": "trigger.get",
                "params": {
                    "output": ["triggerid", "description", "priority", "value", "lastchange"],
                    "sortfield": "lastchange",
                    "sortorder": "DESC",
                    "limit": 50
                },
                "auth": self.auth_token,
                "id": 5
            }
            resp = requests.post(self.base_url, json=payload, timeout=10)
            result = resp.json()
            return result.get("result", [])
        except Exception as e:
            logger.error(f"Error fetching triggers: {e}")
            return []

    def sync_to_database(self):
        db = SessionLocal()
        try:
            # Sync hosts as devices
            hosts = self.get_hosts()
            for host in hosts:
                host_id = host["host"]
                name = host.get("name", host_id)
                ip = host.get("interfaces", [{}])[0].get("ip", "")

                # Skip Zabbix internal hosts to keep monitoring DB clean
                if "zabbix" in host_id.lower() or "server" in host_id.lower():
                    continue

                existing = db.query(Device).filter(Device.device_id == host_id).first()
                if not existing:
                    device = Device(
                        device_id=host_id,
                        name=name,
                        location=f"Zabbix Host ({ip})",
                        type="zabbix_host",
                        status="online"
                    )
                    db.add(device)
                    logger.info(f"Added Zabbix host: {host_id}")

            db.flush()

            # Sync item measurements
            for host in hosts:
                host_id = host["host"]
                items = self.get_items(host["hostid"])

                for item in items:
                    item_key = item.get("key_", "")
                    last_value = item.get("lastvalue")
                    last_clock = item.get("lastclock")

                    if last_value is None or last_clock is None:
                        continue

                    timestamp = datetime.fromtimestamp(
                        int(last_clock), tz=timezone.utc).astimezone().replace(tzinfo=None)

                    # Check if measurement already exists for this timestamp
                    existing = db.query(Measurement).filter(
                        Measurement.device_id == host_id,
                        Measurement.received_at == timestamp
                    ).first()

                    if existing:
                        continue

                    # Map Zabbix item keys to measurement fields
                    measurement_data = {
                        "device_id": host_id,
                        "received_at": timestamp
                    }

                    key_lower = item_key.lower()
                    try:
                        value = float(last_value)
                    except (ValueError, TypeError):
                        continue

                    if "temperature" in key_lower or "temp" in key_lower:
                        measurement_data["temperature"] = value
                    elif "humidity" in key_lower:
                        measurement_data["humidity"] = value
                    elif "pressure" in key_lower:
                        measurement_data["pressure"] = value
                    elif "battery" in key_lower or "power" in key_lower:
                        measurement_data["battery"] = value
                    else:
                        # Store generic items as temperature field
                        measurement_data["temperature"] = value

                    measurement = Measurement(**measurement_data)
                    db.add(measurement)
                    logger.info(f"Saved measurement from Zabbix: {host_id} - {item_key}={last_value}")

            # Sync triggers as alerts
            triggers = self.get_triggers()
            for trigger in triggers:
                trigger_id = trigger["triggerid"]
                description = trigger.get("description", "")
                priority = int(trigger.get("priority", 0))
                value = int(trigger.get("value", 0))
                last_change = trigger.get("lastchange", "")

                # Only sync active triggers (value=1 means problem)
                if value != 1:
                    continue

                severity_map = {
                    0: "info", 1: "info", 2: "warning",
                    3: "warning", 4: "high", 5: "high"
                }
                severity = severity_map.get(priority, "warning")

                existing = db.query(Alert).filter(
                    Alert.alert_type == f"zabbix_trigger_{trigger_id}"
                ).first()

                if not existing:
                    alert = Alert(
                        device_id="zabbix",
                        alert_type=f"zabbix_trigger_{trigger_id}",
                        message=f"[Zabbix] {description}",
                        severity=severity,
                        status="active"
                    )
                    db.add(alert)
                    logger.info(f"Added Zabbix trigger alert: {description}")

            db.commit()
            logger.info("Zabbix sync completed successfully")

        except Exception as e:
            logger.error(f"Zabbix sync error: {e}")
            db.rollback()
        finally:
            db.close()

    def sync_loop(self):
        while self.running:
            try:
                if not self.auth_token or not self.login():
                    self.login()
                if self.auth_token:
                    self.sync_to_database()
            except Exception as e:
                logger.error(f"Sync loop error: {e}")
                self.auth_token = None
            time.sleep(self.sync_interval)

    def start(self):
        self.running = True
        thread = threading.Thread(target=self.sync_loop, daemon=True)
        thread.start()
        logger.info("Zabbix sync service started")

    def stop(self):
        self.running = False
        logger.info("Zabbix sync service stopped")


def get_zabbix_sync():
    return ZabbixSync()
