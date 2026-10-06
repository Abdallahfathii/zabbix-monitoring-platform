#!/bin/bash
set -e

# Create monitoring database if it doesn't exist
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    DO \$\$
    BEGIN
        IF NOT EXISTS (SELECT FROM pg_database WHERE datname = 'monitoring') THEN
            CREATE DATABASE monitoring;
        END IF;
    END
    \$\$;
EOSQL

# Run migrations in monitoring database
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "monitoring" -f /docker-entrypoint-initdb.d/002_monitoring_tables.sql
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "monitoring" -f /docker-entrypoint-initdb.d/003_zabbix_host_fields.sql
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "monitoring" -f /docker-entrypoint-initdb.d/004_device_mqtt_topic.sql
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "monitoring" -f /docker-entrypoint-initdb.d/005_device_serial.sql
