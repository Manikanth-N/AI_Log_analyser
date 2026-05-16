"""
Inference provider protocol and shared data types.

Every concrete provider (Anthropic, OpenAI, vLLM, Bedrock) must satisfy the
InferenceProvider protocol.  The InferenceClient in llm/inference_client.py
routes calls to the correct provider based on model tier config.

Design rule: this module has NO provider-specific imports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel

if TYPE_CHECKING:
    pass

T = TypeVar("T", bound=BaseModel)


# ---------------------------------------------------------------------------
# Token accounting
# ---------------------------------------------------------------------------

@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )

    def __iadd__(self, other: "TokenUsage") -> "TokenUsage":
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        return self


@dataclass
class UsageSummary:
    provider_id: str
    model: str
    call_count: int
    usage: TokenUsage
    # Estimated cost in USD using hard-coded rates (best-effort, not billing)
    estimated_cost_usd: float = 0.0

    def __str__(self) -> str:
        return (
            f"{self.provider_id}/{self.model}: "
            f"{self.call_count} calls, "
            f"{self.usage.input_tokens:,} in / {self.usage.output_tokens:,} out tokens, "
            f"~${self.estimated_cost_usd:.4f}"
        )


# ---------------------------------------------------------------------------
# Provider protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class InferenceProvider(Protocol):
    """
    Structural protocol every inference provider must satisfy.

    Providers are stateless with respect to business logic.  They own:
      - auth / connection management
      - per-call retry on transient errors
      - token usage tracking (last_usage)

    They do NOT own:
      - provider routing / fallback (that is InferenceClient)
      - schema validation beyond instructor's built-in retries
      - semaphore / concurrency control (except vLLM which may need it)
    """

    @property
    def provider_id(self) -> str:
        """Stable identifier: "anthropic" | "openai" | "vllm" | "ollama"."""
        ...

    @property
    def default_model(self) -> str:
        """Default model string for this provider (used when caller passes None)."""
        ...

    def structured(
        self,
        messages: list[dict],
        response_model: type[T],
        model: str,
        system: str | None = None,
        temperature: float = 0.1,
        max_retries: int = 3,
    ) -> T:
        """
        Structured JSON output — returns a validated Pydantic instance.
        Raises on unrecoverable failure after max_retries.
        """
        ...

    def complete(
        self,
        messages: list[dict],
        model: str,
        system: str | None = None,
        temperature: float = 0.1,
        max_tokens: int = 4096,
    ) -> str:
        """Raw text completion — for narrative generation tasks."""
        ...

    def last_usage(self) -> TokenUsage:
        """Token usage from the most recent call (input + output)."""
        ...


# ---------------------------------------------------------------------------
# Pricing constants (best-effort, for cost dashboards — not billing)
# Per-million-token rates as of mid-2026; update quarterly.
# ---------------------------------------------------------------------------

_PRICE_PER_M: dict[str, tuple[float, float]] = {
    # (input_per_M_usd, output_per_M_usd)
    "claude-sonnet-4-6":         (3.00,  15.00),
    "claude-sonnet-4-5":         (3.00,  15.00),
    "claude-opus-4-7":           (15.00, 75.00),
    "claude-haiku-4-5":          (0.80,   4.00),
    "gpt-4o-2024-11-20":         (2.50,  10.00),
    "gpt-4o-mini-2024-07-18":    (0.15,   0.60),
    "llama-3.3-70b-versatile":   (0.59,   0.79),  # Groq
}


def estimate_cost(model: str, usage: TokenUsage) -> float:
    """Best-effort USD cost estimate based on known pricing tables."""
    for key, (in_rate, out_rate) in _PRICE_PER_M.items():
        if model.startswith(key) or key in model:
            return (usage.input_tokens * in_rate + usage.output_tokens * out_rate) / 1_000_000
    return 0.0  # unknown model — return 0, don't guess
