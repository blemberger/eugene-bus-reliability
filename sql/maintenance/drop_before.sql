-- Delete everything collected before a date (Eugene time), so the site's history starts that
-- day: used once to drop the trial runs of September 18-25, 2026, when collection was still
-- intermittent. Run with `make server-drop-before BEFORE=YYYY-MM-DD`, which first moves the
-- matching raw archive files aside so `make replay` can't bring them back. One transaction:
-- either everything goes or nothing does. It takes a few minutes and prints each step.
\set ON_ERROR_STOP on

-- an earlier run of this script that is still going (e.g. its terminal was closed) is stopped
select count(pg_terminate_backend(pid)) as earlier_runs_stopped
from pg_stat_activity
where pid <> pg_backend_pid() and query like '%old_fetch%';

begin;
create temp table old_fetch on commit drop as
    select fetch_id from rt.fetch
    where fetched_at < (:'before'::date)::timestamp at time zone 'America/Los_Angeles';
create unique index on old_fetch (fetch_id);
analyze old_fetch;
select count(*) as messages_to_delete from old_fetch;

\echo deleting predictions...
delete from rt.prediction_history
    where start_date < :'before'::date or first_fetch_id in (select fetch_id from old_fetch);
delete from rt.prediction_current
    where start_date < :'before'::date or first_fetch_id in (select fetch_id from old_fetch);
\echo deleting bus positions...
delete from rt.vehicle_position where fetch_id in (select fetch_id from old_fetch);
\echo deleting trip updates and alerts...
delete from rt.trip_update where fetch_id in (select fetch_id from old_fetch);
delete from rt.alert where fetch_id in (select fetch_id from old_fetch);

\echo deleting the messages themselves...
-- Everything that points at these messages is already gone (above). Postgres would still
-- check that for every deleted message, one full scan of the positions table each (they
-- have no index on fetch_id): tens of thousands of scans, hours of work. That is the step
-- that never finished on the first try. The check is switched off for this one statement and
-- done once afterwards instead.
set local session_replication_role = replica;
delete from rt.fetch where fetch_id in (select fetch_id from old_fetch);
set local session_replication_role = origin;
do $$
begin
    if exists (select 1 from rt.vehicle_position v where not exists (select 1 from rt.fetch f where f.fetch_id = v.fetch_id))
       or exists (select 1 from rt.prediction_current p where not exists (select 1 from rt.fetch f where f.fetch_id = p.first_fetch_id))
       or exists (select 1 from rt.trip_update t where not exists (select 1 from rt.fetch f where f.fetch_id = t.fetch_id))
       or exists (select 1 from rt.alert a where not exists (select 1 from rt.fetch f where f.fetch_id = a.fetch_id))
    then
        raise exception 'rows still refer to deleted messages; nothing was deleted';
    end if;
end
$$;
delete from rt.fetch_unchanged
    where hour < (:'before'::date)::timestamp at time zone 'America/Los_Angeles';

-- the next analysis build recomputes every day from what remains (a full refresh)
create table if not exists analytics.build_code (version text, refreshed_at timestamptz);
delete from analytics.build_code;
commit;

\echo tidying up the freed space...
vacuum (analyze) rt.prediction_history, rt.prediction_current, rt.vehicle_position, rt.trip_update, rt.alert, rt.fetch;
select min(fetched_at at time zone 'America/Los_Angeles') as collection_now_starts from rt.fetch;