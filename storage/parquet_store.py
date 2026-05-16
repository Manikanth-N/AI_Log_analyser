"""
Parquet-based telemetry store with pluggable storage backend.

LocalStorageBackend  — filesystem (local dev / tests)
GCSStorageBackend    — Google Cloud Storage (production)

Backend is selected automatically: GCS when GCS_DATA_BUCKET is set, local otherwise.
"""

from __future__ import annotations

import json
import shutil
from abc import ABC, abstractmethod
from pathlib import Path

import duckdb
import polars as pl

from config.settings import settings
from parsers.schema import TIMESTAMP_COL


# ── Storage backend abstraction ───────────────────────────────────────────────

class StorageBackend(ABC):
    @abstractmethod
    def makedirs(self, path: str) -> None:
        """Create directory hierarchy. No-op for GCS."""

    @abstractmethod
    def list_parquets(self, prefix: str) -> list[str]:
        """Return full paths/URIs of all .parquet files under prefix."""

    @abstractmethod
    def read_json(self, path: str) -> dict | list | None:
        """Read JSON from path/URI. Returns None if not found."""

    @abstractmethod
    def write_json(self, path: str, data: dict | list) -> None:
        """Write JSON to path/URI."""

    @abstractmethod
    def upload(self, local_path: Path, dest: str) -> None:
        """Copy local file to dest path/URI."""

    @abstractmethod
    def download(self, src: str, local_path: Path) -> None:
        """Download from src path/URI to local file."""


class LocalStorageBackend(StorageBackend):
    def makedirs(self, path: str) -> None:
        Path(path).mkdir(parents=True, exist_ok=True)

    def list_parquets(self, prefix: str) -> list[str]:
        d = Path(prefix)
        if not d.exists():
            return []
        return [str(p) for p in d.glob("*.parquet")]

    def read_json(self, path: str) -> dict | list | None:
        p = Path(path)
        if not p.exists():
            return None
        with open(p) as f:
            return json.load(f)

    def write_json(self, path: str, data: dict | list) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w") as f:
            json.dump(data, f, indent=2, default=str)

    def upload(self, local_path: Path, dest: str) -> None:
        dest_path = Path(dest)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        if str(local_path) != dest:
            shutil.copy2(local_path, dest_path)

    def download(self, src: str, local_path: Path) -> None:
        src_path = Path(src)
        if str(src_path) != str(local_path):
            shutil.copy2(src_path, local_path)


class GCSStorageBackend(StorageBackend):
    def __init__(self, bucket_name: str):
        self._bucket_name = bucket_name
        self._client = None

    @property
    def _gcs(self):
        if self._client is None:
            from google.cloud import storage
            self._client = storage.Client()
        return self._client

    def _blob_name(self, uri: str) -> str:
        """Strip gs://bucket/ prefix, returning just the blob name."""
        if uri.startswith("gs://"):
            _, _, rest = uri[5:].partition("/")
            return rest
        return uri

    def makedirs(self, path: str) -> None:
        pass  # GCS has no directory objects

    def list_parquets(self, prefix: str) -> list[str]:
        blob_prefix = self._blob_name(prefix)
        if blob_prefix and not blob_prefix.endswith("/"):
            blob_prefix += "/"
        bucket = self._gcs.bucket(self._bucket_name)
        return [
            f"gs://{self._bucket_name}/{b.name}"
            for b in bucket.list_blobs(prefix=blob_prefix)
            if b.name.endswith(".parquet")
        ]

    def read_json(self, path: str) -> dict | list | None:
        try:
            blob = self._gcs.bucket(self._bucket_name).blob(self._blob_name(path))
            return json.loads(blob.download_as_text())
        except Exception:
            return None

    def write_json(self, path: str, data: dict | list) -> None:
        blob = self._gcs.bucket(self._bucket_name).blob(self._blob_name(path))
        blob.upload_from_string(
            json.dumps(data, indent=2, default=str),
            content_type="application/json",
        )

    def upload(self, local_path: Path, dest: str) -> None:
        blob = self._gcs.bucket(self._bucket_name).blob(self._blob_name(dest))
        blob.upload_from_filename(str(local_path))

    def download(self, src: str, local_path: Path) -> None:
        blob = self._gcs.bucket(self._bucket_name).blob(self._blob_name(src))
        blob.download_to_filename(str(local_path))


