-- Migration: Add MQTT topic field to devices table
-- Run after 003_zabbix_host_fields.sql

ALTER TABLE devices
  ADD COLUMN IF NOT EXISTS mqtt_topic VARCHAR(256);