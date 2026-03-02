import json
import logging
import os
import threading
import urllib.request

logger = logging.getLogger(__name__)

_listener_count: int | None = None
_metrics_thread: threading.Thread | None = None
_stop_event = threading.Event()

_ICECAST_URL = "http://icecast:8000/status-json.xsl"
_POLL_INTERVAL_S = 60


def get_listener_count() -> int | None:
    """Return the most recently polled listener count, or None if not yet available."""
    return _listener_count


def _fetch_listener_count() -> int | None:
    try:
        with urllib.request.urlopen(_ICECAST_URL, timeout=5) as resp:  # noqa: S310
            data = json.loads(resp.read())
        source = data.get("icestats", {}).get("source", {})
        # source is a dict for a single mount, list for multiple mounts
        if isinstance(source, list):
            return sum(int(s.get("listeners", 0)) for s in source)
        return int(source.get("listeners", 0))
    except Exception as e:
        logger.warning("Failed to fetch Icecast listener count: %s", e)
        return None


def _push_cloudwatch(count: int) -> None:
    try:
        import boto3  # type: ignore[import-untyped]

        hostname = os.environ.get("SERVER_HOSTNAME", "unknown")
        region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
        client = boto3.client("cloudwatch", region_name=region)
        client.put_metric_data(
            Namespace="FamilyRadio",
            MetricData=[
                {
                    "MetricName": "ListenerCount",
                    "Dimensions": [{"Name": "Station", "Value": hostname}],
                    "Value": float(count),
                    "Unit": "Count",
                }
            ],
        )
    except Exception as e:
        logger.warning("Failed to push CloudWatch metric: %s", e)


def _metrics_loop() -> None:
    global _listener_count
    logger.info("Metrics poller started")
    while not _stop_event.is_set():
        count = _fetch_listener_count()
        if count is not None:
            _listener_count = count
            _push_cloudwatch(count)
        _stop_event.wait(timeout=_POLL_INTERVAL_S)
    logger.info("Metrics poller stopped")


def start_metrics_poller() -> None:
    global _metrics_thread
    _stop_event.clear()
    _metrics_thread = threading.Thread(target=_metrics_loop, daemon=True, name="metrics-poller")
    _metrics_thread.start()


def stop_metrics_poller() -> None:
    _stop_event.set()
    if _metrics_thread:
        _metrics_thread.join(timeout=10)
