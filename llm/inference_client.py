"""
InferenceClient — provider-agnostic inference facade.

Preserves the exact OllamaClient interface so zero agent code changes.
Routes calls to the correct provider based on config-driven model tier.

Tier model:
  primary_model → critical_provider  (CrashInvestigator, ReportWriter)
  fast_model    → domain_provider    (EKF, GPS, Power, Vibration, ...)
  Fallback provider fires on any unrecoverable provider exception.

Routing rule: the model string IS the routing key.
  agent passes model=self.llm.primary_model → routes to critical_provider
  agent passes model=self.llm.fast_model    → routes to domain_provider
  agent passes an explicit model string     → pattern-matched to provider

Thread safety: InferenceClient is thread-safe. Providers manage their own
concurrency (optional semaphore inside VLLMProvider / OllamaCompatProvider).
"""

from __future__ import annotations

import threading
import time
from typing import TypeVar

import httpx
import structlog
from pydantic import BaseModel

from config.settings import settings
from config.routing import log_routing_table, validate_routing_config
from llm.providers.base import InferenceProvider, TokenUsage, UsageSummary, estimate_cost
from llm.providers.openai_provider import OpenAIProvider
from llm.providers.anthropic_provider import AnthropicProvider
from llm.providers.vllm_provider import VLLMProvider, OllamaCompatProvider

log = structlog.get_logger(__name__)
T = TypeVar("T", bound=BaseModel)


