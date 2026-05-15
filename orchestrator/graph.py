"""
LangGraph investigation graph.
Coordinates all agents through the 9-step forensic investigation protocol.
"""

import asyncio
import json
from typing import Any

import structlog
from langgraph.graph import StateGraph, END

from agents import (
    FlightTimelineAgent,
    EKFDiagnosticsAgent,
    GPSIntegrityAgent,
    PowerSystemAgent,
    VibrationAnalysisAgent,
    ESCMotorAgent,
    MissionBehaviorAgent,
    FlightDynamicsAgent,
    ParameterDriftAgent,
    ComparativeAnalystAgent,
    SafetyComplianceAgent,
    CrashInvestigatorAgent,
    ReportWriterAgent,
)
from storage.parquet_store import ParquetStore
from .state import InvestigationState

log = structlog.get_logger(__name__)


def _make_node(agent_cls, flight_id: str, investigation_id: str, store: ParquetStore):
    """Create a LangGraph node from an agent class."""
    agent = agent_cls(flight_id=flight_id, investigation_id=investigation_id, store=store)

    def node_fn(state: InvestigationState) -> InvestigationState:
        try:
            return agent.run(state)
        except Exception as e:
            log.error("agent_error", agent=agent_cls.AGENT_NAME, error=str(e))
            state.setdefault("errors", []).append(f"{agent_cls.AGENT_NAME}: {str(e)}")
            return state

    node_fn.__name__ = agent_cls.AGENT_NAME
    return node_fn


def _parallel_domain_analysis(flight_id: str, investigation_id: str, store: ParquetStore):
    """
    Run all domain analysis agents.
    In LangGraph, these are sequential nodes — true parallelism
    is achieved via concurrent Celery tasks at the task level.
    In-process parallelism can be added with ThreadPoolExecutor if desired.
    """
    agents = [
        EKFDiagnosticsAgent,
        GPSIntegrityAgent,
        PowerSystemAgent,
        VibrationAnalysisAgent,
        ESCMotorAgent,
        MissionBehaviorAgent,
        FlightDynamicsAgent,
        ParameterDriftAgent,
        SafetyComplianceAgent,
    ]

    def parallel_node(state: InvestigationState) -> InvestigationState:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        # Agents run in parallel threads. LLM calls are serialized by
        # _OLLAMA_SEMAPHORE in llm/client.py — deterministic agents (ESCMotor,
        # MissionBehavior, FlightDynamics, ParameterDrift, SafetyCompliance)
        # run freely while LLM-calling agents queue at the semaphore.
        #
        # max_workers caps the thread count. 9 workers = one thread per agent.
        # Per-future timeout must cover the worst case: all LLM agents queued
        # behind each other. With ~200s per LLM call and 5 LLM agents:
        # timeout = 5 × ollama_timeout ≈ 5 × 1200 = 6000s (capped to 2× setting).
        from config.settings import settings as _s
        _agent_timeout = _s.ollama_timeout_seconds * 2

        with ThreadPoolExecutor(max_workers=9) as executor:
            futures = {
                executor.submit(
                    _make_node(cls, flight_id, investigation_id, store),
                    {
                        **state,
                        "hypotheses": [],
                        "anomalies": [],
                        "messages": [],
                        "errors": [],
                        "agent_findings": {},
                    },
                ): cls.AGENT_NAME
                for cls in agents
            }

            for future in as_completed(futures):
                agent_name = futures[future]
                try:
                    result_state = future.result(timeout=_agent_timeout)
                    # Merge results back into shared state
                    state.setdefault("anomalies", []).extend(
                        result_state.get("anomalies", [])
                    )
                    state.setdefault("agent_findings", {}).update(
                        result_state.get("agent_findings", {})
                    )
                    state.setdefault("hypotheses", []).extend(
                        result_state.get("hypotheses", [])
                    )
                    state.setdefault("messages", []).extend(
                        result_state.get("messages", [])
                    )
                except Exception as e:
                    log.error("parallel_agent_error", agent=agent_name, error=str(e))
                    state.setdefault("errors", []).append(f"{agent_name}: {str(e)}")

        # Deduplicate anomalies by rule+timestamp
        seen = set()
        unique_anomalies = []
        for a in state.get("anomalies", []):
            key = (a.get("rule_name"), a.get("timestamp_us"))
            if key not in seen:
                seen.add(key)
                unique_anomalies.append(a)
        state["anomalies"] = sorted(unique_anomalies, key=lambda a: a.get("timestamp_us", 0))

        return state

    return parallel_node


def _should_gather_more_evidence(state: InvestigationState) -> str:
    """
    Conditional edge: determine if more evidence gathering is needed.
    Returns "gather_more" or "crash_investigation".
    """
    iteration = state.get("iteration", 0)
    max_iter = state.get("max_iterations", 3)

    if iteration >= max_iter:
        return "crash_investigation"

    # Check if any agent requested more data
    needs_more = state.get("needs_more_evidence", False)
    if needs_more:
        state["iteration"] = iteration + 1
        return "domain_analysis"

    return "crash_investigation"


