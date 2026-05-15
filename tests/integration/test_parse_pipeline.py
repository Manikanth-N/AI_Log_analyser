"""
Integration test: upload → parse worker → flight ready.

Tier: API-level (requires API server + Celery parse worker).
Speed: ~2-5 min (29 MB BIN parse time).
Marks: requires_workers, slow

What is tested:
  1. POST /api/v1/flights/upload returns 200 with a flight_id
  2. Flight record is created in DB with status "parsing"
  3. Parse worker completes and status transitions to "ready"
  4. Flight metadata is populated (duration, message_types, fw_version)
  5. Parquet storage directory exists with expected message type files
  6. Anomaly count in DB > 0 (deterministic rules fired)
  7. Derived "phases" and "timeline" JSON files written
  8. Cleanup removes all created records and files
"""

import pytest

from tests.integration.conftest import PARSE_TIMEOUT_S, poll_until

pytestmark = [pytest.mark.slow, pytest.mark.requires_workers]


# ── helpers ───────────────────────────────────────────────────────────────────

def _flight_status(api_client, flight_id: str) -> str:
    r = api_client.get(f"/api/v1/flights/{flight_id}")
    r.raise_for_status()
    return r.json()["status"]


# ── tests ─────────────────────────────────────────────────────────────────────

def test_upload_returns_flight_id(api_client, fixture_log, tracked_flight_ids):
    """Upload succeeds and returns a parseable flight_id UUID."""
    with open(fixture_log, "rb") as fh:
        r = api_client.post(
            "/api/v1/flights/upload",
            files={"file": (fixture_log.name, fh, "application/octet-stream")},
        )

    assert r.status_code == 200, f"Upload failed: {r.text}"
    body = r.json()
    assert "flight_id" in body, "Response missing flight_id"

    flight_id = body["flight_id"]
    tracked_flight_ids.append(flight_id)

    # flight_id must be a valid UUID (no ValueError means it parsed)
    import uuid
    uuid.UUID(flight_id)


def test_parse_pipeline_end_to_end(api_client, fixture_log, tracked_flight_ids):
    """
    Full parse pipeline: upload → queued → parsing → ready.

    Assertions:
      - flight status reaches "ready" within PARSE_TIMEOUT_S
      - fw_version populated by parser
      - message_types list is non-empty
      - duration_s is positive
      - Parquet files exist on disk
      - Derived timeline + phases JSON written
      - Anomalies persisted to DB
    """
    from config.settings import settings
    from storage.metadata_db import MetadataDB

    # 1. Upload
    with open(fixture_log, "rb") as fh:
        r = api_client.post(
            "/api/v1/flights/upload",
            files={"file": (fixture_log.name, fh, "application/octet-stream")},
        )
    assert r.status_code == 200
    flight_id = r.json()["flight_id"]
    tracked_flight_ids.append(flight_id)

    # 2. Poll until ready
    poll_until(
        lambda: _flight_status(api_client, flight_id) in ("ready", "error"),
        timeout_s=PARSE_TIMEOUT_S,
        description=f"flight {flight_id} to reach ready/error",
    )

    # 3. Status must be ready, not error
    status = _flight_status(api_client, flight_id)
    assert status == "ready", f"Parse ended with status={status!r} (expected 'ready')"

    # 4. Flight metadata populated
    flight_data = api_client.get(f"/api/v1/flights/{flight_id}").json()
    assert flight_data["fw_version"], "fw_version not populated by parser"
    assert flight_data["message_types"], "message_types empty after parse"
    assert flight_data["duration_s"] and flight_data["duration_s"] > 0, "duration_s not set"

    # 5. Parquet files on disk
    flight_dir = settings.flights_storage / flight_id
    assert flight_dir.is_dir(), f"No Parquet directory at {flight_dir}"
    parquet_files = list(flight_dir.glob("*.parquet"))
    assert parquet_files, f"No .parquet files under {flight_dir}"

    # 6. Derived files written (timeline + phases)
    derived_dir = flight_dir / "derived"
    assert (derived_dir / "timeline.json").exists(), "timeline.json not written"
    assert (derived_dir / "phases.json").exists(), "phases.json not written"

    # 7. Anomalies in DB
    anomalies = api_client.get(f"/api/v1/flights/{flight_id}/anomalies").json()
    assert len(anomalies) > 0, "No anomalies persisted after parse (rules engine should fire)"

    # 8. Telemetry series API works for a known channel
    r = api_client.get(
        f"/api/v1/telemetry/{flight_id}/series",
        params={"channels": "ATT.roll_deg", "max_points": 50},
    )
    assert r.status_code == 200
    series = r.json()
    assert series, "Telemetry series API returned empty list"
    assert series[0]["channel"] == "ATT.roll_deg"
    assert len(series[0]["values"]) > 0
