"""
Log ingestion pipeline: fetch → parse → write Parquet → extract events → detect phases.

Accepts a gcs_uri string:
  - "gs://bucket/raw/..." — downloads from GCS, uploads results to GCS
  - "/local/path/to/file" — reads locally, writes to local storage backend
"""

import hashlib
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import structlog

from parsers.base import get_parser
from parsers.schema import LogMetadata
from storage.parquet_store import ParquetStore

log = structlog.get_logger(__name__)


@dataclass
class IngestionResult:
    flight_id: str
    metadata: LogMetadata
    parquet_paths: dict[str, Path]
    timeline: dict
    phases: list[dict]
    duration_s: float
    rows_parsed: int
    sha256: str | None = None


class IngestionPipeline:
    def __init__(
        self,
        flight_id: str,
        gcs_uri: str,
        store: ParquetStore | None = None,
        progress_cb: Callable[[str, float], None] | None = None,
    ):
        self.flight_id = flight_id
        self.gcs_uri = gcs_uri
        self.store = store or ParquetStore()
        self.progress_cb = progress_cb or (lambda stage, pct: None)

    def run(self) -> IngestionResult:
        with tempfile.TemporaryDirectory(prefix=f"ff_parse_{self.flight_id}_") as tmpdir:
            return self._run(Path(tmpdir))

    def _run(self, tmp: Path) -> IngestionResult:
        start = time.monotonic()

        # 1. Fetch raw file to local temp
        self.progress_cb("fetching", 0.0)
        local_raw, sha256 = self._fetch_raw(tmp)

        # 2. Parse to local parquet temp dir (parsers require Path input/output)
        self.progress_cb("parsing", 0.05)
        parser = get_parser(local_raw)
        metadata = parser.parse_metadata()

        local_parsed = tmp / "parsed"
        local_parsed.mkdir()

        total_rows = max(1, sum(metadata.message_counts.values()))
        parquet_paths = parser.parse_to_dataframes(
            output_dir=local_parsed,
            progress_cb=lambda n: self.progress_cb("parsing", 0.05 + 0.55 * (n / total_rows)),
        )

        # 3. Upload parquets to storage backend
        self.progress_cb("uploading", 0.65)
        self.store.upload_parsed(local_parsed, self.flight_id)

        # 4. Extract events and phases (these read via store)
        self.progress_cb("extracting_events", 0.70)

        from pipeline.event_extractor import EventExtractor
        from pipeline.phase_detector import PhaseDetector

        extractor = EventExtractor(self.flight_id, self.store)
        timeline = extractor.extract()

        detector = PhaseDetector(self.flight_id, self.store)
        phases = detector.detect(timeline)

        # 5. Save derived results
        self.progress_cb("saving_derived", 0.90)
        self.store.write_derived(self.flight_id, "timeline", timeline)
        self.store.write_derived(self.flight_id, "phases", phases)

        # 6. Run fast anomaly pass
        self.progress_cb("anomaly_detection", 0.93)
        from pipeline.anomaly_detector import FastAnomalyDetector
        detector_anom = FastAnomalyDetector(self.flight_id, self.store)
        anomalies = detector_anom.run_all_rules()
        self.store.write_derived(self.flight_id, "anomalies_fast", [
            {k: v for k, v in a.__dict__.items()} for a in anomalies
        ])

        self.progress_cb("complete", 1.0)
        elapsed = time.monotonic() - start
        rows = sum(metadata.message_counts.values())

        log.info(
            "ingestion_complete",
            flight_id=self.flight_id,
            duration_s=elapsed,
            rows=rows,
            message_types=len(parquet_paths),
            anomalies=len(anomalies),
        )

        return IngestionResult(
            flight_id=self.flight_id,
            metadata=metadata,
            parquet_paths={mtype: local_parsed / f"{mtype}.parquet" for mtype in parquet_paths},
            timeline=timeline,
            phases=phases,
            duration_s=elapsed,
            rows_parsed=rows,
            sha256=sha256,
        )

    def _fetch_raw(self, tmp: Path) -> tuple[Path, str | None]:
        """Fetch the raw log file. Returns (local_path, sha256)."""
        if self.gcs_uri.startswith("gs://"):
            filename = self.gcs_uri.rsplit("/", 1)[-1]
            local_path = tmp / filename
            self.store.download_raw(self.gcs_uri, local_path)
        else:
            local_path = Path(self.gcs_uri)

        sha256 = _compute_sha256(local_path)
        return local_path, sha256


def _compute_sha256(path: Path) -> str | None:
    try:
        sha = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                sha.update(chunk)
        return sha.hexdigest()
    except OSError:
        return None
