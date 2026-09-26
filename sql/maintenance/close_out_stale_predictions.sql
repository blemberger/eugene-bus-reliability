-- Move predictions for service days that ended more than two days ago from
-- rt.prediction_current to rt.prediction_history, so the upsert target stays
-- bounded (roughly one service day of trip-stops). Idempotent; run daily.
--
-- Both column lists are explicit. Migrations append columns with ALTER TABLE ADD
-- COLUMN, so the two tables' physical column orders differ on any database created
-- before the migration (history has closed_at before scheduled_time; current has
-- no closed_at). A positional `SELECT moved.*` would put values in the wrong columns.
WITH moved AS (
    DELETE FROM rt.prediction_current
    WHERE start_date < current_date - 2
    RETURNING trip_id, start_date, stop_sequence, stop_id,
              arrival_time, departure_time, arrival_delay, departure_delay,
              schedule_relationship, scheduled_time,
              first_seen_at, last_seen_at, first_fetch_id
)
INSERT INTO rt.prediction_history
    (trip_id, start_date, stop_sequence, stop_id,
     arrival_time, departure_time, arrival_delay, departure_delay,
     schedule_relationship, scheduled_time,
     first_seen_at, last_seen_at, first_fetch_id, closed_at)
SELECT trip_id, start_date, stop_sequence, stop_id,
       arrival_time, departure_time, arrival_delay, departure_delay,
       schedule_relationship, scheduled_time,
       first_seen_at, last_seen_at, first_fetch_id, now()
FROM moved
ON CONFLICT DO NOTHING;