def _default_backend() -> StorageBackend:
    if settings.gcs_data_bucket:
        return GCSStorageBackend(settings.gcs_data_bucket)
    return LocalStorageBackend()


# ── ParquetStore ──────────────────────────────────────────────────────────────

class ParquetStore:
    def __init__(self, backend: StorageBackend | None = None):
        self._backend = backend or _default_backend()
        if settings.gcs_data_bucket:
            self._root = f"gs://{settings.gcs_data_bucket}/flights"
        else:
            root = settings.storage_root / "flights"
            root.mkdir(parents=True, exist_ok=True)
            self._root = str(root)

    def _path(self, *parts: str) -> str:
        return "/".join([self._root] + list(parts))

    def parsed_dir(self, flight_id: str) -> str:
        return self._path(flight_id, "parsed")

    def derived_dir(self, flight_id: str) -> str:
        return self._path(flight_id, "derived")

    def available_types(self, flight_id: str) -> list[str]:
        parquets = self._backend.list_parquets(self.parsed_dir(flight_id))
        return [p.rsplit("/", 1)[-1].removesuffix(".parquet") for p in parquets]

    def load(
        self,
        flight_id: str,
        message_type: str,
        columns: list[str] | None = None,
        start_us: int | None = None,
        end_us: int | None = None,
    ) -> pl.DataFrame:
        path = self._path(flight_id, "parsed", f"{message_type}.parquet")
        try:
            df = pl.read_parquet(path, columns=columns)
        except Exception:
            return pl.DataFrame()

        if TIMESTAMP_COL in df.columns:
            if start_us is not None:
                df = df.filter(pl.col(TIMESTAMP_COL) >= start_us)
            if end_us is not None:
                df = df.filter(pl.col(TIMESTAMP_COL) <= end_us)
        return df

    def load_or_none(self, flight_id: str, message_type: str) -> "pl.DataFrame | None":
        df = self.load(flight_id, message_type)
        return df if (df is not None and not df.is_empty()) else None

    def load_many(
        self,
        flight_id: str,
        message_types: list[str],
        start_us: int | None = None,
        end_us: int | None = None,
    ) -> dict[str, pl.DataFrame]:
        return {
            mtype: self.load(flight_id, mtype, start_us=start_us, end_us=end_us)
            for mtype in message_types
        }

    def downsample(self, df: pl.DataFrame, max_points: int = 2000) -> pl.DataFrame:
        if df.is_empty() or len(df) <= max_points:
            return df
        stride = max(1, len(df) // max_points)
        return df[::stride]

    def load_for_plot(
        self,
        flight_id: str,
        channels: list[str],
        start_us: int | None = None,
        end_us: int | None = None,
        max_points: int = 2000,
    ) -> dict[str, dict]:
        by_type: dict[str, list[str]] = {}
        for ch in channels:
            if "." not in ch:
                continue
            mtype, field = ch.split(".", 1)
            by_type.setdefault(mtype, []).append(field)

        result = {}
        for mtype, fields in by_type.items():
            cols = [TIMESTAMP_COL] + fields
            df = self.load(flight_id, mtype, columns=cols, start_us=start_us, end_us=end_us)
            if df.is_empty():
                continue
            df = self.downsample(df, max_points)
            ts = df[TIMESTAMP_COL].to_list()
            for field in fields:
                if field in df.columns:
                    result[f"{mtype}.{field}"] = {
                        "timestamps": ts,
                        "values": df[field].to_list(),
                    }
        return result

    def write_derived(self, flight_id: str, name: str, data: dict | list) -> str:
        """Write derived analysis results as JSON. Returns the storage path/URI."""
        path = self._path(flight_id, "derived", f"{name}.json")
        self._backend.write_json(path, data)
        return path

    def read_derived(self, flight_id: str, name: str) -> dict | list | None:
        path = self._path(flight_id, "derived", f"{name}.json")
        return self._backend.read_json(path)

    def get_time_range(self, flight_id: str, message_type: str = "ATT") -> tuple[int, int]:
        df = self.load(flight_id, message_type, columns=[TIMESTAMP_COL])
        if df.is_empty():
            return 0, 0
        return int(df[TIMESTAMP_COL].min()), int(df[TIMESTAMP_COL].max())

    def upload_parsed(self, local_parquet_dir: Path, flight_id: str) -> dict[str, str]:
        """Upload all .parquet files from a local directory to flight storage."""
        uploaded = {}
        for p in local_parquet_dir.glob("*.parquet"):
            dest = self._path(flight_id, "parsed", p.name)
            self._backend.upload(p, dest)
            uploaded[p.stem] = dest
        return uploaded

    def download_raw(self, src_uri: str, local_path: Path) -> None:
        """Download a raw log file from storage to a local path."""
        self._backend.download(src_uri, local_path)


# ── DuckDB query engine (local storage only) ──────────────────────────────────

class DuckDBQueryEngine:
    """
    DuckDB-based SQL engine over Parquet files.
    Works with local storage. GCS paths require the DuckDB httpfs extension.
    """

    def __init__(self, store: ParquetStore | None = None):
        self.store = store or ParquetStore()
        self._conn: duckdb.DuckDBPyConnection | None = None

    def _get_conn(self) -> duckdb.DuckDBPyConnection:
        if self._conn is None:
            self._conn = duckdb.connect(":memory:")
        return self._conn

    def register_flight(self, flight_id: str) -> list[str]:
        """Register all Parquet files for a flight as DuckDB views."""
        conn = self._get_conn()
        parquets = self.store._backend.list_parquets(self.store.parsed_dir(flight_id))
        registered = []
        for path in parquets:
            view_name = path.rsplit("/", 1)[-1].removesuffix(".parquet")
            conn.execute(f"""
                CREATE OR REPLACE VIEW {view_name}
                AS SELECT * FROM read_parquet('{path}')
            """)
            registered.append(view_name)
        return registered

    def query(self, flight_id: str, sql: str) -> pl.DataFrame:
        self.register_flight(flight_id)
        return self._get_conn().execute(sql).pl()

    def get_aligned_series(
        self,
        flight_id: str,
        primary_type: str,
        secondary_types: list[str],
        fields: dict[str, list[str]],
        time_window_us: int = 100_000,
        start_us: int | None = None,
        end_us: int | None = None,
    ) -> pl.DataFrame:
        self.register_flight(flight_id)
        conn = self._get_conn()

        time_filter = ""
        if start_us is not None:
            time_filter += f" AND p.{TIMESTAMP_COL} >= {start_us}"
        if end_us is not None:
            time_filter += f" AND p.{TIMESTAMP_COL} <= {end_us}"

        primary_fields = ", ".join(
            [f"p.{TIMESTAMP_COL}"] + [f"p.{f} AS {primary_type}_{f}" for f in fields.get(primary_type, [])]
        )

        joins = []
        for sec_type in secondary_types:
            sec_fields = fields.get(sec_type, [])
            if not sec_fields:
                continue
            sec_select = ", ".join([f"s{sec_type}.{f} AS {sec_type}_{f}" for f in sec_fields])
            if sec_select:
                primary_fields += f", {sec_select}"
            joins.append(f"""
                LEFT JOIN {sec_type} s{sec_type}
                  ON ABS(p.{TIMESTAMP_COL} - s{sec_type}.{TIMESTAMP_COL}) <= {time_window_us}
                  AND s{sec_type}.{TIMESTAMP_COL} = (
                      SELECT {TIMESTAMP_COL} FROM {sec_type}
                      WHERE ABS({TIMESTAMP_COL} - p.{TIMESTAMP_COL}) <= {time_window_us}
                      ORDER BY ABS({TIMESTAMP_COL} - p.{TIMESTAMP_COL}) LIMIT 1
                  )
            """)

        sql = f"""
            SELECT {primary_fields}
            FROM {primary_type} p
            {' '.join(joins)}
            WHERE 1=1 {time_filter}
            ORDER BY p.{TIMESTAMP_COL}
        """
        return conn.execute(sql).pl()
