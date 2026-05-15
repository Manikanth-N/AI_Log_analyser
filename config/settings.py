from enum import Enum
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class InferenceMode(str, Enum):
    OLLAMA  = "ollama"   # all calls → local Ollama (default, backward-compat)
    API     = "api"      # all calls → managed cloud APIs (Anthropic + OpenAI)
    HYBRID  = "hybrid"   # domain → domain_provider, critical → critical_provider


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Storage
    storage_root: Path = Path.home() / ".forensic_flight"
    max_upload_size_bytes: int = 6 * 1024 * 1024 * 1024  # 6GB

    # Database
    database_url: str = "postgresql+asyncpg://forensic:forensic@localhost:5432/forensic_flight"
    database_url_sync: str = "postgresql+psycopg2://forensic:forensic@localhost:5432/forensic_flight"

    # Redis
    redis_url: str = "redis://localhost:6379/0"
    redis_result_url: str = "redis://localhost:6379/1"
    redis_pubsub_url: str = "redis://localhost:6379/2"

    # Qdrant
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
    # Fast, cheap, high-volume calls.
    domain_provider: str = "ollama"           # "openai" | "anthropic" | "vllm" | "ollama"
    domain_model: str = ollama_fast_model     # default matches ollama fast_model

    # Critical-path tier (CrashInvestigator, ReportWriter)
    # Best available reasoning quality — safety-critical output.
    critical_provider: str = "ollama"         # "anthropic" | "openai" | "vllm" | "ollama"
    critical_model: str = ollama_primary_model

    # Fallback provider (fires when primary provider throws unrecoverable error)
    fallback_provider: str = "ollama"         # "openai" | "anthropic" | "ollama"
    fallback_model: str = ollama_primary_model

    # ── API keys (never hard-code; always via env / Secrets Manager) ──────────
    anthropic_api_key: str = ""
    openai_api_key: str = ""

    # ── Self-hosted vLLM endpoint ─────────────────────────────────────────────
    vllm_endpoint: str = ""          # e.g. "http://vllm-internal:8000"
    vllm_model: str = ""             # e.g. "meta-llama/Llama-3.3-70B-Instruct"
    vllm_concurrency_limit: int = 4  # parallel requests to vLLM (tune to GPU memory)

    # ── Embedding provider ────────────────────────────────────────────────────
    embedding_provider: str = "ollama"              # "openai" | "ollama"
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
    enable_docs: bool = True  # set False in production to hide Swagger UI

    # ── API key auth + rate limiting ──────────────────────────────────────────
    # Comma-separated list of valid API keys (set via env var API_KEYS).
    # Empty = auth disabled (development only — never deploy without keys).
    api_keys: str = ""
    rate_limit_investigations_per_day: int = 50
    rate_limit_uploads_per_hour: int = 100

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
        """True when at least one tier is routed to a managed API provider."""
        return self.inference_mode in (InferenceMode.API, InferenceMode.HYBRID) or (
            self.critical_provider in ("anthropic", "openai")
            or self.domain_provider in ("anthropic", "openai")
        )


settings = Settings()
