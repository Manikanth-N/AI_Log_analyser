"""
Agent routing policy — explicit tier mapping for every agent in the system.

This module is the single source of truth for which inference tier each agent uses.
It is used by:
  - InferenceClient._build_providers() for startup validation
  - Benchmark runner for per-run routing overrides
  - Documentation and auditing

Tier definitions:
  CRITICAL — primary_model (best reasoning quality; CrashInvestigator, ReportWriter)
  DOMAIN   — fast_model   (high-volume, domain-specific structured output)
  EMBED    — embed()      (vector search; no structured output)
  NONE     — no LLM calls (fully deterministic)
"""

from __future__ import annotations

import structlog
from enum import Enum

log = structlog.get_logger(__name__)


class AgentTier(str, Enum):
    CRITICAL = "critical"   # primary_model — safety-critical synthesis
    DOMAIN   = "domain"     # fast_model    — domain diagnostic agents
    EMBED    = "embed"      # embed() only  — comparative / similarity search
    NONE     = "none"       # no LLM        — fully deterministic


# ── Routing table ─────────────────────────────────────────────────────────────
#
# Every agent that exists in agents/ MUST appear here.
# Missing entries are caught by validate_routing_coverage() at startup.
#
AGENT_ROUTING: dict[str, AgentTier] = {
    # ── Critical path (primary_model) ─────────────────────────────────────────
    "CrashInvestigatorAgent": AgentTier.CRITICAL,
    "ReportWriterAgent":      AgentTier.CRITICAL,
    # ── Domain agents (fast_model) ────────────────────────────────────────────
    "EKFDiagnosticsAgent":    AgentTier.DOMAIN,
    "GPSIntegrityAgent":      AgentTier.DOMAIN,
    "PowerSystemAgent":       AgentTier.DOMAIN,
    "VibrationAnalysisAgent": AgentTier.DOMAIN,
    # ── Embedding only ────────────────────────────────────────────────────────
    "ComparativeAnalystAgent": AgentTier.EMBED,
    # ── Deterministic (no LLM) ────────────────────────────────────────────────
    "FlightTimelineAgent":    AgentTier.NONE,
    "ESCMotorAgent":          AgentTier.NONE,
    "FlightDynamicsAgent":    AgentTier.NONE,
    "MissionBehaviorAgent":   AgentTier.NONE,
    "ParameterDriftAgent":    AgentTier.NONE,
    "SafetyComplianceAgent":  AgentTier.NONE,
}


# ── Derived groupings ─────────────────────────────────────────────────────────

CRITICAL_AGENTS = frozenset(
    name for name, tier in AGENT_ROUTING.items() if tier == AgentTier.CRITICAL
)
DOMAIN_AGENTS = frozenset(
    name for name, tier in AGENT_ROUTING.items() if tier == AgentTier.DOMAIN
)
LLM_AGENTS = frozenset(
    name for name, tier in AGENT_ROUTING.items()
    if tier in (AgentTier.CRITICAL, AgentTier.DOMAIN)
)


# ── Startup validation ────────────────────────────────────────────────────────

def validate_routing_config(providers: dict, _settings=None) -> list[str]:
    """
    Check that the configured providers can serve the current routing policy.
    Returns a list of warning strings (empty = clean).

    _settings: override for testing; uses the global settings singleton if None.
    """
    from config.settings import settings as _global_settings
    s = _settings if _settings is not None else _global_settings

    warnings: list[str] = []

    # Critical-path provider check
    if s.critical_provider not in providers:
        warnings.append(
            f"critical_provider={s.critical_provider!r} not registered — "
            f"CrashInvestigatorAgent and ReportWriterAgent will fall back to ollama. "
            f"Check CRITICAL_PROVIDER and the corresponding API key env var."
        )

    # Domain provider check
    if s.domain_provider not in providers:
        warnings.append(
            f"domain_provider={s.domain_provider!r} not registered — "
            f"domain agents (EKF, GPS, Power, Vibration) will fall back to ollama. "
            f"Check DOMAIN_PROVIDER and the corresponding API key env var."
        )

    # Fallback provider check
    if s.fallback_provider not in providers:
        warnings.append(
            f"fallback_provider={s.fallback_provider!r} not registered — "
            f"provider failures have no fallback (ollama will be used as last resort)."
        )

    # API mode with missing keys
    from config.settings import InferenceMode  # local import — avoids circular import
    if s.inference_mode in (InferenceMode.API, InferenceMode.HYBRID):
        if s.critical_provider == "anthropic" and not s.anthropic_api_key:
            warnings.append(
                "INFERENCE_MODE=api but ANTHROPIC_API_KEY is not set — "
                "critical path will fall back to ollama."
            )
        if s.domain_provider == "openai" and not s.openai_api_key:
            warnings.append(
                "INFERENCE_MODE=api but OPENAI_API_KEY is not set — "
                "domain agents will fall back to ollama."
            )
        if s.critical_provider == "openai" and not s.openai_api_key:
            warnings.append(
                "INFERENCE_MODE=api but OPENAI_API_KEY is not set — "
                "critical path will fall back to ollama."
            )

    for w in warnings:
        log.warning("routing_config_warning", detail=w)

    return warnings


def log_routing_table(providers: dict) -> None:
    """Log the effective routing table at startup (once per process)."""
    from config.settings import settings as _global_settings
    s = _global_settings

    log.info(
        "agent_routing_table",
        inference_mode=s.inference_mode,
        critical_path={
            "agents": sorted(CRITICAL_AGENTS),
            "provider": s.critical_provider,
            "model": s.critical_model,
            "registered": s.critical_provider in providers,
        },
        domain_agents={
            "agents": sorted(DOMAIN_AGENTS),
            "provider": s.domain_provider,
            "model": s.domain_model,
            "registered": s.domain_provider in providers,
        },
        fallback={
            "provider": s.fallback_provider,
            "model": s.fallback_model,
        },
    )
