import pytest

from eugene_bus_reliability.gtfs_static import gtfs_date, gtfs_time_to_seconds


@pytest.mark.parametrize(
    "value,expected",
    [
        ("00:00:00", 0),
        ("7:05:30", 7 * 3600 + 5 * 60 + 30),
        ("23:58:00", 23 * 3600 + 58 * 60),
        ("24:05:00", 24 * 3600 + 5 * 60),  # past midnight, same service day
        ("25:10:00", 25 * 3600 + 10 * 60),
        ("", None),
        (None, None),
    ],
)
def test_gtfs_time_to_seconds(value, expected):
    assert gtfs_time_to_seconds(value) == expected


@pytest.mark.parametrize("bad", ["24:60:00", "12:00", "abc"])
def test_gtfs_time_rejects_malformed(bad):
    with pytest.raises(ValueError):
        gtfs_time_to_seconds(bad)


def test_gtfs_date():
    assert gtfs_date("20260906").isoformat() == "2026-09-06"
    assert gtfs_date("") is None


def test_rows_are_python_typed(gtfs_zip_bytes):
    """Ints must be ints, not floats, or COPY into integer columns fails."""
    import io
    import zipfile

    from eugene_bus_reliability.gtfs_static import rows_for

    zf = zipfile.ZipFile(io.BytesIO(gtfs_zip_bytes))
    stop_times = list(rows_for(zf, "stop_times.txt", 1))
    assert len(stop_times) == 3
    fv, trip_id, seq, stop_id, arr, dep, timepoint, *_ = stop_times[2]
    assert (fv, trip_id, seq, stop_id) == (1, "T1", 3, "S3")
    assert arr == 24 * 3600 + 5 * 60 and type(arr) is int
    assert type(timepoint) is int
    stops = list(rows_for(zf, "stops.txt", 1))
    assert type(stops[0][6]) is int  # location_type defaulted to 0, as int
    cal = list(rows_for(zf, "calendar.txt", 1))
    assert cal[0][2] is True and cal[0][7] is False
