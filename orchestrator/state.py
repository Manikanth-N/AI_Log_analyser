"""Investigation state — shared across all LangGraph nodes."""

from typing import Annotated, Any
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict


class InvestigationState(TypedDict):
    # Identity
    flight_id: str
    investigation_id: str
    user_query: str

    # Timeline
    flight_phases: list[dict]
    event_timeline: list[dict]

    # Hypotheses lifecycle
    hypotheses: list[dict]

    # Unified anomaly registry (all agents write here)
    anomalies: list[dict]

    # Per-agent findings
    agent_findings: dict[str, Any]

    # Evidence store (key-value, for cross-agent data sharing)
    evidence_store: dict[str, Any]

    # Investigation control
    iteration: int
    max_iterations: int
    needs_more_evidence: bool

    # Final outputs
    root_cause: str | None
    confidence: str | None        # HIGH / MEDIUM / LOW / DEFINITIVE from CrashInvestigator
    contributing_factors: list[str]
    recommendations: list[str]
    open_questions: list[str]
    final_report: dict | None
    report_path: str | None

    # Agent activity stream (published to Redis pubsub)
    messages: list[dict]

    # Error handling
    errors: list[str]
