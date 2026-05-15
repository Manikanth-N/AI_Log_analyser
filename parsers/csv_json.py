"""CSV and JSON log importer. Expects timestamp column in microseconds or ISO8601."""

import json
from pathlib import Path
from typing import Callable, Iterator

import polars as pl
import structlog

from .base import AbstractLogParser
from .schema import AutopilotType, LogFormat, LogMetadata, TIMESTAMP_COL, VehicleClass

log = structlog.get_logger(__name__)

_TIMESTAMP_ALIASES = ["timestamp_us", "TimeUS", "timestamp", "time_us", "time", "t"]


class CsvJsonParser(AbstractLogParser):
    FORMAT = LogFormat.CSV

    def detect(self) -> bool:
        return True  # Fallback parser

    def _load_df(self) -> pl.DataFrame:
        suffix = self.file_path.suffix.lower()
        if suffix == ".json":
            with open(self.file_path) as f:
                data = json.load(f)
            if isinstance(data, list):
                return pl.DataFrame(data)
            elif isinstance(data, dict):
                return pl.DataFrame([data])
        return pl.read_csv(self.file_path, infer_schema_length=10000)

    def _find_timestamp_col(self, df: pl.DataFrame) -> str | None:
        for alias in _TIMESTAMP_ALIASES:
            if alias in df.columns:
                return alias
        return None

    def _normalize_timestamps(self, df: pl.DataFrame, ts_col: str) -> pl.DataFrame:
        col = df[ts_col]
        if col.dtype == pl.Utf8:
            # ISO8601 → microseconds
            df = df.with_columns(
                pl.col(ts_col).str.to_datetime().dt.timestamp("us").alias(TIMESTAMP_COL)
            )
        elif col.max() < 1e12:
            # Seconds → microseconds
            df = df.with_columns(
                (pl.col(ts_col) * 1_000_000).cast(pl.Int64).alias(TIMESTAMP_COL)
            )
        else:
            df = df.with_columns(pl.col(ts_col).cast(pl.Int64).alias(TIMESTAMP_COL))

        if ts_col != TIMESTAMP_COL:
            df = df.drop(ts_col)
        return df

    def parse_metadata(self) -> LogMetadata:
        df = self._load_df()
        ts_col = self._find_timestamp_col(df)
        n = len(df)
        dur = 0
        start_ts = 0
        end_ts = 0
        if ts_col:
            start_ts = int(df[ts_col].min() or 0)
            end_ts = int(df[ts_col].max() or 0)
            dur = end_ts - start_ts

        return LogMetadata(
            format=LogFormat.CSV,
            autopilot=AutopilotType.UNKNOWN,
            vehicle_class=VehicleClass.UNKNOWN,
            firmware_version=None,
            firmware_hash=None,
            log_start_us=start_ts,
            log_end_us=end_ts,
            duration_us=dur,
            message_types=["CSV"],
            message_counts={"CSV": n},
            sample_rates_hz={},
            missing_critical=[],
            vehicle_id=None,
            parameter_count=0,
            file_size_bytes=self.file_path.stat().st_size,
            file_sha256=self.sha256(self.file_path),
        )

    def stream_messages(
        self,
        message_types: list[str] | None = None,
        progress_cb: Callable[[int, int], None] | None = None,
    ) -> Iterator[dict]:
        df = self._load_df()
        ts_col = self._find_timestamp_col(df)
        if ts_col:
            df = self._normalize_timestamps(df, ts_col)
        for row in df.iter_rows(named=True):
            row["_type"] = "CSV"
            yield row

    def parse_to_dataframes(
        self,
        output_dir: Path,
        message_types: list[str] | None = None,
        progress_cb: Callable[[float], None] | None = None,
    ) -> dict[str, Path]:
        output_dir.mkdir(parents=True, exist_ok=True)
        df = self._load_df()
        ts_col = self._find_timestamp_col(df)
        if ts_col:
            df = self._normalize_timestamps(df, ts_col)
        if TIMESTAMP_COL in df.columns:
            df = df.sort(TIMESTAMP_COL)
        out_path = output_dir / "CSV.parquet"
        df.write_parquet(out_path)
        return {"CSV": out_path}
