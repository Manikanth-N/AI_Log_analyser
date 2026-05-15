from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # Ollama
    ollama_url: str = "http://localhost:11434"
    ollama_primary_model: str = "qwen3:32b-q4_K_M"  # production model; override via env for smoke test
    ollama_fast_model: str = "qwen3:8b-q4_K_M"
    ollama_embedding_model: str = "nomic-embed-text:v1.5"
    ollama_timeout_seconds: int = 1200  # 20 min — local CPU inference is slow
    ollama_context_length: int = 32768

    # Parsing
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

    @property
    def raw_storage(self) -> Path:
        return self.storage_root / "raw"

    @property
    def flights_storage(self) -> Path:
        return self.storage_root / "flights"

    @property
    def baselines_storage(self) -> Path:
        return self.storage_root / "baselines"


settings = Settings()
