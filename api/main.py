"""FastAPI application — main entry point."""

import hashlib
import json
import time
import uuid
from pathlib import Path

import aiofiles
import redis.asyncio as aioredis
import structlog
from fastapi import FastAPI, File, HTTPException, UploadFile, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from api.middleware.auth import require_api_key, check_investigation_quota, check_upload_quota
from api.middleware.upload_validation import validate_upload, validate_upload_init
from config.settings import settings
from storage.metadata_db import MetadataDB
from storage.parquet_store import ParquetStore

log = structlog.get_logger(__name__)

app = FastAPI(
    title="Forensic Flight AI",
    version="1.0.0",
    description="Autonomous UAV flight log forensic investigator",
    docs_url="/docs" if getattr(settings, "enable_docs", True) else None,
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["X-API-Key", "Content-Type"],
)

db = MetadataDB()
store = ParquetStore()


# ─────────────────────────────────────────────────────────────
# STARTUP
# ─────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    # Schema is managed by Alembic — run `alembic upgrade head` before deploying.
    # No create_all() here; Alembic is the sole schema authority.
    if not settings.gcs_data_bucket:
        # Local dev: ensure directories exist
        settings.storage_root.mkdir(parents=True, exist_ok=True)
        settings.raw_storage.mkdir(parents=True, exist_ok=True)
        settings.flights_storage.mkdir(parents=True, exist_ok=True)
    log.info("api_startup", gcs_mode=bool(settings.gcs_data_bucket))


# ─────────────────────────────────────────────────────────────
# CAPABILITIES
# ─────────────────────────────────────────────────────────────

@app.get("/api/v1/capabilities")
def get_capabilities():
    """Returns feature flags. Frontend uses upload_mode to select upload flow."""
    return {
        "upload_mode": "gcs" if settings.gcs_data_bucket else "direct",
        "max_upload_size_bytes": settings.max_upload_size_bytes,
    }


# ─────────────────────────────────────────────────────────────
# FLIGHTS — upload (GCS signed-URL flow)
# ─────────────────────────────────────────────────────────────

@app.post("/api/v1/flights/upload/init")
async def init_upload(
    body: dict,
    api_key: str = Depends(require_api_key),
):
    """
    Step 1 of GCS upload flow. Returns a resumable upload URL + flight_id.
    The client uploads the file directly to upload_url (PUT), then calls /process.
    """
    if not settings.gcs_data_bucket:
        raise HTTPException(501, "GCS upload not configured — use /upload instead")

    filename = body.get("filename", "")
    content_type = body.get("content_type", "application/octet-stream")
    file_size = int(body.get("file_size", 0))

    check_upload_quota(api_key)
    safe_name = validate_upload_init(filename, file_size, settings.max_upload_size_bytes)

    flight_id = str(uuid.uuid4())
    gcs_path = f"raw/{flight_id}/{safe_name}"
    gcs_uri = f"gs://{settings.gcs_data_bucket}/{gcs_path}"

    # Create GCS resumable upload session
    from google.cloud import storage as gcs_lib
    gcs_client = gcs_lib.Client()
    blob = gcs_client.bucket(settings.gcs_data_bucket).blob(gcs_path)
    upload_url = blob.create_resumable_upload_session(
        content_type=content_type,
        size=file_size,
    )

    db.create_flight(
        id=uuid.UUID(flight_id),
        filename=safe_name,
        file_size=file_size,
        gcs_raw_uri=gcs_uri,
        status="pending_upload",
    )

    log.info("upload_init", flight_id=flight_id, filename=safe_name, size=file_size)
    return {"upload_url": str(upload_url), "flight_id": flight_id, "gcs_uri": gcs_uri}


