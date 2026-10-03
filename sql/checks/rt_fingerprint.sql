-- A fingerprint of the realtime tables: row counts plus a hash of every row that a
-- rebuild from the raw archive must reproduce. Run it before and after `make rebuild`
-- (with the poller stopped) and the two outputs must be identical.
-- Excluded: fetch ids and fetched_at (a replay numbers fetches differently and uses the
-- feed's clock for fetched_at), and rt.fetch_unchanged (counts of discarded polls,
-- which are not archived).
-- Run: make fingerprint
select 'fetches' as tbl, count(*) as n,
       md5(string_agg(concat_ws('|', feed, header_timestamp, entity_count, byte_size,
                                encode(sha256, 'hex'), archive_path),
                      ',' order by feed, header_timestamp)) as fingerprint
from rt.fetch
union all
select 'predictions (current)', count(*),
       md5(string_agg(concat_ws('|', trip_id, start_date, stop_sequence, stop_id, arrival_time,
                                departure_time, scheduled_time, first_seen_at, last_seen_at),
                      ',' order by trip_id, start_date, stop_sequence))
from rt.prediction_current
union all
select 'predictions (history)', count(*),
       md5(string_agg(concat_ws('|', trip_id, start_date, stop_sequence, arrival_time,
                                departure_time, first_seen_at, last_seen_at, closed_at),
                      ',' order by trip_id, start_date, stop_sequence, first_seen_at))
from rt.prediction_history
union all
select 'vehicle positions', count(*),
       md5(string_agg(concat_ws('|', vehicle_id, position_timestamp, latitude, longitude, trip_id,
                                start_date),
                      ',' order by vehicle_id, position_timestamp))
from rt.vehicle_position
union all
select 'trip updates', count(*),
       md5(string_agg(concat_ws('|', trip_id, start_date, vehicle_id, trip_delay_seconds),
                      ',' order by trip_id, start_date, vehicle_id, trip_delay_seconds))
from rt.trip_update
union all
select 'alerts', count(*),
       md5(string_agg(concat_ws('|', alert_id, payload::text), ',' order by alert_id, payload::text))
from rt.alert;