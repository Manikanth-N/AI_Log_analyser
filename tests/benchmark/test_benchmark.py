"""
Benchmark test suite — Phase 4A diagnostic accuracy gate.

Runs each active BenchmarkCase in stub mode (StubOllamaClient, no Ollama needed)
and asserts all six grounding/accuracy criteria pass.

Stub mode validates:
  - structural correctness (schema, field types)
  - classification and confidence labels
  - anomaly_registry populated from deterministic detector output
  - contributing_factors evidence grounding (no hallucinated rule_names)
  - no forbidden terms in contributing_factors unless backed by evidence

Live mode (--benchmark-live) uses real Ollama; slow (~25-90 min per case).

Usage:
  pytest tests/benchmark/test_benchmark.py -v                   # stub mode
  pytest tests/benchmark/test_benchmark.py --benchmark-live -v  # live mode
  pytest tests/benchmark/ -v -m benchmark                       # all benchmark tests
"""

from __future__ import annotations

import pytest

from tests.benchmark.corpus import ACTIVE_CORPUS, BenchmarkCase
from tests.benchmark.runner import BenchmarkResult, BenchmarkRunner


# ── CLI option ────────────────────────────────────────────────────────────────

def pytest_addoption(parser):
    parser.addoption(
        "--benchmark-live",
        action="store_true",
        default=False,
        help="Run benchmarks against real Ollama (slow, full diagnostic accuracy test)",
    )


# ── Parametrized suite ────────────────────────────────────────────────────────

def _case_id(case: BenchmarkCase) -> str:
    return case.case_id


@pytest.mark.benchmark
@pytest.mark.parametrize("case", ACTIVE_CORPUS, ids=_case_id)
def test_benchmark_case(case: BenchmarkCase, request):
    live = request.config.getoption("--benchmark-live", default=False)
    mode = "live" if live else "stub"

    if not live and case.case_id != "gps_crash_006":
        # StubOllamaClient is hard-coded for the gps_crash_006 scenario (GPS failure
        # → EKF divergence → CRASH/HIGH). All other cases — including other CRASH
        # types — require real Ollama to validate diagnostic accuracy.
        pytest.skip(
            f"Stub is programmed for gps_crash_006 only; "
            f"full gate for {case.case_id!r} requires --benchmark-live"
        )

    runner = BenchmarkRunner(mode=mode)
    result: BenchmarkResult = runner.run(case)

    # Print full result on failure for diagnosis
    if not result.passed:
        print(f"\n{result}")

    assert result.passed, (
        f"Benchmark {case.case_id!r} failed (score={result.score:.0%}):\n{result}"
    )


# ── Per-criterion smoke tests (stub mode, always fast) ───────────────────────

@pytest.mark.benchmark
@pytest.mark.parametrize("case", ACTIVE_CORPUS, ids=_case_id)
def test_classification_match(case: BenchmarkCase, request):
    live = request.config.getoption("--benchmark-live", default=False)
    if not live and case.case_id != "gps_crash_006":
        pytest.skip("Stub programmed for gps_crash_006 only; requires --benchmark-live")
    runner = BenchmarkRunner(mode="stub" if not live else "live")
    result = runner.run(case)
    criterion = next(
        (c for c in result.criteria if c.name == "classification_match"), None
    )
    assert criterion is not None, "classification_match criterion missing"
    assert criterion.passed, (
        f"{case.case_id}: classification mismatch — {criterion.detail}"
    )


@pytest.mark.benchmark
@pytest.mark.parametrize("case", ACTIVE_CORPUS, ids=_case_id)
def test_confidence_sufficient(case: BenchmarkCase):
    runner = BenchmarkRunner(mode="stub")
    result = runner.run(case)
    criterion = next(
        (c for c in result.criteria if c.name == "confidence_sufficient"), None
    )
    assert criterion is not None, "confidence_sufficient criterion missing"
    assert criterion.passed, (
        f"{case.case_id}: confidence below threshold — {criterion.detail}"
    )


@pytest.mark.benchmark
@pytest.mark.parametrize("case", ACTIVE_CORPUS, ids=_case_id)
def test_required_rules_present(case: BenchmarkCase):
    if not case.required_rule_names:
        pytest.skip("No required_rule_names defined for this case")
    runner = BenchmarkRunner(mode="stub")
    result = runner.run(case)
    criterion = next(
        (c for c in result.criteria if c.name == "required_rules_present"), None
    )
    assert criterion is not None, "required_rules_present criterion missing"
    assert criterion.passed, (
        f"{case.case_id}: missing required rules — {criterion.detail}"
    )


@pytest.mark.benchmark
@pytest.mark.parametrize("case", ACTIVE_CORPUS, ids=_case_id)
def test_no_forbidden_terms(case: BenchmarkCase, request):
    if not case.forbidden_contributing_terms:
        pytest.skip("No forbidden_contributing_terms defined for this case")
    live = request.config.getoption("--benchmark-live", default=False)
    if not live and case.case_id != "gps_crash_006":
        pytest.skip("Stub programmed for gps_crash_006 only; requires --benchmark-live")
    runner = BenchmarkRunner(mode="stub" if not live else "live")
    result = runner.run(case)
    criterion = next(
        (c for c in result.criteria if c.name == "no_forbidden_terms"), None
    )
    assert criterion is not None, "no_forbidden_terms criterion missing"
    assert criterion.passed, (
        f"{case.case_id}: forbidden terms in contributing_factors — {criterion.detail}"
    )


# ── Structural smoke test (no ground truth needed) ───────────────────────────

@pytest.mark.benchmark
@pytest.mark.parametrize("case", ACTIVE_CORPUS, ids=_case_id)
def test_result_structure(case: BenchmarkCase):
    """Report structure sanity check — fields populated, score in range."""
    runner = BenchmarkRunner(mode="stub")
    result = runner.run(case)

    assert result.case_id == case.case_id
    assert result.log_filename == case.log_filename
    assert 0.0 <= result.score <= 1.0
    assert result.classification_actual is not None, "classification_actual is None"
    assert result.confidence_actual is not None, "confidence_actual is None"
    assert result.elapsed_s >= 0.0
    assert len(result.criteria) > 0, "No criteria evaluated"
    assert not result.errors, f"Runner errors: {result.errors}"
