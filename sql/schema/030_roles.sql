-- Read-only role for the dashboard. On a new database this runs once, after the
-- tables exist.
-- The password is not set here (schema files run before .env is read);
-- `make migrate` sets it from READER_PASSWORD.
-- The dbt schemas (staging, intermediate, marts, analytics) are created by dbt, so
-- their grants live in dbt/dbt_project.yml's on-run-end hook, not here.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ltd_reader') THEN
        CREATE ROLE ltd_reader LOGIN;
    END IF;
END $$;
ALTER ROLE ltd_reader SET statement_timeout = '20s';
ALTER ROLE ltd_reader SET default_transaction_read_only = on;
GRANT USAGE ON SCHEMA gtfs, rt TO ltd_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA gtfs, rt TO ltd_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA gtfs, rt GRANT SELECT ON TABLES TO ltd_reader;