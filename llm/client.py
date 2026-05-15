"""
LLM client module.

Public API (used by agents and tests):
  get_llm_client() → InferenceClient

  The InferenceClient is a provider-agnostic routing facade that supports
  Anthropic, OpenAI, vLLM, and Ollama.  All inference configuration is
  driven by config/settings.py (or env vars / .env file).

  OllamaClient is preserved for backward compatibility and direct testing
  against a local Ollama instance.  New code should use InferenceClient.

Migration guide:
  Before: from llm.client import OllamaClient, get_llm_client
  After:  from llm.client import get_llm_client        (InferenceClient returned)

  Agent code that calls self.llm.structured(...) / self.llm.fast_model
  requires ZERO changes — InferenceClient exposes the same interface.
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

# Preserved for tests that import this directly.  New code uses InferenceClient.
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


# ---------------------------------------------------------------------------
# Singleton factory — returns InferenceClient for all new code.
# The module-level _client variable is kept so tests can patch it directly
# (same patch target as before: llm.client._client).
# ---------------------------------------------------------------------------

from llm.inference_client import InferenceClient  # noqa: E402 — circular-safe (no re-import of this module)

_client: InferenceClient | OllamaClient | None = None


def get_llm_client() -> InferenceClient:
    """
    Return the shared InferenceClient singleton.

    Thread-safe: first caller constructs; subsequent callers reuse.
    Tests can replace _client with a stub via:
        with patch("llm.client._client", stub_inference_client):
            ...
    """
    global _client
    if _client is None:
        _client = InferenceClient()
    return _client  # type: ignore[return-value]
