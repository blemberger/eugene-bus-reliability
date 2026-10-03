"""Eugene Bus Watch pipeline: collects Lane Transit District's schedule and realtime feeds and
loads them into Postgres for the reliability analysis. Website: https://eugenebuswatch.com"""

__version__ = "0.1.0"

# Sent with every request, so LTD can see who is fetching its feeds and how to reach us.
USER_AGENT = (
    f"eugene-bus-reliability/{__version__} "
    "(+https://eugenebuswatch.com; https://github.com/blemberger/eugene-bus-reliability)"
)
