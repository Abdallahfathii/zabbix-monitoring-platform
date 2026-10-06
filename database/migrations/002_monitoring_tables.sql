-- ============================================
-- MONITORING PLATFORM - IoT Tables Migration
-- ============================================
-- Creates tables in the monitoring database.
-- Matches app/models.py exactly.
-- ============================================

-- Table: devices
CREATE TABLE IF NOT EXISTS devices (
    id SERIAL PRIMARY KEY,
    device_id VARCHAR(64) UNIQUE NOT NULL,
    name VARCHAR(128) NOT NULL,
    location VARCHAR(128),
    type VARCHAR(32) DEFAULT 'sensor',
    status VARCHAR(16) DEFAULT 'online',
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- Table: measurements
CREATE TABLE IF NOT EXISTS measurements (
    id BIGSERIAL PRIMARY KEY,
    device_id VARCHAR(64) NOT NULL,
    temperature REAL,
    humidity REAL,
    pressure REAL,
    battery REAL,
    sensor_1 REAL,
    sensor_2 REAL,
    sensor_3 REAL,
    sensor_4 REAL,
    sensor_min REAL,
    sensor_max REAL,
    threshold_min REAL,
    threshold_max REAL,
    received_at TIMESTAMP DEFAULT NOW(),
    FOREIGN KEY (device_id) REFERENCES devices(device_id)
);

-- Table: mqtt_messages
CREATE TABLE IF NOT EXISTS mqtt_messages (
    id BIGSERIAL PRIMARY KEY,
    topic VARCHAR(256) NOT NULL,
    payload JSONB,
    qos INTEGER DEFAULT 0,
    received_at TIMESTAMP DEFAULT NOW(),
    uploaded_at TIMESTAMP DEFAULT NOW()
);

-- Table: alerts
CREATE TABLE IF NOT EXISTS alerts (
    id SERIAL PRIMARY KEY,
    device_id VARCHAR(64),
    alert_type VARCHAR(32) NOT NULL,
    message TEXT NOT NULL,
    severity VARCHAR(16) DEFAULT 'warning',
    status VARCHAR(16) DEFAULT 'active',
    created_at TIMESTAMP DEFAULT NOW(),
    resolved_at TIMESTAMP
);

-- Table: sensor_summary
CREATE TABLE IF NOT EXISTS sensor_summary (
    device_id VARCHAR(64) PRIMARY KEY,
    min_temp REAL,
    max_temp REAL,
    avg_temp REAL,
    latest_temp REAL,
    reading_count INTEGER,
    last_seen TIMESTAMP
);

-- Table: history
CREATE TABLE IF NOT EXISTS history (
    id BIGSERIAL PRIMARY KEY,
    device_id VARCHAR(64),
    topic VARCHAR(256),
    payload JSONB,
    key_name VARCHAR(256),
    value REAL,
    received_at TIMESTAMP DEFAULT NOW()
);

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_measurements_device_id ON measurements(device_id);
CREATE INDEX IF NOT EXISTS idx_measurements_received_at ON measurements(received_at);
CREATE INDEX IF NOT EXISTS idx_measurements_device_received ON measurements(device_id, received_at);
CREATE INDEX IF NOT EXISTS idx_mqtt_messages_received_at ON mqtt_messages(received_at);
CREATE INDEX IF NOT EXISTS idx_mqtt_messages_uploaded_at ON mqtt_messages(uploaded_at);
CREATE INDEX IF NOT EXISTS idx_alerts_device_id ON alerts(device_id);
CREATE INDEX IF NOT EXISTS idx_alerts_status ON alerts(status);
CREATE INDEX IF NOT EXISTS idx_history_device_time ON history(device_id, received_at);
