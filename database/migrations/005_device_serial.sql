-- Migration: Add serial field to devices table
-- Run after 004_device_mqtt_topic.sql

ALTER TABLE devices
  ADD COLUMN IF NOT EXISTS serial VARCHAR(64);