@app.post("/api/v1/flights/{flight_id}/process")
async def process_flight(
    flight_id: str,
    api_key: str = Depends(require_api_key),
):
    """
    Step 2 of GCS upload flow. Triggers parsing after client has uploaded to GCS.
    """
    flight = db.get_flight(flight_id)
    if not flight:
        raise HTTPException(404, "Flight not found")
    if flight.status != "pending_upload":
        raise HTTPException(409, f"Unexpected status: {flight.status}")

    # Verify the blob actually landed in GCS before burning a parse queue slot.
    # Catches silent client-side PUT failures (network drops, GCS rejections).
    from google.cloud import storage as gcs_lib
    gcs_uri = flight.gcs_raw_uri or ""
    if gcs_uri.startswith("gs://"):
        bucket_name, _, blob_name = gcs_uri[5:].partition("/")
        gcs_client = gcs_lib.Client()
        blob = gcs_client.bucket(bucket_name).blob(blob_name)
        if not blob.exists():
            raise HTTPException(
                422,
                "File not found in storage. Upload may have failed — please retry the upload.",
            )

    db.update_flight_status(flight_id, "uploaded")

    from api.workers.tasks import parse_log_task
    parse_log_task.apply_async(
        args=[flight_id, gcs_uri],
        queue="parse",
    )

    log.info("parse_queued", flight_id=flight_id, filename=flight.filename)
    return {"flight_id": flight_id, "status": "parsing", "filename": flight.filename}


# ─────────────────────────────────────────────────────────────
# FLIGHTS — upload (direct flow for local dev)
# ─────────────────────────────────────────────────────────────

@app.post("/api/v1/flights/upload")
async def upload_flight(
    file: UploadFile = File(...),
    api_key: str = Depends(require_api_key),
):
    """
    Direct multipart upload for local development (no GCS_DATA_BUCKET set).
    Supports .BIN, .ULOG, .TLOG, .CSV, .JSON up to 6GB.
    """
    if settings.gcs_data_bucket:
        raise HTTPException(
            400,
            "GCS mode active — use POST /api/v1/flights/upload/init instead",
        )

    check_upload_quota(api_key)
    safe_name = await validate_upload(file, max_bytes=settings.max_upload_size_bytes)

    flight_id = str(uuid.uuid4())
    upload_dir = settings.raw_storage / flight_id
    upload_dir.mkdir(parents=True, exist_ok=True)
    dest_path = upload_dir / safe_name

    sha256 = hashlib.sha256()
    file_size = 0

    async with aiofiles.open(dest_path, "wb") as f:
        while chunk := await file.read(1024 * 1024):
            await f.write(chunk)
            sha256.update(chunk)
            file_size += len(chunk)

    file_hash = sha256.hexdigest()

    existing = db.get_flight_by_hash(file_hash) if hasattr(db, "get_flight_by_hash") else None
    if existing:
        dest_path.unlink()
        upload_dir.rmdir()
        return JSONResponse({"flight_id": str(existing.id), "status": "existing", "duplicate": True})

    db.create_flight(
        id=uuid.UUID(flight_id),
        sha256=file_hash,
        filename=safe_name,
        file_size=file_size,
        raw_path=str(dest_path),
        status="uploaded",
    )

    from api.workers.tasks import parse_log_task
    parse_log_task.apply_async(
        args=[flight_id, str(dest_path)],
        queue="parse",
    )

    log.info("upload_complete", flight_id=flight_id, size=file_size, filename=safe_name)
    return {"flight_id": flight_id, "status": "parsing", "filename": safe_name}


# ─────────────────────────────────────────────────────────────
# FLIGHTS — read
# ─────────────────────────────────────────────────────────────

@app.get("/api/v1/flights/{flight_id}")
def get_flight(flight_id: str):
    flight = db.get_flight(flight_id)
    if not flight:
        raise HTTPException(404, "Flight not found")
    return {
        "id": str(flight.id),
        "filename": flight.filename,
        "status": flight.status,
        "format": flight.format,
        "autopilot": flight.autopilot,
        "fw_version": flight.fw_version,
        "duration_s": flight.duration_s,
        "message_types": flight.message_types,
        "missing_critical": flight.missing_critical,
        "uploaded_at": flight.uploaded_at.isoformat() if flight.uploaded_at else None,
    }


