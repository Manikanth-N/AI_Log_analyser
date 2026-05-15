"""
Unit tests for config/routing.py — agent tier assignments and startup validation.
"""

from __future__ import annotations

import pytest

from config.routing import (
    AGENT_ROUTING,
    AgentTier,
    CRITICAL_AGENTS,
    DOMAIN_AGENTS,
    LLM_AGENTS,
    validate_routing_config,
)


# ── Routing table completeness ────────────────────────────────────────────────

KNOWN_AGENTS = {
    # Critical path
    "CrashInvestigatorAgent",
    "ReportWriterAgent",
    # Domain (LLM)
    "EKFDiagnosticsAgent",
    "GPSIntegrityAgent",
    "PowerSystemAgent",
    "VibrationAnalysisAgent",
    # Embed only
    "ComparativeAnalystAgent",
    # Deterministic
    "FlightTimelineAgent",
    "ESCMotorAgent",
    "FlightDynamicsAgent",
    "MissionBehaviorAgent",
    "ParameterDriftAgent",
    "SafetyComplianceAgent",
}


def test_all_known_agents_in_routing_table():
    missing = KNOWN_AGENTS - set(AGENT_ROUTING.keys())
    assert not missing, f"Agents missing from routing table: {missing}"


def test_no_unknown_agents_in_routing_table():
    extra = set(AGENT_ROUTING.keys()) - KNOWN_AGENTS
    assert not extra, f"Unknown agents in routing table (update KNOWN_AGENTS): {extra}"


# ── Tier assignments ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("agent", ["CrashInvestigatorAgent", "ReportWriterAgent"])
def test_critical_agents_are_critical_tier(agent):
    assert AGENT_ROUTING[agent] == AgentTier.CRITICAL


@pytest.mark.parametrize("agent", [
    "EKFDiagnosticsAgent",
    "GPSIntegrityAgent",
    "PowerSystemAgent",
    "VibrationAnalysisAgent",
])
def test_domain_agents_are_domain_tier(agent):
    assert AGENT_ROUTING[agent] == AgentTier.DOMAIN


def test_comparative_analyst_is_embed_tier():
    assert AGENT_ROUTING["ComparativeAnalystAgent"] == AgentTier.EMBED


@pytest.mark.parametrize("agent", [
    "FlightTimelineAgent",
    "ESCMotorAgent",
    "FlightDynamicsAgent",
    "MissionBehaviorAgent",
    "ParameterDriftAgent",
    "SafetyComplianceAgent",
])
def test_deterministic_agents_are_none_tier(agent):
    assert AGENT_ROUTING[agent] == AgentTier.NONE


# ── Derived groupings ─────────────────────────────────────────────────────────

def test_critical_agents_set():
    assert CRITICAL_AGENTS == {"CrashInvestigatorAgent", "ReportWriterAgent"}


def test_domain_agents_set():
    assert DOMAIN_AGENTS == {
        "EKFDiagnosticsAgent",
        "GPSIntegrityAgent",
        "PowerSystemAgent",
        "VibrationAnalysisAgent",
    }


def test_llm_agents_is_critical_union_domain():
    assert LLM_AGENTS == CRITICAL_AGENTS | DOMAIN_AGENTS


def test_llm_agents_excludes_embed_and_none():
    assert "ComparativeAnalystAgent" not in LLM_AGENTS
    assert "FlightTimelineAgent" not in LLM_AGENTS


# ── Startup validation ────────────────────────────────────────────────────────

def test_validate_routing_config_clean_with_ollama_only():
    """Default: ollama is the only provider. No warnings expected."""
    providers = {"ollama": object()}
    warnings = validate_routing_config(providers)
    assert warnings == []


def test_validate_routing_config_warns_missing_critical_provider():
    """anthropic configured as critical provider but not registered → warning."""
    from config.settings import Settings, InferenceMode

    mock_settings = Settings(
        inference_mode=InferenceMode.API,
        critical_provider="anthropic",
        critical_model="claude-sonnet-4-6",
        domain_provider="openai",
        domain_model="gpt-4o-mini-2024-07-18",
        anthropic_api_key="",    # missing
        openai_api_key="sk-test",
    )
    providers = {"openai": object(), "ollama": object()}

    warnings = validate_routing_config(providers, _settings=mock_settings)

    # Should warn: anthropic not registered AND ANTHROPIC_API_KEY missing
    assert any("critical_provider" in w or "ANTHROPIC_API_KEY" in w for w in warnings)


def test_validate_routing_config_warns_missing_domain_provider():
    """openai configured as domain provider but not registered → warning."""
    from config.settings import Settings, InferenceMode

    mock_settings = Settings(
        inference_mode=InferenceMode.API,
        domain_provider="openai",
        domain_model="gpt-4o-mini-2024-07-18",
        critical_provider="ollama",
        critical_model="qwen3:32b-q4_K_M",
        openai_api_key="",  # missing
    )
    providers = {"ollama": object()}  # openai not registered

    warnings = validate_routing_config(providers, _settings=mock_settings)

    assert any("domain_provider" in w or "OPENAI_API_KEY" in w for w in warnings)


def test_validate_routing_config_no_warnings_when_providers_registered():
    """All configured providers present → no warnings."""
    from config.settings import Settings, InferenceMode

    mock_settings = Settings(
        inference_mode=InferenceMode.API,
        critical_provider="anthropic",
        critical_model="claude-sonnet-4-6",
        domain_provider="openai",
        domain_model="gpt-4o-mini-2024-07-18",
        fallback_provider="openai",
        fallback_model="gpt-4o-2024-11-20",
        anthropic_api_key="sk-ant-test",
        openai_api_key="sk-test",
    )
    providers = {"anthropic": object(), "openai": object(), "ollama": object()}

    warnings = validate_routing_config(providers, _settings=mock_settings)

    assert warnings == []


# ── InferenceMode enum ────────────────────────────────────────────────────────

def test_inference_mode_enum_values():
    from config.settings import InferenceMode
    assert InferenceMode.OLLAMA == "ollama"
    assert InferenceMode.API == "api"
    assert InferenceMode.HYBRID == "hybrid"


def test_inference_mode_rejects_invalid():
    from pydantic import ValidationError
    from config.settings import Settings

    with pytest.raises((ValidationError, ValueError)):
        Settings(inference_mode="invalid_mode")  # type: ignore[arg-type]
