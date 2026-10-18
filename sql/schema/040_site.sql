-- Page views on the public site, recorded by the app (log_page_view in app/common.py): when,
-- which page, phone or computer, a random id per browser tab (so visits can be counted), and
-- a visitor code that is the same for one browser all day (so a day's visitors can be counted,
-- as WordPress stats do): made from the browser's internet address and type mixed with a
-- random value that changes every day (site.visitor_salt) and is deleted the next day. No IP
-- address, cookie or anything else that identifies a person is stored.
-- The site's read-only login may add rows but not read them; the visitor report
-- (`make visitors`) runs as the owner. Kept in step with sql/migrations/002_site_page_views.sql,
-- 005_page_view_load_time.sql and 006_page_view_visitor.sql.
CREATE SCHEMA IF NOT EXISTS site;
CREATE TABLE IF NOT EXISTS site.page_view (
    viewed_at  timestamptz NOT NULL DEFAULT now(),
    session_id text        NOT NULL,
    page       text        NOT NULL,
    detail     text,
    device     text        NOT NULL CHECK (device IN ('phone', 'computer', 'bot')),
    -- how long the page took to draw on the server, in milliseconds
    load_ms    integer,
    -- the same for one browser all day, different the next (see above)
    visitor    text
);
CREATE INDEX IF NOT EXISTS page_view_viewed_at_idx ON site.page_view USING brin (viewed_at);
GRANT USAGE ON SCHEMA site TO ltd_reader;
GRANT INSERT ON site.page_view TO ltd_reader;
-- the day's random value behind the visitor code; yesterday's and older are deleted
CREATE TABLE IF NOT EXISTS site.visitor_salt (
    day  date PRIMARY KEY,
    salt text NOT NULL DEFAULT md5(random()::text || clock_timestamp()::text)
);
GRANT SELECT, INSERT, UPDATE, DELETE ON site.visitor_salt TO ltd_reader;
-- dbt's default privileges give the read-only login SELECT on every new table; not this one,
-- so the site's data explorer can't list who looked at what.
REVOKE SELECT ON site.page_view FROM ltd_reader;