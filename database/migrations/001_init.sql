-- ============================================
-- MONITORING PLATFORM - Database Initialization
-- ============================================
-- This script runs when PostgreSQL first starts (in the default POSTGRES_DB).
-- It ONLY creates the 'monitoring' database.
-- Tables are created by 002_monitoring_tables.sql which runs inside 'monitoring'.
-- ============================================

-- ============================================
-- Create monitoring database (runs in zabbix DB context on first start)
-- ============================================
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_database WHERE datname = 'monitoring') THEN
        CREATE DATABASE monitoring;
    END IF;
END
$$;
