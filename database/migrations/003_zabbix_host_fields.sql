-- Migration: Add Zabbix host provisioning fields to devices table
-- Run after 002_monitoring_tables.sql

ALTER TABLE devices
  ADD COLUMN IF NOT EXISTS ip_address VARCHAR(64),
  ADD COLUMN IF NOT EXISTS dns_name VARCHAR(128),
  ADD COLUMN IF NOT EXISTS port INTEGER DEFAULT 10050,
  ADD COLUMN IF NOT EXISTS interface_type INTEGER DEFAULT 1,
  ADD COLUMN IF NOT EXISTS host_groups JSON,
  ADD COLUMN IF NOT EXISTS templates JSON,
  ADD COLUMN IF NOT EXISTS macros JSON,
  ADD COLUMN IF NOT EXISTS inventory JSON,
  ADD COLUMN IF NOT EXISTS zabbix_hostid VARCHAR(32),
  ADD COLUMN IF NOT EXISTS zabbix_synced_at TIMESTAMP WITH TIME ZONE,
  ADD COLUMN IF NOT EXISTS zabbix_sync_status VARCHAR(16) DEFAULT 'pending';

-- Index for sync status queries
CREATE INDEX IF NOT EXISTS idx_devices_zabbix_sync_status ON devices(zabbix_sync_status);