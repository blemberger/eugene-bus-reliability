"""The plain report pages' wording and addresses (no database)."""

from eugene_bus_reliability import static_site as ss


def test_range_words():
    assert ss.range_words(-70, 250) == "between 1 min early and 4 min late"
    assert ss.range_words(10, 200) == "between on time and 3 min late"


def test_delay_words():
    assert ss.delay_words(96) == "1.6 min late"
    assert ss.delay_words(-24) == "0.4 min early"
    assert ss.delay_words(2) == "on time"


def test_base_url(monkeypatch):
    monkeypatch.setenv("SITE_ADDRESS", "eugenebuswatch.com www.eugenebuswatch.com")
    assert ss.base_url() == "https://eugenebuswatch.com"
    monkeypatch.setenv("SITE_ADDRESS", ":80")
    assert ss.base_url() is None


def test_hour_chart_needs_three_hours():
    row = {"n": 50, "median_delay_s": 60, "p10_delay_s": 0, "p90_delay_s": 200}
    assert ss.hour_chart([{**row, "hour_local": 7}]) == ""
    svg = ss.hour_chart([{**row, "hour_local": h} for h in (7, 8, 9)])
    assert svg.count("<svg") == 2 and "on time" in svg
