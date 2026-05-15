"""
Benchmark runner — executes investigation and scores results against BenchmarkCase ground truth.

Scoring model:
  Each BenchmarkCase has N criteria. Each criterion is pass/fail.
  Overall score = passed_criteria / total_criteria.

  Criteria:
    1. classification_match       — report.classification == expected
    2. confidence_sufficient      — confidence >= min_confidence_level
    3. required_rules_present     — all required_rule_names in anomaly_registry
    4. root_cause_keyword_present — at least one acceptable keyword in root_cause
    5. contributing_evidence_present — at least one required evidence ID grounded
    6. no_forbidden_terms         — no forbidden term in contributing_factors (ungrounded)

Two modes:
  stub  — uses StubOllamaClient, ~15s, tests structural/schema correctness only
  live  — uses real Ollama, ~25-90 min, tests diagnostic accuracy
"""

from __future__ import annotations

import json
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from tests.benchmark.corpus import BenchmarkCase, _CONF_ORDER


@dataclass
class CriterionResult:
    name: str
    passed: bool
    detail: str


@dataclass
class BenchmarkResult:
    case_id: str
    log_filename: str
    passed: bool
    score: float                            # 0.0 – 1.0
    criteria: list[CriterionResult] = field(default_factory=list)
    classification_actual: str | None = None
    confidence_actual: str | None = None
    root_cause_actual: str | None = None
    contributing_factors_actual: list[str] = field(default_factory=list)
    anomaly_rule_names_actual: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0
    errors: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        lines = [f"[{status}] {self.case_id}  score={self.score:.0%}  "
                 f"({self.classification_actual}/{self.confidence_actual})  "
                 f"{self.elapsed_s:.0f}s"]
        for c in self.criteria:
            mark = "✓" if c.passed else "✗"
            lines.append(f"  {mark} {c.name}: {c.detail}")
        if self.errors:
            for e in self.errors:
                lines.append(f"  ! ERROR: {e}")
        return "\n".join(lines)


