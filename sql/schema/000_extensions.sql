-- Runs once on first start of an empty database (docker-entrypoint-initdb.d).

CREATE EXTENSION IF NOT EXISTS postgis;

CREATE SCHEMA IF NOT EXISTS gtfs;   -- static schedule, versioned by feed download
CREATE SCHEMA IF NOT EXISTS rt;     -- realtime observations, append-only or change-only