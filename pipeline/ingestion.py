"""
Log ingestion pipeline: parse → normalize → write Parquet → extract events → detect phases.
Entry point for all log processing. Runs as a Celery task.
"""

import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import structlog

from config.settings import settings
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


class IngestionPipeline:
    def __init__(
        self,
        flight_id: str,
        source_path: Path,
        store: ParquetStore | None = None,
        progress_cb: Callable[[str, float], None] | None = None,
    ):
        self.flight_id = flight_id
        self.source_path = source_path
        self.store = store or ParquetStore()
        self.progress_cb = progress_cb or (lambda stage, pct: None)

    def run(self) -> IngestionResult:
        start = time.monotonic()

        # 1. Copy raw file to content-addressed storage
        self.progress_cb("copying", 0.0)
        raw_path = self._copy_raw()

        # 2. Parse to Parquet
        self.progress_cb("parsing", 0.05)
        parser = get_parser(self.source_path)
        metadata = parser.parse_metadata()

        parsed_dir = self.store.parsed_dir(self.flight_id)
        parsed_dir.mkdir(parents=True, exist_ok=True)

        parquet_paths = parser.parse_to_dataframes(
            output_dir=parsed_dir,
            progress_cb=lambda n: self.progress_cb("parsing", 0.05 + 0.60 * (n / max(1, sum(metadata.message_counts.values())))),
        )

        self.progress_cb("extracting_events", 0.70)

        # 3. Extract events and phases
        from pipeline.event_extractor import EventExtractor
        from pipeline.phase_detector import PhaseDetector

        extractor = EventExtractor(self.flight_id, self.store)
        timeline = extractor.extract()

        detector = PhaseDetector(self.flight_id, self.store)
        phases = detector.detect(timeline)

        # 4. Save derived results
        self.progress_cb("saving_derived", 0.90)
        self.store.write_derived(self.flight_id, "timeline", timeline)
        self.store.write_derived(self.flight_id, "phases", phases)

        # 5. Run fast anomaly pass
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
            parquet_paths=parquet_paths,
            timeline=timeline,
            phases=phases,
            duration_s=elapsed,
            rows_parsed=rows,
        )

    def _copy_raw(self) -> Path:
        raw_dir = settings.raw_storage / self.flight_id
        raw_dir.mkdir(parents=True, exist_ok=True)
        dest = raw_dir / self.source_path.name
        if not dest.exists():
            shutil.copy2(self.source_path, dest)
        return dest
