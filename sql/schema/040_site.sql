-- Page views on the public site, recorded by the app (log_page_view in app/common.py): when,
-- which page, phone or computer, and a random id per browser tab, so visits can be counted.
-- No IP address, cookie or anything else that identifies a person is stored.
-- The site's read-only login may add rows but not read them; the visitor report
-- (`make visitors`) runs as the owner. Kept in step with sql/migrations/002_site_page_views.sql.
CREATE SCHEMA IF NOT EXISTS site;
CREATE TABLE IF NOT EXISTS site.page_view (
    viewed_at  timestamptz NOT NULL DEFAULT now(),
    session_id text        NOT NULL,
    page       text        NOT NULL,
    detail     text,
    device     text        NOT NULL CHECK (device IN ('phone', 'computer', 'bot'))
);
CREATE INDEX IF NOT EXISTS page_view_viewed_at_idx ON site.page_view USING brin (viewed_at);
GRANT USAGE ON SCHEMA site TO ltd_reader;
GRANT INSERT ON site.page_view TO ltd_reader;
-- dbt's default privileges give the read-only login SELECT on every new table; not this one,
-- so the site's data explorer can't list who looked at what.
REVOKE SELECT ON site.page_view FROM ltd_reader;