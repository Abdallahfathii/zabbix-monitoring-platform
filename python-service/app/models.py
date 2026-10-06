from sqlalchemy import Column, Integer, BigInteger, String, Float, Text, DateTime, ForeignKey, JSON, Index
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from .database import Base


class Device(Base):
    __tablename__ = "devices"

    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(String(64), unique=True, nullable=False, index=True)
    name = Column(String(128), nullable=False)
    location = Column(String(128))
    type = Column(String(32), default="sensor")
    status = Column(String(16), default="online")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Zabbix host provisioning fields
    ip_address = Column(String(64))
    dns_name = Column(String(128))
    port = Column(Integer, default=10050)
    interface_type = Column(Integer, default=1)  # 1=agent, 2=SNMP, 3=IPMI, 4=JMX
    host_groups = Column(JSON)  # List of group names
    templates = Column(JSON)    # List of template names
    macros = Column(JSON)       # Dict of macro name -> value
    inventory = Column(JSON)    # Dict of inventory fields
    zabbix_hostid = Column(String(32))
    zabbix_synced_at = Column(DateTime(timezone=True))
    zabbix_sync_status = Column(String(16), default="pending")  # pending, synced, failed
    mqtt_topic = Column(String(256))  # MQTT topic the device publishes to (e.g. sensors/ZTHT37)
    serial = Column(String(64))   # Serial from payload (for zeta/datalogger mapping)

    measurements = relationship("Measurement", back_populates="device")
    alerts = relationship("Alert", back_populates="device")


class Measurement(Base):
    __tablename__ = "measurements"
    __table_args__ = (
        Index("idx_measurements_device_received", "device_id", "received_at"),
    )

    id = Column(BigInteger, primary_key=True, index=True)
    device_id = Column(String(64), ForeignKey("devices.device_id"), nullable=False, index=True)
    temperature = Column(Float)
    humidity = Column(Float)
    pressure = Column(Float)
    battery = Column(Float)
    sensor_1 = Column(Float)
    sensor_2 = Column(Float)
    sensor_3 = Column(Float)
    sensor_4 = Column(Float)
    # Min/max of the external probes in this MQTT payload (NULL if none reported).
    sensor_min = Column(Float)
    sensor_max = Column(Float)
    # Alarm limits published by the device (ext_t1..4_min/max, else 2.0/8.0).
    threshold_min = Column(Float)
    threshold_max = Column(Float)
    # Sensor clock time of this reading (payload epoch), not the upload time.
    received_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)

    device = relationship("Device", back_populates="measurements")


class MQTTMessage(Base):
    __tablename__ = "mqtt_messages"

    id = Column(BigInteger, primary_key=True, index=True)
    topic = Column(String(256), nullable=False)
    payload = Column(JSON)
    qos = Column(Integer, default=0)
    # Time the sensor says it took the reading (payload epoch).
    received_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    # Time the broker delivered the message (server clock) - kept for freshness.
    uploaded_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)


class Alert(Base):
    __tablename__ = "alerts"

    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(String(64), ForeignKey("devices.device_id"), index=True)
    alert_type = Column(String(32), nullable=False)
    message = Column(Text, nullable=False)
    severity = Column(String(16), default="warning")
    status = Column(String(16), default="active")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    resolved_at = Column(DateTime(timezone=True))

    device = relationship("Device", back_populates="alerts")


class History(Base):
    __tablename__ = "history"
    __table_args__ = (
        Index("idx_history_device_time", "device_id", "received_at"),
    )

    id = Column(BigInteger, primary_key=True, index=True)
    device_id = Column(String(64))
    topic = Column(String(256))
    payload = Column(JSON)
    key_name = Column(String(256))
    value = Column(Float)
    received_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)


class SensorSummary(Base):
    __tablename__ = "sensor_summary"

    device_id = Column(String(64), primary_key=True)
    min_temp = Column(Float)
    max_temp = Column(Float)
    avg_temp = Column(Float)
    latest_temp = Column(Float)
    reading_count = Column(Integer)
    # Sensor clock time of the most recent reading.
    last_seen = Column(DateTime(timezone=True))
