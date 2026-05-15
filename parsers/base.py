"""Abstract base class for all log parsers."""

import hashlib
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable, Iterator

import polars as pl

from .schema import LogFormat, LogMetadata


class ParseError(Exception):
    pass


class AbstractLogParser(ABC):
    FORMAT: LogFormat

    def __init__(self, file_path: Path):
        self.file_path = file_path
        self._validate_file()

    def _validate_file(self):
        if not self.file_path.exists():
            raise FileNotFoundError(f"Log file not found: {self.file_path}")
        if self.file_path.stat().st_size == 0:
            raise ParseError("Log file is empty")

    @abstractmethod
    def detect(self) -> bool:
        """Return True if this parser can handle the file."""
        ...

    @abstractmethod
    def parse_metadata(self) -> LogMetadata:
        """Extract log metadata without full parse."""
        ...

    @abstractmethod
    def stream_messages(
        self,
        message_types: list[str] | None = None,
        progress_cb: Callable[[int, int], None] | None = None,
    ) -> Iterator[dict]:
        """
        Yield normalized message dicts one at a time.
        Each dict has: {'type': str, 'timestamp_us': int, **fields}
        """
        ...

    @abstractmethod
    def parse_to_dataframes(
        self,
        output_dir: Path,
        message_types: list[str] | None = None,
        progress_cb: Callable[[float], None] | None = None,
    ) -> dict[str, Path]:
        """
        Parse all messages, write per-type Parquet files to output_dir.
        Returns mapping: message_type -> parquet_path.
        Uses streaming to avoid OOM on large files.
        """
        ...

    @staticmethod
    def sha256(file_path: Path) -> str:
        h = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    @staticmethod
    def detect_format(file_path: Path) -> LogFormat:
        """Detect log format from magic bytes, not extension."""
        with open(file_path, "rb") as f:
            header = f.read(32)

        # ArduPilot .BIN: starts with 0xA3 0x95 0x80 0x80 (FMT message header)
        if len(header) >= 4 and header[0] == 0xA3 and header[1] == 0x95:
            return LogFormat.ARDUPILOT_BIN

        # PX4 .ULOG: "ULog\x01"
        if header[:5] == b"ULog\x01":
            return LogFormat.PX4_ULOG

        # MAVLink TLOG: starts with 0xFE or 0xFD (MAVLink v1/v2 magic)
        if header[0] in (0xFE, 0xFD):
            return LogFormat.MAVLINK_TLOG

        # CSV: text-based, check first line
        try:
            text_start = header.decode("utf-8", errors="replace")
            if "," in text_start and not text_start.startswith("{"):
                return LogFormat.CSV
        except Exception:
            pass

        # JSON
        if header.lstrip()[:1] in (b"{", b"["):
            return LogFormat.JSON

        # Fallback: try extension
        suffix = file_path.suffix.lower()
        return {
            ".bin": LogFormat.ARDUPILOT_BIN,
            ".ulog": LogFormat.PX4_ULOG,
            ".tlog": LogFormat.MAVLINK_TLOG,
            ".csv": LogFormat.CSV,
            ".json": LogFormat.JSON,
        }.get(suffix, LogFormat.ARDUPILOT_BIN)


def get_parser(file_path: Path) -> AbstractLogParser:
    """Factory: return the correct parser for a file."""
    from .ardupilot_bin import ArduPilotBinParser
    from .csv_json import CsvJsonParser
    from .mavlink_tlog import MavlinkTlogParser
    from .px4_ulog import PX4UlogParser

    fmt = AbstractLogParser.detect_format(file_path)
    parsers = {
        LogFormat.ARDUPILOT_BIN: ArduPilotBinParser,
        LogFormat.PX4_ULOG: PX4UlogParser,
        LogFormat.MAVLINK_TLOG: MavlinkTlogParser,
        LogFormat.CSV: CsvJsonParser,
        LogFormat.JSON: CsvJsonParser,
    }
    cls = parsers.get(fmt)
    if cls is None:
        raise ParseError(f"No parser for format: {fmt}")
    return cls(file_path)
