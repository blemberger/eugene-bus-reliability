-- Delete everything collected before a date (Eugene time), so the site's history starts that
-- day: used once to drop the trial runs of September 18-25, 2026, when collection was still
-- intermittent. Run with `make server-drop-before BEFORE=YYYY-MM-DD`, which first moves the
-- matching raw archive files aside so `make replay` can't bring them back. One transaction:
-- either everything goes or nothing does. The next analysis build recomputes from what remains.
\set ON_ERROR_STOP on
begin;
create temp table old_fetch on commit drop as
    select fetch_id from rt.fetch
    where fetched_at < (:'before'::date)::timestamp at time zone 'America/Los_Angeles';
delete from rt.prediction_history
    where start_date < :'before'::date or first_fetch_id in (select fetch_id from old_fetch);
delete from rt.prediction_current
    where start_date < :'before'::date or first_fetch_id in (select fetch_id from old_fetch);
delete from rt.vehicle_position where fetch_id in (select fetch_id from old_fetch);
delete from rt.trip_update where fetch_id in (select fetch_id from old_fetch);
delete from rt.alert where fetch_id in (select fetch_id from old_fetch);
delete from rt.fetch where fetch_id in (select fetch_id from old_fetch);
delete from rt.fetch_unchanged
    where hour < (:'before'::date)::timestamp at time zone 'America/Los_Angeles';
commit;
-- the incremental analysis table keeps old days unless told otherwise (if it exists yet)
select format('delete from intermediate.int_positions_along_shape where service_date < %L', :'before')
where to_regclass('intermediate.int_positions_along_shape') is not null \gexec
select min(fetched_at at time zone 'America/Los_Angeles') as collection_now_starts from rt.fetch;