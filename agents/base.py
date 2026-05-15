"""
Base agent class. All investigation agents inherit from this.
Provides: LLM access, storage access, evidence building, structured output.
"""

import time
import uuid
from abc import ABC, abstractmethod
from typing import Any, Literal

import structlog

from llm.client import get_llm_client
from llm.inference_client import InferenceClient
from storage.parquet_store import ParquetStore

# Backward-compat: OllamaClient kept importable from agents.base for tests
# that do `from agents.base import ...`.  Remove once all tests migrate.
try:
    from llm.client import OllamaClient
except ImportError:
    OllamaClient = None  # type: ignore[assignment,misc]

HypothesisStatus = Literal["forming", "supported", "refuted", "confirmed"]


def make_hypothesis(
    *,
    title: str,
    description: str,
    agent_source: str,
    confidence: float,
    status: HypothesisStatus = "forming",
    evidence_for: list[str] | None = None,
    evidence_against: list[str] | None = None,
    missing_evidence: list[str] | None = None,
    id: str | None = None,
) -> dict:
    """
    Factory for canonical hypothesis dicts matching HypothesisRecord schema.
    All agents must use this instead of building ad-hoc dicts.
    """
    return {
        "id": id or str(uuid.uuid4()),
        "title": title,
        "description": description,
        "agent_source": agent_source,
        "confidence": confidence,
        "status": status,
        "evidence_for": evidence_for or [],
        "evidence_against": evidence_against or [],
        "missing_evidence": missing_evidence or [],
    }

log = structlog.get_logger(__name__)


class BaseAgent(ABC):
    AGENT_NAME: str
    AGENT_ROLE: str  # Short role description e.g. "[EKF] EKF Diagnostician"

    def __init__(
        self,
        flight_id: str,
        investigation_id: str,
        store: ParquetStore | None = None,
        llm: "InferenceClient | OllamaClient | None" = None,
    ):
        self.flight_id = flight_id
        self.investigation_id = investigation_id
        self.store = store or ParquetStore()
        self.llm: InferenceClient = llm or get_llm_client()  # type: ignore[assignment]
        self.log = structlog.get_logger(self.AGENT_NAME)

    @abstractmethod
    def run(self, state: dict) -> dict:
        """
        Execute the agent's investigation.

        Args:
            state: Current InvestigationState dict

        Returns:
            Updated state dict with agent findings added.
            Must add: state["agent_findings"][AGENT_NAME] = AgentFinding(...)
            Must add: state["messages"].append(...)
            Must add to: state["anomalies"] and state["hypotheses"] as appropriate.
        """
        ...

    def emit(self, state: dict, message: str, level: str = "info"):
        """Emit a progress message to the investigation stream."""
        state.setdefault("messages", []).append({
            "agent": self.AGENT_NAME,
            "level": level,
            "message": message,
            "timestamp": time.time(),
        })
        self.log.info(message)

    def load_data(self, *message_types: str, **kwargs) -> dict:
        """Load multiple message type DataFrames at once."""
        return self.store.load_many(self.flight_id, list(message_types), **kwargs)

    def build_evidence_package(self, findings: dict) -> str:
        """
        Build a structured evidence string for LLM consumption.
        Only includes fields that are NOT None (avoids hallucination surface).
        """
        lines = [f"EVIDENCE PACKAGE — {self.AGENT_NAME}"]
        lines.append("=" * 60)
        for key, val in findings.items():
            if val is None or val == [] or val == {}:
                lines.append(f"{key}: NOT AVAILABLE IN LOG")
            elif isinstance(val, float):
                lines.append(f"{key}: {val:.4f}")
            elif isinstance(val, list) and len(val) > 10:
                lines.append(f"{key}: [{val[0]:.2f} ... {val[-1]:.2f}] ({len(val)} values)")
            else:
                lines.append(f"{key}: {val}")
        return "\n".join(lines)

    def timed_llm_call(self, fn, *args, **kwargs):
        """Wrap LLM call with timing log."""
        start = time.monotonic()
        result = fn(*args, **kwargs)
        elapsed = time.monotonic() - start
        self.log.info("llm_call_complete", elapsed_s=f"{elapsed:.1f}")
        return result
