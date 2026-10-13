-- How long each page took to draw on the server (the app's time, mostly waiting on the
-- database), in milliseconds, so slow page loads can be seen and timed against the analysis
-- builds. Same as sql/schema/040_site.sql, for existing databases.
ALTER TABLE site.page_view ADD COLUMN IF NOT EXISTS load_ms integer;