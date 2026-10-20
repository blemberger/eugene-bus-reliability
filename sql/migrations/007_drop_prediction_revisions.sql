-- Counting visitors as WordPress (Jetpack) stats does, without storing who they are: each page
-- view gets a short code made from the browser's internet address and type, mixed with a
-- random value that changes every day (site.visitor_salt). The same browser gets the same code
-- all day, so a day's visitors can be counted; the address itself is never stored, and once
-- that day's random value is deleted (the next day) the code can't be matched back to it.
-- Same as sql/schema/040_site.sql, for existing databases.
ALTER TABLE site.page_view ADD COLUMN IF NOT EXISTS visitor text;
CREATE TABLE IF NOT EXISTS site.visitor_salt (
    day  date PRIMARY KEY,
    salt text NOT NULL DEFAULT md5(random()::text || clock_timestamp()::text)
);
GRANT SELECT, INSERT, UPDATE, DELETE ON site.visitor_salt TO ltd_reader;