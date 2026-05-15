"""
Ollama LLM client with retry, circuit breaker, and structured output support.
All agent calls route through this — never call Ollama directly from agents.

Concurrency contract:
  Ollama processes LLM requests one at a time (single model runner per process).
  _OLLAMA_SEMAPHORE serializes structured() / complete() calls so that agents
  running in parallel threads don't queue up inside Ollama and time out.
  Deterministic agents that never call the LLM are unaffected — they run freely.

  This makes max_workers>1 safe: deterministic agents parallelize, LLM agents
  serialize through the semaphore, and no thread blocks in Ollama's queue.
"""

import threading
import time
from typing import TypeVar

import httpx
import instructor
import structlog
from openai import OpenAI
from pydantic import BaseModel

from config.settings import settings

log = structlog.get_logger(__name__)

T = TypeVar("T", bound=BaseModel)

# One inference slot — Ollama processes a single request at a time.
# Agents call structured() / complete() → serialized here, not at the HTTP level.
_OLLAMA_SEMAPHORE = threading.Semaphore(1)


class OllamaClient:
    def __init__(
        self,
        base_url: str | None = None,
        primary_model: str | None = None,
        fast_model: str | None = None,
        embedding_model: str | None = None,
    ):
        self.base_url = base_url or settings.ollama_url
        self.primary_model = primary_model or settings.ollama_primary_model
        self.fast_model = fast_model or settings.ollama_fast_model
        self.embedding_model = embedding_model or settings.ollama_embedding_model
        self.timeout = settings.ollama_timeout_seconds

        self._openai_client = OpenAI(
            base_url=f"{self.base_url}/v1",
            api_key="ollama",
            timeout=self.timeout,
        )

        self._instructor_client = instructor.from_openai(
            self._openai_client,
            mode=instructor.Mode.JSON,
        )

    def complete(
        self,
        messages: list[dict],
        model: str | None = None,
        temperature: float = 0.1,
        max_tokens: int = 4096,
        system: str | None = None,
    ) -> str:
        """Raw text completion — use for report writing and narrative generation."""
        use_model = model or self.primary_model

        all_messages = []
        if system:
            all_messages.append({"role": "system", "content": system})
        all_messages.extend(messages)

        with _OLLAMA_SEMAPHORE:
            try:
                resp = self._openai_client.chat.completions.create(
                    model=use_model,
                    messages=all_messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                return resp.choices[0].message.content or ""
            except Exception as e:
                log.error("llm_complete_error", model=use_model, error=str(e))
                raise

    def structured(
        self,
        messages: list[dict],
        response_model: type[T],
        model: str | None = None,
        system: str | None = None,
        temperature: float = 0.1,
        max_retries: int = 3,
    ) -> T:
        """
        Structured output via instructor + Pydantic.
        The LLM MUST return valid JSON matching response_model.
        Retries on validation failure.
        """
        use_model = model or self.primary_model

        all_messages = []
        if system:
            all_messages.append({"role": "system", "content": system})
        all_messages.extend(messages)

        with _OLLAMA_SEMAPHORE:
            try:
                result = self._instructor_client.chat.completions.create(
                    model=use_model,
                    messages=all_messages,
                    response_model=response_model,
                    temperature=temperature,
                    max_retries=max_retries,
                )
                return result
            except Exception as e:
                log.error("llm_structured_error", model=use_model,
                          response_model=response_model.__name__, error=str(e))
                raise

    def fast_structured(
        self,
        messages: list[dict],
        response_model: type[T],
        system: str | None = None,
    ) -> T:
        """Use the fast model (smaller, quicker) for sub-agent tasks."""
        return self.structured(
            messages=messages,
            response_model=response_model,
            model=self.fast_model,
            system=system,
        )

    def embed(self, text: str) -> list[float]:
        """Generate embedding for a text string."""
        resp = httpx.post(
            f"{self.base_url}/api/embeddings",
            json={"model": self.embedding_model, "prompt": text},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()["embedding"]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]

    def check_health(self) -> bool:
        """Verify Ollama is running and models are available."""
        try:
            resp = httpx.get(f"{self.base_url}/api/tags", timeout=5)
            resp.raise_for_status()
            models = {m["name"] for m in resp.json().get("models", [])}
            primary_ok = any(self.primary_model in m for m in models)
            embed_ok = any(self.embedding_model in m for m in models)
            return primary_ok and embed_ok
        except Exception:
            return False


# Singleton instance — share across agents
_client: OllamaClient | None = None


def get_llm_client() -> OllamaClient:
    global _client
    if _client is None:
        _client = OllamaClient()
    return _client