def build_investigation_graph(
    flight_id: str,
    investigation_id: str,
    store: ParquetStore | None = None,
) -> StateGraph:
    """
    Build the complete investigation graph.

    Flow:
    timeline → domain_analysis (parallel) → [conditional: more_evidence?]
    → crash_investigation → report_writer → END
    """
    store = store or ParquetStore()

    graph = StateGraph(InvestigationState)

    # Node: STEP 2-3 — Timeline & phase reconstruction
    graph.add_node(
        "timeline",
        _make_node(FlightTimelineAgent, flight_id, investigation_id, store),
    )

    # Node: STEP 4-5 — All domain analysis agents (parallel)
    graph.add_node(
        "domain_analysis",
        _parallel_domain_analysis(flight_id, investigation_id, store),
    )

    # Node: STEP 5b — Comparative baseline analysis (runs after domain agents)
    graph.add_node(
        "comparative_analysis",
        _make_node(ComparativeAnalystAgent, flight_id, investigation_id, store),
    )

    # Node: STEP 6-8 — Crash investigation & root cause
    graph.add_node(
        "crash_investigation",
        _make_node(CrashInvestigatorAgent, flight_id, investigation_id, store),
    )

    # Node: STEP 9 — Report writing
    graph.add_node(
        "report_writer",
        _make_node(ReportWriterAgent, flight_id, investigation_id, store),
    )

    # Edges
    graph.set_entry_point("timeline")
    graph.add_edge("timeline", "domain_analysis")

    # Conditional: iterate for more evidence or proceed to comparative analysis
    graph.add_conditional_edges(
        "domain_analysis",
        _should_gather_more_evidence,
        {
            "domain_analysis": "domain_analysis",
            "crash_investigation": "comparative_analysis",
        },
    )

    graph.add_edge("comparative_analysis", "crash_investigation")
    graph.add_edge("crash_investigation", "report_writer")
    graph.add_edge("report_writer", END)

    return graph


class InvestigationOrchestrator:
    """
    High-level orchestrator — compiles the graph and runs it.
    Used by Celery tasks.
    """

    def __init__(
        self,
        flight_id: str,
        investigation_id: str,
        store: ParquetStore | None = None,
        pubsub_client=None,
    ):
        self.flight_id = flight_id
        self.investigation_id = investigation_id
        self.store = store or ParquetStore()
        self.pubsub = pubsub_client

    def run(
        self,
        user_query: str = "Perform complete forensic investigation",
        max_iterations: int = 3,
    ) -> dict:
        graph = build_investigation_graph(
            self.flight_id,
            self.investigation_id,
            self.store,
        )
        compiled = graph.compile()

        # Seed with parse-phase anomalies so all 669 detections are available
        # from the start; agents re-detect their own subset and merge on top.
        parse_anomalies = self.store.read_derived(self.flight_id, "anomalies_fast") or []

        initial_state: InvestigationState = {
            "flight_id": self.flight_id,
            "investigation_id": self.investigation_id,
            "user_query": user_query,
            "flight_phases": [],
            "event_timeline": [],
            "hypotheses": [],
            "anomalies": parse_anomalies,
            "agent_findings": {},
            "evidence_store": {},
            "iteration": 0,
            "max_iterations": max_iterations,
            "needs_more_evidence": False,
            "root_cause": None,
            "confidence": None,
            "contributing_factors": [],
            "recommendations": [],
            "open_questions": [],
            "final_report": None,
            "report_path": None,
            "messages": [],
            "errors": [],
        }

        log.info("investigation_start", flight_id=self.flight_id, query=user_query)

        # Stream values (accumulated state after each node) for correct final state.
        # Track message count to emit only newly added messages per step.
        prev_msg_count = 0
        final_state: dict = {}
        for chunk in compiled.stream(initial_state, stream_mode="values"):
            new_messages = chunk.get("messages", [])[prev_msg_count:]
            prev_msg_count = len(chunk.get("messages", []))
            if new_messages and self.pubsub:
                for msg in new_messages:
                    self._publish(msg)
            final_state = chunk

        log.info(
            "investigation_complete",
            flight_id=self.flight_id,
            anomalies=len(final_state.get("anomalies", [])),
        )

        return final_state

    def _publish(self, message: dict):
        """Publish agent activity to Redis pubsub for SSE streaming."""
        if self.pubsub:
            try:
                self.pubsub.publish(
                    f"inv:{self.investigation_id}",
                    json.dumps(message, default=str),
                )
            except Exception as e:
                log.warning("pubsub_error", error=str(e))
