-- Static GTFS. Every table carries feed_version_id because LTD publishes a new
-- schedule roughly three times a year and realtime data must be matched to the
-- schedule that was in force when it was observed, not the one current today.

CREATE TABLE gtfs.feed_version (
    feed_version_id   integer      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sha256            bytea        NOT NULL UNIQUE,
    source_url        text         NOT NULL,
    downloaded_at     timestamptz  NOT NULL DEFAULT now(),
    feed_start_date   date,                      -- from feed_info.txt if present
    feed_end_date     date,
    feed_version      text,                      -- publisher's own label, if any
    byte_size         integer      NOT NULL
);

CREATE TABLE gtfs.agency (
    feed_version_id  integer NOT NULL REFERENCES gtfs.feed_version,
    agency_id        text    NOT NULL DEFAULT '',
    agency_name      text    NOT NULL,
    agency_url       text,
    agency_timezone  text    NOT NULL,
    PRIMARY KEY (feed_version_id, agency_id)
);

CREATE TABLE gtfs.routes (
    feed_version_id   integer  NOT NULL REFERENCES gtfs.feed_version,
    route_id          text     NOT NULL,
    agency_id         text     NOT NULL DEFAULT '',
    route_short_name  text,                      -- "11", "EmX"
    route_long_name   text,
    route_type        smallint NOT NULL,         -- 3 = bus
    route_color       text,
    PRIMARY KEY (feed_version_id, route_id)
);

CREATE TABLE gtfs.stops (
    feed_version_id  integer          NOT NULL REFERENCES gtfs.feed_version,
    stop_id          text             NOT NULL,
    stop_code        text,                       -- the number printed on the sign
    stop_name        text             NOT NULL,
    stop_lat         double precision NOT NULL,
    stop_lon         double precision NOT NULL,
    location_type    smallint         NOT NULL DEFAULT 0,   -- 0 stop, 1 station
    parent_station   text,
    geom             geography(Point, 4326)
        GENERATED ALWAYS AS (ST_SetSRID(ST_MakePoint(stop_lon, stop_lat), 4326)::geography) STORED,
    PRIMARY KEY (feed_version_id, stop_id)
);
CREATE INDEX stops_geom_idx ON gtfs.stops USING gist (geom);

CREATE TABLE gtfs.calendar (
    feed_version_id  integer NOT NULL REFERENCES gtfs.feed_version,
    service_id       text    NOT NULL,
    monday           boolean NOT NULL,
    tuesday          boolean NOT NULL,
    wednesday        boolean NOT NULL,
    thursday         boolean NOT NULL,
    friday           boolean NOT NULL,
    saturday         boolean NOT NULL,
    sunday           boolean NOT NULL,
    start_date       date    NOT NULL,
    end_date         date    NOT NULL,
    PRIMARY KEY (feed_version_id, service_id)
);

CREATE TABLE gtfs.calendar_dates (
    feed_version_id  integer  NOT NULL REFERENCES gtfs.feed_version,
    service_id       text     NOT NULL,
    date             date     NOT NULL,
    exception_type   smallint NOT NULL CHECK (exception_type IN (1, 2)),  -- 1 added, 2 removed
    PRIMARY KEY (feed_version_id, service_id, date)
);

CREATE TABLE gtfs.trips (
    feed_version_id  integer  NOT NULL REFERENCES gtfs.feed_version,
    trip_id          text     NOT NULL,
    route_id         text     NOT NULL,
    service_id       text     NOT NULL,
    trip_headsign    text,
    direction_id     smallint,                   -- 0/1, outbound/inbound
    block_id         text,                       -- the vehicle assignment (same bus runs a block of trips)
    shape_id         text,
    PRIMARY KEY (feed_version_id, trip_id),
    FOREIGN KEY (feed_version_id, route_id) REFERENCES gtfs.routes
);
CREATE INDEX trips_route_idx ON gtfs.trips (feed_version_id, route_id);

-- Times are stored as seconds after "noon minus 12h" on the service day, the
-- GTFS convention, so values past 86400 are legal and mean "after midnight on
-- the same service day". Converting to a wall-clock timestamp requires the
-- service date and is done in dbt (int_scheduled_stop_events).
CREATE TABLE gtfs.stop_times (
    feed_version_id     integer  NOT NULL REFERENCES gtfs.feed_version,
    trip_id             text     NOT NULL,
    stop_sequence       integer  NOT NULL,       -- 1, 2, 3 ... along the trip
    stop_id             text     NOT NULL,
    arrival_seconds     integer,
    departure_seconds   integer,
    timepoint           smallint,           -- 1 = exact timepoint, 0 = approximate, NULL = unknown
    pickup_type         smallint,
    drop_off_type       smallint,
    shape_dist_traveled double precision,
    PRIMARY KEY (feed_version_id, trip_id, stop_sequence),
    FOREIGN KEY (feed_version_id, trip_id) REFERENCES gtfs.trips,
    FOREIGN KEY (feed_version_id, stop_id) REFERENCES gtfs.stops
);
CREATE INDEX stop_times_stop_idx ON gtfs.stop_times (feed_version_id, stop_id);

CREATE TABLE gtfs.shapes (
    feed_version_id      integer          NOT NULL REFERENCES gtfs.feed_version,
    shape_id             text             NOT NULL,
    shape_pt_sequence    integer          NOT NULL,
    shape_pt_lat         double precision NOT NULL,
    shape_pt_lon         double precision NOT NULL,
    shape_dist_traveled  double precision,
    PRIMARY KEY (feed_version_id, shape_id, shape_pt_sequence)
);
