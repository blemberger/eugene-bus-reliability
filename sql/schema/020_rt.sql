-- Realtime layer. Design rules:
--   * rt.fetch is one row per HTTP fetch that carried a NEW feed header timestamp.
--     Fetches whose header timestamp hasn't advanced are counted but not stored.
--   * vehicle positions are append-only, deduplicated on (vehicle_id, position_timestamp).
--   * stop-time predictions are stored CHANGE-ONLY: one row per distinct predicted
--     value per trip-stop, with the interval over which the feed asserted it.
--     Re-recording an unchanged prediction every poll would be ~10x the volume
--     and carry no information; the [first_seen_at, last_seen_at] interval carries all of it.

CREATE TYPE rt.feed_kind AS ENUM ('trip_updates', 'vehicle_positions', 'alerts');

CREATE TABLE rt.fetch (
    fetch_id          bigint       GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    feed              rt.feed_kind NOT NULL,
    fetched_at        timestamptz  NOT NULL,    -- our clock
    header_timestamp  timestamptz  NOT NULL,    -- the feed's clock: when the agency generated it
    entity_count      integer      NOT NULL,
    byte_size         integer      NOT NULL,
    sha256            bytea        NOT NULL,
    archive_path      text,                          -- relative path of the gzipped .pb
    UNIQUE (feed, header_timestamp)
);
CREATE INDEX fetch_fetched_at_idx ON rt.fetch (feed, fetched_at);

-- A fetch that returned the same header timestamp as the previous one. Kept as a
-- count only, so the poll cadence can be tuned to the feed's real update rate.
CREATE TABLE rt.fetch_unchanged (
    feed              rt.feed_kind NOT NULL,
    hour              timestamptz  NOT NULL,   -- truncated to the hour
    unchanged_count   integer      NOT NULL DEFAULT 0,
    PRIMARY KEY (feed, hour)
);

CREATE TABLE rt.vehicle_position (
    fetch_id                bigint           NOT NULL REFERENCES rt.fetch,
    vehicle_id              text             NOT NULL,
    position_timestamp      timestamptz      NOT NULL,   -- the vehicle's own report time
    trip_id                 text,
    route_id                text,
    start_date              date,                        -- service date of the trip
    latitude                double precision NOT NULL,
    longitude               double precision NOT NULL,
    bearing                 real,
    speed_mps               real,
    stop_id                 text,
    current_stop_sequence   integer,
    current_status          smallint,                    -- 0 incoming_at, 1 stopped_at, 2 in_transit_to
    occupancy_status        smallint,                    -- GTFS-RT OccupancyStatus enum (0 empty .. 6 full)
    occupancy_percentage    smallint,
    geom                    geography(Point, 4326)
        GENERATED ALWAYS AS (ST_SetSRID(ST_MakePoint(longitude, latitude), 4326)::geography) STORED,
    PRIMARY KEY (vehicle_id, position_timestamp)
);
CREATE INDEX vehicle_position_trip_idx ON rt.vehicle_position (trip_id, start_date, position_timestamp);
CREATE INDEX vehicle_position_ts_idx   ON rt.vehicle_position (position_timestamp);

-- One row per trip per fetch: trip-level fields of the TripUpdate.
CREATE TABLE rt.trip_update (
    fetch_id               bigint      NOT NULL REFERENCES rt.fetch,
    trip_id                text        NOT NULL,
    start_date             date,
    route_id               text,
    vehicle_id             text,
    schedule_relationship  smallint,                -- 0 scheduled, 1 added, 2 unscheduled, 3 canceled
    trip_timestamp         timestamptz,             -- TripUpdate.timestamp, if the feed sets it
    trip_delay_seconds     integer,
    PRIMARY KEY (fetch_id, trip_id)
);

-- Current prediction per trip-stop (upsert target). See rt.prediction_history
-- for superseded values. Together they give the full history of what the feed
-- predicted for each stop and when.
CREATE TABLE rt.prediction_current (
    trip_id                text        NOT NULL,
    start_date             date        NOT NULL,
    stop_sequence          integer     NOT NULL,
    stop_id                text,
    arrival_time           timestamptz,
    departure_time         timestamptz,
    arrival_delay          integer,                 -- seconds vs schedule, as the feed reports it
    departure_delay        integer,
    schedule_relationship  smallint,                -- 0 scheduled, 1 skipped, 2 no_data
    scheduled_time         timestamptz,             -- the feed's own scheduled time for this stop, if it sends one
    first_seen_at          timestamptz NOT NULL,    -- header timestamp of the fetch that first asserted this value
    last_seen_at           timestamptz NOT NULL,    -- header timestamp of the latest fetch still asserting it
    first_fetch_id         bigint      NOT NULL REFERENCES rt.fetch,
    PRIMARY KEY (trip_id, start_date, stop_sequence)
);
CREATE INDEX prediction_current_start_date_idx ON rt.prediction_current (start_date);

CREATE TABLE rt.prediction_history (
    LIKE rt.prediction_current INCLUDING DEFAULTS,
    -- header timestamp of the message that superseded this value (the feed's clock, so a
    -- rebuild from the archive reproduces it); for rows moved by the daily cleanup, when moved
    closed_at              timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (trip_id, start_date, stop_sequence, first_seen_at)
);
CREATE INDEX prediction_history_start_date_idx ON rt.prediction_history (start_date);
CREATE INDEX prediction_history_closed_at_idx ON rt.prediction_history USING brin (closed_at);

-- Alerts: stored whole as JSON per fetch. Low volume, rarely analyzed, kept for completeness.
CREATE TABLE rt.alert (
    fetch_id      bigint  NOT NULL REFERENCES rt.fetch,
    alert_id      text    NOT NULL,
    payload       jsonb   NOT NULL,
    PRIMARY KEY (fetch_id, alert_id)
);