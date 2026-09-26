"""Configuration from environment variables (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise RuntimeError(f"missing required environment variable {name}")
    return value


@dataclass(frozen=True)
class Settings:
    database_url: str
    gtfs_static_url: str
    trip_updates_url: str
    vehicle_positions_url: str
    alerts_url: str
    poll_interval_seconds: float
    raw_archive_dir: Path

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            database_url=_env("DATABASE_URL"),
            gtfs_static_url=_env(
                "GTFS_STATIC_URL",
                "https://feed.ltd.org/TMGTFSRealTimeWebService/GTFS/google_transit.zip",
            ),
            trip_updates_url=_env(
                "GTFS_RT_TRIP_UPDATES_URL",
                "https://feed.ltd.org/TMGTFSRealTimeWebService/TripUpdate/TripUpdates.pb",
            ),
            vehicle_positions_url=_env(
                "GTFS_RT_VEHICLE_POSITIONS_URL",
                "https://feed.ltd.org/TMGTFSRealTimeWebService/Vehicle/VehiclePositions.pb",
            ),
            alerts_url=_env(
                "GTFS_RT_ALERTS_URL",
                "https://feed.ltd.org/TMGTFSRealTimeWebService/Alert/Alerts.pb",
            ),
            poll_interval_seconds=float(_env("POLL_INTERVAL_SECONDS", "30")),
            raw_archive_dir=Path(_env("RAW_ARCHIVE_DIR", "./data/raw")),
        )
