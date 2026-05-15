"""
vLLM inference provider.

vLLM exposes an OpenAI-compatible /v1 API, so this is a thin wrapper over
OpenAIProvider that:
  - Sets provider_id to "vllm"
  - Configures the concurrency semaphore (GPU has finite capacity)
  - Uses JSON instructor mode (TOOLS mode requires function calling support;
    JSON mode is universally supported by vLLM)
  - Stores the configured model name (vLLM serves one model per deployment)

Migration note: when moving from Ollama to vLLM, update:
  VLLM_ENDPOINT=http://<host>:8000
  VLLM_MODEL=meta-llama/Llama-3.3-70B-Instruct   (or whatever is loaded)
  VLLM_CONCURRENCY_LIMIT=4   (tune to your GPU memory / batch capacity)
"""

from __future__ import annotations

import instructor
import structlog

from .openai_provider import OpenAIProvider

log = structlog.get_logger(__name__)


class VLLMProvider(OpenAIProvider):
    """
    vLLM inference server (OpenAI-compatible endpoint).

    Use this for:
      - Self-hosted vLLM on AWS g5 / p4 instances
      - On-premise enterprise GPU deployments
      - Groq / Together AI / Fireworks (set appropriate api_key)
    """

    def __init__(
        self,
        endpoint: str,
        model: str,
        api_key: str = "vllm",
        timeout: float = 120.0,
        max_concurrency: int = 4,
    ):
        # vLLM's structured output is most reliable in JSON mode.
        # If the model supports function calling (Llama 3.1+, Qwen2.5+),
        # switch to instructor.Mode.TOOLS for better schema compliance.
        super().__init__(
            api_key=api_key,
            base_url=f"{endpoint.rstrip('/')}/v1",
            timeout=timeout,
            max_concurrency=max_concurrency,
            instructor_mode=instructor.Mode.JSON,
        )
        self._model = model
        self._pid = "vllm"

    @property
    def provider_id(self) -> str:
        return "vllm"

    @property
    def default_model(self) -> str:
        return self._model


class OllamaCompatProvider(OpenAIProvider):
    """
    Ollama via its OpenAI-compatible /v1 endpoint.

    This is the backward-compatible path for local development.
    Production should use VLLMProvider instead.

    Serialises all calls through a single semaphore (Ollama is single-threaded).
    """

    def __init__(
        self,
        base_url: str,
        primary_model: str,
        fast_model: str,
        timeout: float = 1200,
    ):
        super().__init__(
            api_key="ollama",
            base_url=f"{base_url.rstrip('/')}/v1",
            timeout=timeout,
            max_concurrency=1,          # Ollama: one request at a time
            instructor_mode=instructor.Mode.JSON,
        )
        self._primary = primary_model
        self._fast = fast_model
        self._pid = "ollama"

    @property
    def provider_id(self) -> str:
        return "ollama"

    @property
    def default_model(self) -> str:
        return self._primary

    @property
    def fast_model_name(self) -> str:
        return self._fast
