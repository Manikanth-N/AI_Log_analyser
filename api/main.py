"""FastAPI application — main entry point."""

import hashlib
import json
import uuid
from pathlib import Path

import aiofiles
import redis.asyncio as aioredis
import structlog
from fastapi import FastAPI, File, HTTPException, UploadFile, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from sse_starlette.sse import EventSourceResponse

from config.settings import settings
from storage.metadata_db import MetadataDB, create_tables
from storage.parquet_store import ParquetStore

log = structlog.get_logger(__name__)

app = FastAPI(
    title="Forensic Flight AI",
    version="1.0.0",
    description="Local autonomous UAV flight log forensic investigator",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

db = MetadataDB()
store = ParquetStore()


# ─────────────────────────────────────────────────────────────
# STARTUP
# ─────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    create_tables()
    settings.storage_root.mkdir(parents=True, exist_ok=True)
    settings.raw_storage.mkdir(parents=True, exist_ok=True)
    settings.flights_storage.mkdir(parents=True, exist_ok=True)
    log.info("api_startup", storage_root=str(settings.storage_root))


# ─────────────────────────────────────────────────────────────
# FLIGHTS
# ─────────────────────────────────────────────────────────────

@app.post("/api/v1/flights/upload")
async def upload_flight(file: UploadFile = File(...)):
    """
    Upload a UAV log file. Triggers background parsing.
    Supports .BIN, .ULOG, .TLOG, .CSV, .JSON up to 6GB.
    """
    if file.size and file.size > settings.max_upload_size_bytes:
        raise HTTPException(413, "File too large")

    flight_id = str(uuid.uuid4())
    upload_dir = settings.raw_storage / flight_id
    upload_dir.mkdir(parents=True, exist_ok=True)
    dest_path = upload_dir / (file.filename or "log.bin")

    # Stream write to disk (handles large files)
    sha256 = hashlib.sha256()
    file_size = 0

    async with aiofiles.open(dest_path, "wb") as f:
        while chunk := await file.read(1024 * 1024):  # 1MB chunks
            await f.write(chunk)
            sha256.update(chunk)
            file_size += len(chunk)

    file_hash = sha256.hexdigest()

    # Check for duplicate
    existing = db.get_flight_by_hash(file_hash) if hasattr(db, 'get_flight_by_hash') else None
    if existing:
        dest_path.unlink()
        upload_dir.rmdir()
        return JSONResponse({"flight_id": str(existing.id), "status": "existing", "duplicate": True})

    # Create flight record
    flight = db.create_flight(
        id=uuid.UUID(flight_id),
        sha256=file_hash,
        filename=file.filename or "log.bin",
        file_size=file_size,
        raw_path=str(dest_path),
        status="uploaded",
    )

    # Trigger background parse task
    from api.workers.tasks import parse_log_task
    parse_log_task.apply_async(
        args=[flight_id, str(dest_path)],
        queue="parse",
    )

    log.info("upload_complete", flight_id=flight_id, size=file_size)
    return {"flight_id": flight_id, "status": "parsing", "filename": file.filename}


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
    # Normalise to {phases: [...]} regardless of whether stored as list or dict
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
    channels: str,          # comma-separated "ATT.roll_deg,GPS.hdop"
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
    # Convert {channel: {timestamps, values}} → [{channel, timestamps_us, values}]
    return [
        {"channel": ch, "timestamps_us": v["timestamps"], "values": v["values"]}
        for ch, v in raw.items()
    ]


# ─────────────────────────────────────────────────────────────
# INVESTIGATIONS
# ─────────────────────────────────────────────────────────────

@app.post("/api/v1/investigations")
def start_investigation(body: dict):
    flight_id = body.get("flight_id")
    query = body.get("query", "Perform complete forensic investigation of this flight log")

    if not flight_id:
        raise HTTPException(400, "flight_id required")

    flight = db.get_flight(flight_id)
    if not flight:
        raise HTTPException(404, "Flight not found")
    if flight.status != "ready":
        raise HTTPException(409, f"Flight is not ready (status={flight.status})")

    investigation = db.create_investigation(flight_id=flight_id, query=query)
    inv_id = str(investigation.id)

    # Trigger investigation task
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
    store = ParquetStore()

    # Load context
    timeline = store.read_derived(flight_id, "timeline") or {}
    report = store.read_derived(flight_id, f"report_{investigation_id}") or {}

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

@app.get("/api/v1/health")
def health():
    from llm.client import get_llm_client
    llm_ok = get_llm_client().check_health()
    return {
        "status": "ok",
        "ollama": "ok" if llm_ok else "degraded",
        "version": "1.0.0",
    }