class BenchmarkRunner:
    """
    Runs an investigation for a BenchmarkCase and returns a BenchmarkResult.

    mode="stub"  — patches LLM with StubInferenceClient (fast, no server needed)
    mode="live"  — uses real inference (Ollama, cloud API, or hybrid)

    inference_overrides: optional dict of settings overrides for live mode, e.g.
        {
            "inference_mode": "api",
            "critical_provider": "anthropic",
            "critical_model": "claude-sonnet-4-6",
            "domain_provider": "openai",
            "domain_model": "gpt-4o-mini-2024-07-18",
        }
    These are applied on top of the current settings for the duration of the run.
    """

    def __init__(
        self,
        mode: Literal["stub", "live"] = "stub",
        logs_dir: Path | None = None,
        inference_overrides: dict | None = None,
    ):
        self.mode = mode
        self.logs_dir = logs_dir or Path(__file__).parent.parent.parent / "logs"
        self.inference_overrides = inference_overrides or {}

    def run(self, case: BenchmarkCase) -> BenchmarkResult:
        from config.settings import settings
        from storage.metadata_db import MetadataDB
        from storage.parquet_store import ParquetStore

        db = MetadataDB()
        store = ParquetStore()
        t0 = time.monotonic()
        errors: list[str] = []

        # ── Resolve flight_id (parse if needed) ──────────────────────────────
        flight_id = case.known_parsed_flight_id
        if flight_id is None:
            flight_id, err = self._parse_log(case, db, settings)
            if err:
                return BenchmarkResult(
                    case_id=case.case_id,
                    log_filename=case.log_filename,
                    passed=False,
                    score=0.0,
                    errors=[err],
                    elapsed_s=time.monotonic() - t0,
                )
        else:
            # Verify the flight is parsed and ready
            flt = db.get_flight(flight_id)
            if flt is None or flt.status != "ready":
                return BenchmarkResult(
                    case_id=case.case_id,
                    log_filename=case.log_filename,
                    passed=False,
                    score=0.0,
                    errors=[f"Flight {flight_id} not ready (status={getattr(flt,'status',None)})"],
                    elapsed_s=time.monotonic() - t0,
                )

        # ── Create investigation record ───────────────────────────────────────
        inv_record = db.create_investigation(
            flight_id=flight_id,
            query=f"Benchmark investigation: {case.description}",
        )
        investigation_id = str(inv_record.id)

        # ── Run investigation ─────────────────────────────────────────────────
        try:
            if self.mode == "stub":
                report_data, inv_errors = self._run_stub(
                    flight_id, investigation_id, store
                )
            else:
                report_data, inv_errors = self._run_live(
                    flight_id, investigation_id, store,
                    inference_overrides=self.inference_overrides,
                )
            errors.extend(inv_errors)
        except Exception as e:
            errors.append(str(e))
            return BenchmarkResult(
                case_id=case.case_id,
                log_filename=case.log_filename,
                passed=False,
                score=0.0,
                errors=errors,
                elapsed_s=time.monotonic() - t0,
            )

        elapsed = time.monotonic() - t0

        # ── Load report from disk ─────────────────────────────────────────────
        report_path = (
            settings.flights_storage
            / flight_id / "derived"
            / f"report_{investigation_id}.json"
        )
        if not report_path.exists():
            # Fallback: use in-memory report_data
            report = report_data or {}
        else:
            report = json.loads(report_path.read_text())

        # ── Score ─────────────────────────────────────────────────────────────
        result = self._score(case, report, errors, elapsed)
        return result

    # ── private helpers ───────────────────────────────────────────────────────

    def _parse_log(self, case, db, settings):
        from api.workers.tasks import parse_log_task

        log_path = self.logs_dir / case.log_filename
        if not log_path.exists():
            return None, f"Log not found: {log_path}"

        flight_id = str(uuid.uuid4())
        upload_dir = settings.raw_storage / flight_id
        upload_dir.mkdir(parents=True, exist_ok=True)
        dest = upload_dir / case.log_filename
        shutil.copy2(log_path, dest)

        db.create_flight(
            id=uuid.UUID(flight_id),
            sha256=f"bench-{case.case_id}",
            filename=case.log_filename,
            file_size=log_path.stat().st_size,
            raw_path=str(dest),
            status="uploaded",
        )
        result = parse_log_task.apply(args=[flight_id, str(dest)])
        if result.failed():
            return None, f"Parse failed: {result.traceback[:200]}"
        return flight_id, None

    def _run_stub(self, flight_id, investigation_id, store):
        from unittest.mock import patch
        from tests.integration.stub_llm import _make_stub_client
        from orchestrator.graph import InvestigationOrchestrator

        stub = _make_stub_client()
        with patch("llm.client._client", stub):
            orch = InvestigationOrchestrator(
                flight_id=flight_id,
                investigation_id=investigation_id,
                store=store,
            )
            final_state = orch.run(max_iterations=1)
        return final_state.get("final_report", {}), final_state.get("errors", [])

    def _run_live(self, flight_id, investigation_id, store, inference_overrides=None):
        """
        Run a live investigation.

        inference_overrides patches settings fields for the duration of this call,
        allowing the benchmark to target a specific inference configuration without
        changing the process-level .env file.  A new InferenceClient singleton is
        forced so provider construction picks up the patched settings.
        """
        from unittest.mock import patch
        from api.workers.tasks import run_investigation_task
        from config.settings import settings

        overrides = inference_overrides or {}
        patches = {f"config.settings.settings.{k}": v for k, v in overrides.items()}

        # Force a fresh InferenceClient so overrides take effect
        import llm.client as _llm_client

        def _run():
            result = run_investigation_task.apply(args=[
                investigation_id, flight_id,
                "Benchmark forensic investigation",
            ])
            if result.failed():
                return {}, [f"Investigation task failed: {result.traceback[:200]}"]
            report_path = (
                settings.flights_storage / flight_id / "derived"
                / f"report_{investigation_id}.json"
            )
            report = json.loads(report_path.read_text()) if report_path.exists() else {}
            return report, []

        if overrides:
            with patch.multiple("config.settings.settings", **overrides):
                _llm_client._client = None   # force new client with patched settings
                try:
                    return _run()
                finally:
                    _llm_client._client = None  # reset so next call re-builds cleanly
        else:
            return _run()

    def _score(
        self,
        case: BenchmarkCase,
        report: dict,
        errors: list[str],
        elapsed: float,
    ) -> BenchmarkResult:
        criteria: list[CriterionResult] = []

        classification = report.get("classification", "")
        confidence = report.get("confidence_level", "")
        root_cause = (report.get("root_cause_determination") or "").lower()
        registry = report.get("anomaly_registry", [])
        registry_rules = {e.get("rule_name", "") for e in registry}
        cf_list = report.get("contributing_factors", [])
        cf_text = " ".join(cf_list).lower()
        cf_evidence_ids: set[str] = set()
        for cf in cf_list:
            # Extract evidence IDs from serialized format "[evidence: ID1, ID2]"
            if "[evidence:" in cf:
                inner = cf.split("[evidence:")[1].split("]")[0]
                cf_evidence_ids.update(i.strip() for i in inner.split(","))

        # 1. Classification
        ok = classification == case.expected_classification
        criteria.append(CriterionResult(
            "classification_match",
            ok,
            f"got {classification!r}, expected {case.expected_classification!r}",
        ))

        # 2. Confidence sufficient
        ok = case.confidence_at_least(confidence)
        criteria.append(CriterionResult(
            "confidence_sufficient",
            ok,
            f"got {confidence!r}, min {case.min_confidence_level!r}",
        ))

        # 3. Required rule names in anomaly_registry (deterministic)
        missing_rules = [r for r in case.required_rule_names if r not in registry_rules]
        ok = len(missing_rules) == 0
        criteria.append(CriterionResult(
            "required_rules_present",
            ok,
            f"missing: {missing_rules}" if missing_rules
            else f"all {len(case.required_rule_names)} required rules present",
        ))

        # 4. Root cause contains acceptable keyword
        if case.acceptable_root_cause_keywords:
            matched = [kw for kw in case.acceptable_root_cause_keywords if kw in root_cause]
            ok = len(matched) > 0
            criteria.append(CriterionResult(
                "root_cause_keyword_present",
                ok,
                f"matched: {matched}" if matched
                else f"none of {case.acceptable_root_cause_keywords} in root cause",
            ))

        # 5. Required contributing evidence present
        if case.required_contributing_evidence:
            matched = [e for e in case.required_contributing_evidence
                       if e in cf_evidence_ids]
            ok = len(matched) > 0
            criteria.append(CriterionResult(
                "contributing_evidence_present",
                ok,
                f"matched: {matched}" if matched
                else f"none of {case.required_contributing_evidence[:3]}... in evidence",
            ))

        # 6. No forbidden terms in contributing factors
        if case.forbidden_contributing_terms:
            found_forbidden = [
                t for t in case.forbidden_contributing_terms if t.lower() in cf_text
            ]
            ok = len(found_forbidden) == 0
            criteria.append(CriterionResult(
                "no_forbidden_terms",
                ok,
                f"forbidden terms found: {found_forbidden}" if found_forbidden
                else "no forbidden terms in contributing factors",
            ))

        passed_count = sum(1 for c in criteria if c.passed)
        score = passed_count / len(criteria) if criteria else 0.0
        # Benchmark passes if all criteria pass (or no criteria defined)
        all_passed = all(c.passed for c in criteria)

        return BenchmarkResult(
            case_id=case.case_id,
            log_filename=case.log_filename,
            passed=all_passed,
            score=score,
            criteria=criteria,
            classification_actual=classification,
            confidence_actual=confidence,
            root_cause_actual=report.get("root_cause_determination"),
            contributing_factors_actual=cf_list,
            anomaly_rule_names_actual=sorted(registry_rules),
            elapsed_s=elapsed,
            errors=errors,
        )