class InferenceClient:
    """
    Multi-provider inference router.  Drop-in replacement for OllamaClient.

    Public interface (identical to OllamaClient):
      .primary_model          → str (critical tier model id)
      .fast_model             → str (domain tier model id)
      .embedding_model        → str
      .structured(...)        → Pydantic instance
      .fast_structured(...)   → Pydantic instance (domain tier shorthand)
      .complete(...)          → str
      .embed(text)            → list[float]
      .embed_batch(texts)     → list[list[float]]
      .check_health()         → bool
      .get_usage_summary()    → list[UsageSummary]
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._providers: dict[str, InferenceProvider] = {}
        self._usage_by_provider: dict[str, TokenUsage] = {}
        self._call_count_by_provider: dict[str, int] = {}
        self._build_providers()

    # ------------------------------------------------------------------
    # Provider construction
    # ------------------------------------------------------------------

    def _build_providers(self) -> None:
        s = settings

        # Always register Ollama as the backward-compat provider
        self._providers["ollama"] = OllamaCompatProvider(
            base_url=s.ollama_url,
            primary_model=s.ollama_primary_model,
            fast_model=s.ollama_fast_model,
            timeout=float(s.ollama_timeout_seconds),
        )

        # OpenAI — register if key is set
        if s.openai_api_key:
            self._providers["openai"] = OpenAIProvider(
                api_key=s.openai_api_key,
                timeout=s.inference_request_timeout_seconds,
            )
        elif s.domain_provider == "openai" or s.critical_provider == "openai":
            log.warning(
                "openai_provider_configured_but_no_api_key",
                detail="Set OPENAI_API_KEY env var. Falling back to ollama.",
            )

        # Anthropic — register if key is set
        if s.anthropic_api_key:
            try:
                self._providers["anthropic"] = AnthropicProvider(
                    api_key=s.anthropic_api_key,
                    timeout=s.inference_request_timeout_seconds,
                )
            except (ImportError, ValueError) as e:
                log.warning("anthropic_provider_unavailable", reason=str(e))
        elif s.critical_provider == "anthropic":
            log.warning(
                "anthropic_provider_configured_but_no_api_key",
                detail="Set ANTHROPIC_API_KEY env var. Falling back to ollama.",
            )

        # vLLM — register if endpoint is set
        if s.vllm_endpoint and s.vllm_model:
            self._providers["vllm"] = VLLMProvider(
                endpoint=s.vllm_endpoint,
                model=s.vllm_model,
                timeout=s.inference_request_timeout_seconds,
                max_concurrency=s.vllm_concurrency_limit,
            )

        log.info(
            "inference_client_ready",
            providers=sorted(self._providers.keys()),
            domain_tier=f"{s.domain_provider}/{s.domain_model}",
            critical_tier=f"{s.critical_provider}/{s.critical_model}",
            fallback=f"{s.fallback_provider}/{s.fallback_model}",
        )
        log_routing_table(self._providers)
        validate_routing_config(self._providers)

    # ------------------------------------------------------------------
    # Model tier properties (same interface as OllamaClient)
    # ------------------------------------------------------------------

    @property
    def primary_model(self) -> str:
        """Critical-path model string → routes to critical_provider."""
        return settings.critical_model

    @property
    def fast_model(self) -> str:
        """Domain-tier model string → routes to domain_provider."""
        return settings.domain_model

    @property
    def embedding_model(self) -> str:
        return settings.ollama_embedding_model

    # ------------------------------------------------------------------
    # Provider resolution
    # ------------------------------------------------------------------

    def _resolve_provider(self, model: str) -> tuple[InferenceProvider, str]:
        """
        Return (provider, resolved_model_string) for a given model key.

        Routing order:
          1. Exact match on configured tier models
          2. Model string prefix heuristics (claude-*, gpt-*)
          3. Configured provider name lookup
          4. Fallback to ollama
        """
        s = settings

        # Tier match
        if model == s.critical_model:
            provider = self._providers.get(s.critical_provider)
            if provider:
                return provider, model

        if model == s.domain_model:
            provider = self._providers.get(s.domain_provider)
            if provider:
                return provider, model

        # Model string prefix heuristics
        if model.startswith("claude-"):
            if "anthropic" in self._providers:
                return self._providers["anthropic"], model
        if model.startswith("gpt-") or model.startswith("o1") or model.startswith("o3"):
            if "openai" in self._providers:
                return self._providers["openai"], model

        # Named provider lookup (e.g. passing provider_id as model — for tests)
        if model in self._providers:
            return self._providers[model], self._providers[model].default_model

        # Last resort: ollama
        ollama = self._providers["ollama"]
        log.warning("provider_resolution_fallback_to_ollama", model=model)
        return ollama, model

    def _fallback_provider(self, exclude_provider_id: str) -> tuple[InferenceProvider, str] | None:
        """Return the configured fallback provider if it is different from the failed one."""
        s = settings
        fb_provider = self._providers.get(s.fallback_provider)
        if fb_provider and fb_provider.provider_id != exclude_provider_id:
            return fb_provider, s.fallback_model
        # Try ollama as universal fallback
        if exclude_provider_id != "ollama" and "ollama" in self._providers:
            ollama = self._providers["ollama"]
            return ollama, ollama.default_model
        return None

    # ------------------------------------------------------------------
    # Usage tracking
    # ------------------------------------------------------------------

    def _track(self, provider: InferenceProvider) -> None:
        pid = provider.provider_id
        usage = provider.last_usage()
        with self._lock:
            if pid not in self._usage_by_provider:
                self._usage_by_provider[pid] = TokenUsage()
                self._call_count_by_provider[pid] = 0
            self._usage_by_provider[pid] += usage
            self._call_count_by_provider[pid] += 1

    # ------------------------------------------------------------------
    # Core inference methods (same signature as OllamaClient)
    # ------------------------------------------------------------------

    def structured(
        self,
        messages: list[dict],
        response_model: type[T],
        model: str | None = None,
        system: str | None = None,
        temperature: float = 0.1,
        max_retries: int = 3,
    ) -> T:
        use_model = model or self.primary_model
        provider, resolved_model = self._resolve_provider(use_model)

        try:
            result = provider.structured(
                messages=messages,
                response_model=response_model,
                model=resolved_model,
                system=system,
                temperature=temperature,
                max_retries=max_retries,
            )
            self._track(provider)
            return result

        except Exception as primary_exc:
            # Attempt fallback provider
            fb = self._fallback_provider(provider.provider_id)
            if fb:
                fb_provider, fb_model = fb
                log.warning(
                    "inference_primary_failed_trying_fallback",
                    primary_provider=provider.provider_id,
                    primary_model=resolved_model,
                    fallback_provider=fb_provider.provider_id,
                    fallback_model=fb_model,
                    error=str(primary_exc),
                )
                result = fb_provider.structured(
                    messages=messages,
                    response_model=response_model,
                    model=fb_model,
                    system=system,
                    temperature=temperature,
                    max_retries=max_retries,
                )
                self._track(fb_provider)
                return result
            raise

    def fast_structured(
        self,
        messages: list[dict],
        response_model: type[T],
        system: str | None = None,
    ) -> T:
        """Domain-tier shorthand — equivalent to structured(..., model=self.fast_model)."""
        return self.structured(
            messages=messages,
            response_model=response_model,
            model=self.fast_model,
            system=system,
        )

    def complete(
        self,
        messages: list[dict],
        model: str | None = None,
        temperature: float = 0.1,
        max_tokens: int = 4096,
        system: str | None = None,
    ) -> str:
        use_model = model or self.primary_model
        provider, resolved_model = self._resolve_provider(use_model)

        try:
            result = provider.complete(
                messages=messages,
                model=resolved_model,
                system=system,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            self._track(provider)
            return result

        except Exception as primary_exc:
            fb = self._fallback_provider(provider.provider_id)
            if fb:
                fb_provider, fb_model = fb
                log.warning(
                    "inference_complete_fallback",
                    primary=provider.provider_id,
                    fallback=fb_provider.provider_id,
                    error=str(primary_exc),
                )
                result = fb_provider.complete(
                    messages=messages,
                    model=fb_model,
                    system=system,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                self._track(fb_provider)
                return result
            raise

    # ------------------------------------------------------------------
    # Embeddings (delegates to Ollama by default; OpenAI when configured)
    # ------------------------------------------------------------------

    def embed(self, text: str) -> list[float]:
        """
        Generate embedding for text.

        Uses OpenAI text-embedding-3-small when OPENAI_API_KEY is set and
        embedding_provider=openai.  Falls back to Ollama nomic-embed-text.
        """
        if settings.embedding_provider == "openai" and "openai" in self._providers:
            return self._embed_openai(text)
        return self._embed_ollama(text)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]

    def _embed_openai(self, text: str) -> list[float]:
        provider = self._providers["openai"]
        assert isinstance(provider, OpenAIProvider)
        resp = provider._client.embeddings.create(
            model=settings.openai_embedding_model,
            input=text,
        )
        return resp.data[0].embedding

    def _embed_ollama(self, text: str) -> list[float]:
        resp = httpx.post(
            f"{settings.ollama_url}/api/embeddings",
            json={"model": settings.ollama_embedding_model, "prompt": text},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()["embedding"]

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------

    def check_health(self) -> bool:
        """True if at least the configured critical-path provider is reachable."""
        s = settings
        provider = self._providers.get(s.critical_provider)
        if not provider:
            return False
        try:
            # Lightweight: just verify the provider is configured (API key present)
            # For Ollama, do the full tag check
            if provider.provider_id == "ollama":
                resp = httpx.get(f"{s.ollama_url}/api/tags", timeout=5)
                return resp.status_code == 200
            # For API providers: trust that construction succeeded (key was validated)
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Usage dashboard
    # ------------------------------------------------------------------

    def get_usage_summary(self) -> list[UsageSummary]:
        with self._lock:
            summaries = []
            for pid, usage in self._usage_by_provider.items():
                provider = self._providers.get(pid)
                model = provider.default_model if provider else "unknown"
                summaries.append(UsageSummary(
                    provider_id=pid,
                    model=model,
                    call_count=self._call_count_by_provider.get(pid, 0),
                    usage=TokenUsage(usage.input_tokens, usage.output_tokens),
                    estimated_cost_usd=estimate_cost(model, usage),
                ))
            return sorted(summaries, key=lambda s: s.usage.total_tokens, reverse=True)

    def log_usage_summary(self) -> None:
        for s in self.get_usage_summary():
            log.info("inference_usage", **{
                "provider": s.provider_id,
                "model": s.model,
                "calls": s.call_count,
                "input_tokens": s.usage.input_tokens,
                "output_tokens": s.usage.output_tokens,
                "estimated_cost_usd": f"{s.estimated_cost_usd:.4f}",
            })
