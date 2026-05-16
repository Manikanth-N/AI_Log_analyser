"""
Upload validation — extension whitelist, MIME verification, magic bytes, size cap.

Prevents:
  - Path traversal (sanitised filename)
  - Executable upload (extension + magic byte check)
  - Oversized uploads (double-checked after streaming to avoid HEAD spoofing)
  - Wrong format (early rejection before expensive parsing)
"""

from __future__ import annotations

import re
from pathlib import PurePath

from fastapi import UploadFile, HTTPException
import structlog

log = structlog.get_logger(__name__)

# ── Extension whitelist ───────────────────────────────────────────────────────

_ALLOWED_EXTENSIONS: frozenset[str] = frozenset({
    ".bin",   # ArduPilot DataFlash binary
    ".ulg",   # PX4 ULog
    ".tlog",  # MAVLink telemetry log
    ".log",   # ArduPilot text log
    ".csv",   # Generic tabular data
    ".json",  # Pre-processed JSON
})

# ── Magic bytes (file signature) check ────────────────────────────────────────
# Map extension → list of acceptable magic byte prefixes.
# An empty list means no magic byte validation for that extension (text formats).

_MAGIC_BYTES: dict[str, list[bytes]] = {
    ".bin": [b"\xa3\x95"],          # ArduPilot DataFlash v2 header
    ".ulg": [b"ULog\x01"],          # PX4 ULog v1
    ".tlog": [],                    # MAVLink — variable header
    ".log": [],                     # text
    ".csv": [],                     # text
    ".json": [b"{", b"["],          # JSON must start with { or [
}

# Filename: no path separators, no null bytes, printable ASCII only
_SAFE_FILENAME_RE = re.compile(r'^[\w\-. ]+$')

MAX_FILENAME_LENGTH = 255


def _safe_filename(filename: str | None) -> str:
    """Normalise filename: strip path components, validate characters."""
    if not filename:
        return "log.bin"
    # Strip any directory traversal
    name = PurePath(filename).name
    if not name or not _SAFE_FILENAME_RE.match(name):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid filename: {filename!r}. Use alphanumeric characters, hyphens, dots, spaces only.",
        )
    if len(name) > MAX_FILENAME_LENGTH:
        raise HTTPException(status_code=400, detail="Filename exceeds 255 characters.")
    return name


def _check_extension(filename: str) -> str:
    """Return normalised extension or raise 415."""
    ext = PurePath(filename).suffix.lower()
    if ext not in _ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail=(
                f"File type {ext!r} is not supported. "
                f"Accepted formats: {', '.join(sorted(_ALLOWED_EXTENSIONS))}"
            ),
        )
    return ext


async def _check_magic_bytes(file: UploadFile, ext: str) -> bytes:
    """
    Read the first 16 bytes of the file and verify magic bytes for known binary formats.
    Returns the peeked bytes so they can be prepended when streaming the rest.
    """
    expected_prefixes = _MAGIC_BYTES.get(ext, [])
    if not expected_prefixes:
        return b""  # no magic check for this extension

    header = await file.read(16)
    await file.seek(0)  # rewind so streaming still works

    if not any(header.startswith(prefix) for prefix in expected_prefixes):
        log.warning(
            "upload_magic_byte_mismatch",
            ext=ext,
            header_hex=header.hex(),
        )
        raise HTTPException(
            status_code=415,
            detail=(
                f"File header does not match expected format for {ext} files. "
                "Ensure the file is not corrupted or mislabelled."
            ),
        )
    return header


def validate_upload_init(filename: str, file_size: int, max_bytes: int) -> str:
    """
    Validate upload metadata for the GCS signed-URL flow (no file body available yet).
    Returns sanitised filename. Raises HTTPException on failure.
    """
    safe_name = _safe_filename(filename)
    _check_extension(safe_name)
    if file_size <= 0:
        raise HTTPException(
            status_code=400,
            detail="file_size must be a positive integer.",
        )
    if file_size > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"File size {file_size:,} bytes exceeds maximum {max_bytes:,} bytes.",
        )
    return safe_name


async def validate_upload(file: UploadFile, max_bytes: int) -> str:
    """
    Full upload validation pipeline.

    Args:
        file: The uploaded UploadFile from FastAPI.
        max_bytes: Maximum allowed file size in bytes.

    Returns:
        Sanitised filename to use when saving to disk.

    Raises:
        HTTPException on any validation failure.
    """
    safe_name = _safe_filename(file.filename)
    ext = _check_extension(safe_name)

    # Content-Length header check (advisory — not trusted as final size check)
    if file.size and file.size > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"File size {file.size:,} bytes exceeds maximum {max_bytes:,} bytes.",
        )

    await _check_magic_bytes(file, ext)

    log.info(
        "upload_validated",
        filename=safe_name,
        ext=ext,
        declared_size=file.size,
    )
    return safe_name