@app.get("/api/v1/flights")
def list_flights(limit: int = 50, offset: int = 0):
    flights = db.list_flights(limit=limit, offset=offset)
    return [
        {"id": str(f.id), "filename": f.filename, "status": f.status,
         "duration_s": f.duration_s, "uploaded_at": f.uploaded_at.isoformat() if f.uploaded_at else None}
        for f in flights
    ]


@app.get("/api/v1/flights/{flight_id}/timeline")
def get_timeline(flight_id: str):
    timeline = store.read_derived(flight_id, "timeline")
    if timeline is None:
        raise HTTPException(404, "Timeline not yet computed")
    return timeline


@app.get("/api/v1/flights/{flight_id}/phases")
def get_phases(flight_id: str):
    phases = store.read_derived(flight_id, "phases")
    if phases is None:
        raise HTTPException(404, "Phases not yet computed")
    if isinstance(phases, list):
        phases = {"phases": phases}
    return phases


@app.get("/api/v1/flights/{flight_id}/anomalies")
def get_anomalies(
    flight_id: str,
    severity: str | None = None,
    category: str | None = None,
):
    severities = severity.split(",") if severity else None
    anomalies = db.get_anomalies(flight_id, severity=severities, category=category)
    return [
        {
            "id": str(a.id),
            "timestamp_us": a.timestamp_us,
            "severity": a.severity,
            "category": a.category,
            "rule_name": a.rule_name,
            "description": a.description,
            "raw_values": a.raw_values,
        }
        for a in anomalies
    ]


@app.get("/api/v1/telemetry/{flight_id}/series")
def get_telemetry_series(
    flight_id: str,
    channels: str,
    start_us: int | None = None,
    end_us: int | None = None,
    max_points: int = 2000,
):
    channel_list = [c.strip() for c in channels.split(",")]
    raw = store.load_for_plot(
        flight_id=flight_id,
        channels=channel_list,
        start_us=start_us,
        end_us=end_us,
        max_points=max_points,
    )
    return [
        {"channel": ch, "timestamps_us": v["timestamps"], "values": v["values"]}
        for ch, v in raw.items()
    ]


# ─────────────────────────────────────────────────────────────
# INVESTIGATIONS
# ─────────────────────────────────────────────────────────────

@app.post("/api/v1/investigations")
def start_investigation(body: dict, api_key: str = Depends(require_api_key)):
    flight_id = body.get("flight_id")
    query = body.get("query", "Perform complete forensic investigation of this flight log")

    if not flight_id:
        raise HTTPException(400, "flight_id required")

    check_investigation_quota(api_key)

    flight = db.get_flight(flight_id)
    if not flight:
        raise HTTPException(404, "Flight not found")
    if flight.status != "ready":
        raise HTTPException(409, f"Flight is not ready (status={flight.status})")

    investigation = db.create_investigation(flight_id=flight_id, query=query)
    inv_id = str(investigation.id)

    from api.workers.tasks import run_investigation_task
    run_investigation_task.apply_async(
        args=[inv_id, flight_id, query],
        queue="investigate",
    )

    return {"investigation_id": inv_id, "status": "queued", "query": query}


@app.get("/api/v1/investigations/{investigation_id}")
def get_investigation(investigation_id: str):
    inv = db.get_investigation(investigation_id)
    if not inv:
        raise HTTPException(404, "Investigation not found")
    return {
        "id": str(inv.id),
        "flight_id": str(inv.flight_id),
        "status": inv.status,
        "query": inv.query,
        "root_cause": inv.root_cause,
        "contributing_factors": inv.contributing_factors,
        "recommendations": inv.recommendations,
        "confidence": inv.confidence,
        "report_path": inv.report_path,
        "agent_findings": inv.agent_findings,
    }


