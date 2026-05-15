"""
OpenAI inference provider.

Handles:
  - OpenAI API (gpt-4o, gpt-4o-mini, etc.)
  - Any OpenAI-compatible endpoint (Groq, Together AI, Fireworks, etc.)
  - Ollama's OpenAI-compatible /v1 endpoint (legacy path)

Uses instructor for structured output with Pydantic schema validation.
"""

from __future__ import annotations

import threading
from typing import TypeVar

import instructor
import structlog
from openai import OpenAI, APIError, APITimeoutError, RateLimitError
from pydantic import BaseModel

from .base import InferenceProvider, TokenUsage

log = structlog.get_logger(__name__)
T = TypeVar("T", bound=BaseModel)


class OpenAIProvider:
    """
    OpenAI-compatible inference provider.

    base_url=None → official OpenAI API
    base_url="http://....:8000/v1" → vLLM, Groq, Together, Ollama, etc.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        timeout: float = 120.0,
        max_concurrency: int | None = None,
        instructor_mode: instructor.Mode = instructor.Mode.TOOLS,
    ):
        self._client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
        )
        self._instructor = instructor.from_openai(self._client, mode=instructor_mode)
        self._last_usage = TokenUsage()
        # Optional semaphore — used by vLLM/Ollama for GPU serialisation
        self._sem: threading.Semaphore | None = (
            threading.Semaphore(max_concurrency) if max_concurrency else None
        )
        self._pid = "openai" if base_url is None else "openai-compat"

    @property
    def provider_id(self) -> str:
        return self._pid

    @property
    def default_model(self) -> str:
        return "gpt-4o-mini-2024-07-18"

    def _acquire(self):
        if self._sem:
            self._sem.acquire()

    def _release(self):
        if self._sem:
            self._sem.release()

    def structured(
        self,
        messages: list[dict],
        response_model: type[T],
        model: str,
        system: str | None = None,
        temperature: float = 0.1,
        max_retries: int = 3,
    ) -> T:
        all_messages: list[dict] = []
        if system:
            all_messages.append({"role": "system", "content": system})
        all_messages.extend(messages)

        self._acquire()
        try:
            response, completion = self._instructor.chat.completions.create_with_completion(
                model=model,
                messages=all_messages,
                response_model=response_model,
                temperature=temperature,
                max_retries=max_retries,
            )
            if completion.usage:
                self._last_usage = TokenUsage(
                    input_tokens=completion.usage.prompt_tokens or 0,
                    output_tokens=completion.usage.completion_tokens or 0,
                )
            log.debug(
                "openai_structured_ok",
                model=model,
                schema=response_model.__name__,
                input_tokens=self._last_usage.input_tokens,
                output_tokens=self._last_usage.output_tokens,
            )
            return response
        except (RateLimitError, APITimeoutError, APIError) as e:
            log.error("openai_structured_error", model=model,
                      schema=response_model.__name__, error=str(e))
            raise
        finally:
            self._release()

    def complete(
        self,
        messages: list[dict],
        model: str,
        system: str | None = None,
        temperature: float = 0.1,
        max_tokens: int = 4096,
    ) -> str:
        all_messages: list[dict] = []
        if system:
            all_messages.append({"role": "system", "content": system})
        all_messages.extend(messages)

        self._acquire()
        try:
            resp = self._client.chat.completions.create(
                model=model,
                messages=all_messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            if resp.usage:
                self._last_usage = TokenUsage(
                    input_tokens=resp.usage.prompt_tokens or 0,
                    output_tokens=resp.usage.completion_tokens or 0,
                )
            return resp.choices[0].message.content or ""
        except (RateLimitError, APITimeoutError, APIError) as e:
            log.error("openai_complete_error", model=model, error=str(e))
            raise
        finally:
            self._release()

    def last_usage(self) -> TokenUsage:
        return self._last_usage
