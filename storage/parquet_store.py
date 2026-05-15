"""
Parquet-based telemetry store.
Reads per-message-type Parquet files for a flight, with optional downsampling.
"""

from pathlib import Path

import duckdb
import polars as pl

from config.settings import settings
from parsers.schema import TIMESTAMP_COL


class ParquetStore:
    def __init__(self, storage_root: Path | None = None):
        self.root = storage_root or settings.flights_storage

    def flight_dir(self, flight_id: str) -> Path:
        return self.root / flight_id

    def parsed_dir(self, flight_id: str) -> Path:
        return self.flight_dir(flight_id) / "parsed"

    def derived_dir(self, flight_id: str) -> Path:
        return self.flight_dir(flight_id) / "derived"

    def reports_dir(self, flight_id: str) -> Path:
        return self.flight_dir(flight_id) / "reports"

    def available_types(self, flight_id: str) -> list[str]:
        d = self.parsed_dir(flight_id)
        if not d.exists():
            return []
        return [p.stem for p in d.glob("*.parquet")]

    def load(
        self,
        flight_id: str,
        message_type: str,
        columns: list[str] | None = None,
        start_us: int | None = None,
        end_us: int | None = None,
    ) -> pl.DataFrame:
        """Load a message type Parquet file, optionally filtered by time."""
        path = self.parsed_dir(flight_id) / f"{message_type}.parquet"
        if not path.exists():
            return pl.DataFrame()

        df = pl.read_parquet(path, columns=columns)

        if TIMESTAMP_COL in df.columns:
            if start_us is not None:
                df = df.filter(pl.col(TIMESTAMP_COL) >= start_us)
            if end_us is not None:
                df = df.filter(pl.col(TIMESTAMP_COL) <= end_us)

        return df

    def load_or_none(
        self,
        flight_id: str,
        message_type: str,
    ) -> "pl.DataFrame | None":
        """Like load() but returns None instead of empty DataFrame when file is missing."""
        import polars as pl
        df = self.load(flight_id, message_type)
        return df if (df is not None and not df.is_empty()) else None

    def load_many(
        self,
        flight_id: str,
        message_types: list[str],
        start_us: int | None = None,
        end_us: int | None = None,
    ) -> dict[str, pl.DataFrame]:
        """Load multiple message types at once."""
        return {
            mtype: self.load(flight_id, mtype, start_us=start_us, end_us=end_us)
            for mtype in message_types
        }

    def downsample(
        self,
        df: pl.DataFrame,
        max_points: int = 2000,
    ) -> pl.DataFrame:
        """Downsample by uniform stride for frontend plot data."""
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
        """
        Load specific channels across message types for frontend plotting.
        channels format: "MSG_TYPE.field_name" e.g. "ATT.roll_deg"
        Returns: {channel: {"timestamps": [...], "values": [...]}}
        """
        # Group channels by message type
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

    def write_derived(self, flight_id: str, name: str, data: dict | list) -> Path:
        """Write derived analysis results as JSON."""
        import json
        d = self.derived_dir(flight_id)
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{name}.json"
        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)
        return path

    def read_derived(self, flight_id: str, name: str) -> dict | list | None:
        import json
        path = self.derived_dir(flight_id) / f"{name}.json"
        if not path.exists():
            return None
        with open(path) as f:
            return json.load(f)

    def get_time_range(self, flight_id: str, message_type: str = "ATT") -> tuple[int, int]:
        """Get the time range of a flight from any message type."""
        df = self.load(flight_id, message_type, columns=[TIMESTAMP_COL])
        if df.is_empty():
            return 0, 0
        return int(df[TIMESTAMP_COL].min()), int(df[TIMESTAMP_COL].max())


class DuckDBQueryEngine:
    """
    DuckDB-based query engine over Parquet files.
    Enables complex SQL queries across multiple telemetry files.
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
        parsed_dir = self.store.parsed_dir(flight_id)
        registered = []

        for path in parsed_dir.glob("*.parquet"):
            view_name = f"{path.stem}"
            conn.execute(f"""
                CREATE OR REPLACE VIEW {view_name}
                AS SELECT * FROM read_parquet('{path}')
            """)
            registered.append(view_name)

        return registered

    def query(self, flight_id: str, sql: str) -> pl.DataFrame:
        """Execute SQL against a flight's Parquet files."""
        self.register_flight(flight_id)
        conn = self._get_conn()
        return conn.execute(sql).pl()

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
        """
        Time-align multiple message types to primary_type timestamps.
        Uses nearest-neighbor join within time_window_us tolerance.

        Example:
            primary_type = "GPS"
            secondary_types = ["NKF4", "BAT"]
            fields = {"GPS": ["hdop", "num_sats"], "NKF4": ["var_ratio_vel"]}
        """
        self.register_flight(flight_id)
        conn = self._get_conn()

        time_filter = ""
        params = []
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