@app.get("/api/v1/investigations/{investigation_id}/stream")
async def stream_investigation(investigation_id: str):
    """SSE stream of agent activity for live UI updates."""
    redis_client = aioredis.from_url(settings.redis_pubsub_url)

    async def event_generator():
        pubsub = redis_client.pubsub()
        await pubsub.subscribe(f"inv:{investigation_id}")

        yield {"event": "connected", "data": json.dumps({"investigation_id": investigation_id})}

        try:
            async for message in pubsub.listen():
                if message["type"] == "message":
                    data = message["data"]
                    if isinstance(data, bytes):
                        data = data.decode()
                    try:
                        parsed = json.loads(data)
                    except Exception:
                        parsed = {"message": data}

                    yield {"event": parsed.get("type", "update"), "data": json.dumps(parsed)}

                    if parsed.get("type") in ("complete", "error"):
                        break
        finally:
            await pubsub.unsubscribe(f"inv:{investigation_id}")
            await redis_client.aclose()

    return EventSourceResponse(event_generator())


@app.post("/api/v1/investigations/{investigation_id}/query")
async def follow_up_query(investigation_id: str, body: dict):
    """Answer a follow-up question about an investigation."""
    question = body.get("question", "")
    if not question:
        raise HTTPException(400, "question required")

    inv = db.get_investigation(investigation_id)
    if not inv:
        raise HTTPException(404, "Investigation not found")
    if inv.status != "complete":
        raise HTTPException(409, "Investigation not yet complete")

    from llm.client import get_llm_client
    from llm.prompts.system_prompts import INVESTIGATOR_BASE_PROMPT
    from storage.parquet_store import ParquetStore

    flight_id = str(inv.flight_id)
    _store = ParquetStore()

    timeline = _store.read_derived(flight_id, "timeline") or {}
    report = _store.read_derived(flight_id, f"report_{investigation_id}") or {}

    context = f"""
Investigation findings:
- Root cause: {inv.root_cause or 'See report'}
- Contributing factors: {inv.contributing_factors}
- Confidence: {inv.confidence}

Report executive summary: {report.get('executive_summary', 'Not available')}

Agent findings summary: {json.dumps({k: v.get('summary', '') for k, v in (inv.agent_findings or {}).items()}, indent=2)}
"""

    llm = get_llm_client()
    answer = llm.complete(
        messages=[
            {"role": "user", "content": f"Context:\n{context}\n\nQuestion: {question}"}
        ],
        system=INVESTIGATOR_BASE_PROMPT + "\n\nYou are in INTERACTIVE INVESTIGATION MODE. Answer the follow-up question based only on the investigation evidence provided. Do not invent new facts.",
        max_tokens=1024,
    )

    return {"question": question, "answer": answer, "investigation_id": investigation_id}


@app.get("/api/v1/reports/{investigation_id}")
def get_report(investigation_id: str, format: str = "json"):
    inv = db.get_investigation(investigation_id)
    if not inv:
        raise HTTPException(404, "Investigation not found")

    flight_id = str(inv.flight_id)
    report = store.read_derived(flight_id, f"report_{investigation_id}")
    if not report:
        raise HTTPException(404, "Report not yet generated")

    return report


# ─────────────────────────────────────────────────────────────
# HEALTH
# ─────────────────────────────────────────────────────────────

@app.get("/health")
def health_simple():
    return {"status": "ok"}


@app.get("/api/v1/health")
def health_detailed():
    import redis as _redis
    checks: dict[str, str] = {}

    try:
        db.get_flight("00000000-0000-0000-0000-000000000000")
        checks["database"] = "ok"
    except Exception as e:
        err = str(e)
        if any(kw in err.lower() for kw in ("not found", "no result", "none")):
            checks["database"] = "ok"
        else:
            checks["database"] = f"error: {err}"

    try:
        r = _redis.Redis.from_url(settings.redis_url, socket_connect_timeout=2)
        r.ping()
        checks["redis"] = "ok"
    except Exception as e:
        checks["redis"] = f"error: {e}"

    try:
        from llm.client import get_llm_client
        llm_ok = get_llm_client().check_health()
        checks["inference"] = "ok" if llm_ok else "degraded"
    except Exception as e:
        checks["inference"] = f"error: {e}"

    overall = "ok" if all(v == "ok" for v in checks.values()) else "degraded"
    return {
        "status": overall,
        "checks": checks,
        "inference_mode": settings.inference_mode,
        "version": "1.0.0",
    }
