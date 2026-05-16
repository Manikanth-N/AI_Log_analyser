from enum import Enum
from pathlib import Path
from urllib.parse import quote as _url_quote

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class InferenceMode(str, Enum):
    OLLAMA  = "ollama"   # all calls → local Ollama (default, backward-compat)
    API     = "api"      # all calls → managed cloud APIs (Anthropic + OpenAI)
    HYBRID  = "hybrid"   # domain → domain_provider, critical → critical_provider


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ── Storage ───────────────────────────────────────────────────────────────
    storage_root: Path = Path.home() / ".forensic_flight"   # local dev only
    max_upload_size_bytes: int = 6 * 1024 * 1024 * 1024     # 6 GB
    gcs_data_bucket: str = ""   # set to GCS bucket name to enable GCS mode

    # ── Database (prefer DATABASE_URL; fall back to component fields) ─────────
    database_url: str = ""
    database_url_sync: str = ""
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "forensic_flight"
    db_user: str = "forensic_flight"
    db_password: str = "forensic"

    # ── Redis (prefer redis_url; fall back to component fields) ───────────────
    redis_url: str = ""
    redis_result_url: str = ""
    redis_pubsub_url: str = ""
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_password: str = ""

    # Celery broker/backend — workers set these explicitly; API composes from redis_url.
    celery_broker_url: str = ""
    celery_result_url: str = ""

    # ── Qdrant ────────────────────────────────────────────────────────────────
    qdrant_url: str = "http://localhost:6333"
    qdrant_collection_baselines: str = "baseline_flights"
    qdrant_collection_evidence: str = "investigation_evidence"

    # ── Ollama (local / backward-compat) ──────────────────────────────────────
    ollama_url: str = "http://localhost:11434"
    ollama_primary_model: str = "qwen3:32b-q4_K_M"
    ollama_fast_model: str = "qwen3:8b-q4_K_M"
    ollama_embedding_model: str = "nomic-embed-text:v1.5"
    ollama_timeout_seconds: int = 1200  # 20 min — local CPU inference is slow
    ollama_context_length: int = 32768

    # ── Inference tier routing ────────────────────────────────────────────────
    #
    # inference_mode controls which providers are active:
    #   "ollama"  — all calls route to local Ollama (default, backward-compat)
    #   "api"     — managed APIs only (Anthropic + OpenAI)
    #   "hybrid"  — domain agents → domain_provider, critical path → critical_provider
    #
    # To switch to cloud inference, set in .env or environment:
    #   INFERENCE_MODE=api
    #   CRITICAL_PROVIDER=anthropic
    #   CRITICAL_MODEL=claude-sonnet-4-6
    #   DOMAIN_PROVIDER=openai
    #   DOMAIN_MODEL=gpt-4o-mini-2024-07-18
    #   ANTHROPIC_API_KEY=sk-ant-...
    #   OPENAI_API_KEY=sk-...
    #
    inference_mode: InferenceMode = InferenceMode.OLLAMA

    # Domain agents tier (EKF, GPS, Power, Vibration, Mission, etc.)
    domain_provider: str = "ollama"
    domain_model: str = "qwen3:8b-q4_K_M"

    # Critical-path tier (CrashInvestigator, ReportWriter)
    critical_provider: str = "ollama"
    critical_model: str = "qwen3:32b-q4_K_M"

    # Fallback provider
    fallback_provider: str = "ollama"
    fallback_model: str = "qwen3:32b-q4_K_M"

    # ── API keys (never hard-code; always via env / Secrets Manager) ──────────
    anthropic_api_key: str = ""
    openai_api_key: str = ""

    # ── Self-hosted vLLM endpoint ─────────────────────────────────────────────
    vllm_endpoint: str = ""
    vllm_model: str = ""
    vllm_concurrency_limit: int = 4

    # ── Embedding provider ────────────────────────────────────────────────────
    embedding_provider: str = "ollama"
    openai_embedding_model: str = "text-embedding-3-small"

    # ── Request settings ──────────────────────────────────────────────────────
    inference_request_timeout_seconds: float = 120.0

    # ── Parsing ───────────────────────────────────────────────────────────────
    parquet_chunk_rows: int = 500_000
    parquet_compression: str = "snappy"

    # Investigation
    max_investigation_iterations: int = 5
    hypothesis_confidence_threshold: float = 0.70
    agent_timeout_seconds: int = 600

    # API
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    cors_origins: list[str] = ["http://localhost:3000", "http://127.0.0.1:3000"]
    enable_docs: bool = True

    # ── API key auth + rate limiting ──────────────────────────────────────────
    api_keys: str = ""
    rate_limit_investigations_per_day: int = 50
    rate_limit_uploads_per_hour: int = 100

    @model_validator(mode="after")
    def compose_connection_urls(self) -> "Settings":
        # Database: compose from components when DATABASE_URL not explicitly set.
        # Passwords must be percent-encoded — Secret Manager values often contain
        # '/', '@', '#', '%' which break urlparse if embedded literally.
        if not self.database_url:
            user = _url_quote(self.db_user, safe="")
            password = _url_quote(self.db_password, safe="")
            base = f"postgresql://{user}:{password}@{self.db_host}:{self.db_port}/{self.db_name}"
            self.database_url = base.replace("postgresql://", "postgresql+asyncpg://")
            self.database_url_sync = base.replace("postgresql://", "postgresql+psycopg2://")
        else:
            # Normalize driver prefix on pre-set DATABASE_URL and derive missing sync URL.
            url = self.database_url
            if not url.startswith("postgresql+"):
                self.database_url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
            if not self.database_url_sync:
                self.database_url_sync = self.database_url.replace(
                    "postgresql+asyncpg://", "postgresql+psycopg2://", 1
                )

        # Redis: compose from components when redis_url not explicitly set.
        if not self.redis_url:
            auth = f":{_url_quote(self.redis_password, safe='')}@" if self.redis_password else ""
            base = f"redis://{auth}{self.redis_host}:{self.redis_port}"
            self.redis_url = f"{base}/0"
            self.redis_result_url = f"{base}/1"
            self.redis_pubsub_url = f"{base}/2"

        return self

    @property
    def raw_storage(self) -> Path:
        return self.storage_root / "raw"

    @property
    def flights_storage(self) -> Path:
        return self.storage_root / "flights"

    @property
    def baselines_storage(self) -> Path:
        return self.storage_root / "baselines"

    @property
    def using_managed_api(self) -> bool:
        return self.inference_mode in (InferenceMode.API, InferenceMode.HYBRID) or (
            self.critical_provider in ("anthropic", "openai")
            or self.domain_provider in ("anthropic", "openai")
        )


settings = Settings()
