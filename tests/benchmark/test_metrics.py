"""
Unit tests for the BenchmarkMetrics aggregator.

Tests:
  1. Perfect accuracy → 100% correct, 0% FP/FN, exit criteria met
  2. All wrong → 0% accuracy, exit criteria fail
  3. Mixed results → correct partial rates
  4. Fabricated evidence detection via required_rules_present failure
  5. False positive / false negative rate calculation
  6. Criteria pass rates aggregation
  7. Empty result set handled gracefully
  8. Confidence calibration mean computed correctly
"""

from __future__ import annotations

import pytest

from tests.benchmark.corpus import BenchmarkCase
from tests.benchmark.metrics import compute_metrics
from tests.benchmark.runner import BenchmarkResult, CriterionResult


def _spec(case_id="test", classification="CRASH", min_conf="MEDIUM"):
    return BenchmarkCase(
        log_filename=f"{case_id}.BIN",
        case_id=case_id,
        description="test",
        expected_classification=classification,
        min_confidence_level=min_conf,
        required_rule_names=["TEST_RULE"],
    )


def _result(case_id="test", classification="CRASH", confidence="HIGH",
            passed=True, score=1.0, criteria=None, errors=None):
    return BenchmarkResult(
        case_id=case_id,
        log_filename=f"{case_id}.BIN",
        passed=passed,
        score=score,
        classification_actual=classification,
        confidence_actual=confidence,
        criteria=criteria or [
            CriterionResult("classification_match", passed, ""),
            CriterionResult("confidence_sufficient", True, ""),
            CriterionResult("required_rules_present", True, ""),
        ],
        errors=errors or [],
    )


def test_perfect_accuracy():
    specs = [_spec("a", "CRASH"), _spec("b", "ANOMALY")]
    results = [
        _result("a", "CRASH", passed=True, score=1.0),
        _result("b", "ANOMALY", passed=True, score=1.0),
    ]
    m = compute_metrics(results, specs)
    assert m.classification_accuracy == 1.0
    assert m.false_positive_rate == 0.0
    assert m.false_negative_rate == 0.0
    assert m.overall_pass_rate == 1.0
    passes, _ = m.passes_exit_criteria()
    assert passes


def test_all_wrong():
    specs = [_spec("a", "CRASH"), _spec("b", "ANOMALY")]
    results = [
        _result("a", "ANOMALY", passed=False, score=0.0),
        _result("b", "CRASH", passed=False, score=0.0),
    ]
    m = compute_metrics(results, specs)
    assert m.classification_accuracy == 0.0
    assert m.false_positive_rate == 1.0   # b: ANOMALY called CRASH
    assert m.false_negative_rate == 1.0   # a: CRASH called ANOMALY
    passes, failures = m.passes_exit_criteria()
    assert not passes
    assert any("classification_accuracy" in f for f in failures)
    assert any("false_negative" in f for f in failures)


def test_false_positive_rate():
    # 2 non-CRASH cases, 1 wrongly called CRASH
    specs = [_spec("a", "ANOMALY"), _spec("b", "ANOMALY"), _spec("c", "CRASH")]
    results = [
        _result("a", "CRASH", passed=False),   # FP
        _result("b", "ANOMALY", passed=True),
        _result("c", "CRASH", passed=True),
    ]
    m = compute_metrics(results, specs)
    assert m.false_positive_rate == pytest.approx(0.5)   # 1/2 non-CRASH
    assert m.false_negative_rate == 0.0


def test_false_negative_rate():
    # 2 CRASH cases, 1 missed
    specs = [_spec("a", "CRASH"), _spec("b", "CRASH"), _spec("c", "ANOMALY")]
    results = [
        _result("a", "ANOMALY", passed=False),   # FN
        _result("b", "CRASH", passed=True),
        _result("c", "ANOMALY", passed=True),
    ]
    m = compute_metrics(results, specs)
    assert m.false_negative_rate == pytest.approx(0.5)
    assert m.false_positive_rate == 0.0


def test_fabricated_evidence_detection():
    # required_rules_present failure → fabricated evidence counter
    criteria_with_missing_rules = [
        CriterionResult("classification_match", True, ""),
        CriterionResult("required_rules_present", False, "missing: ['REAL_RULE']"),
    ]
    specs = [_spec("a")]
    results = [_result("a", criteria=criteria_with_missing_rules, passed=False, score=0.5)]
    m = compute_metrics(results, specs)
    assert m.fabricated_evidence_rate == 1.0
    assert "a" in m.fabricated_evidence_cases
    passes, failures = m.passes_exit_criteria()
    assert not passes
    assert any("fabricated" in f for f in failures)


def test_no_fabricated_evidence_when_rules_present():
    specs = [_spec("a")]
    results = [_result("a", passed=True, score=1.0)]
    m = compute_metrics(results, specs)
    assert m.fabricated_evidence_rate == 0.0
    assert m.fabricated_evidence_cases == []


def test_criteria_pass_rates():
    criteria_a = [
        CriterionResult("classification_match", True, ""),
        CriterionResult("required_rules_present", True, ""),
    ]
    criteria_b = [
        CriterionResult("classification_match", False, ""),
        CriterionResult("required_rules_present", True, ""),
    ]
    specs = [_spec("a"), _spec("b")]
    results = [
        _result("a", criteria=criteria_a, passed=True, score=1.0),
        _result("b", criteria=criteria_b, passed=False, score=0.5),
    ]
    m = compute_metrics(results, specs)
    assert m.criteria_pass_rates["classification_match"] == pytest.approx(0.5)
    assert m.criteria_pass_rates["required_rules_present"] == pytest.approx(1.0)


def test_empty_results():
    m = compute_metrics([], [])
    assert m.total_cases == 0
    assert m.classification_accuracy == 0.0
    assert m.overall_pass_rate == 0.0


def test_confidence_calibration():
    # HIGH=2, MEDIUM=1 in _CONF_ORDER
    specs = [_spec("a", "CRASH"), _spec("b", "CRASH")]
    results = [
        _result("a", "CRASH", confidence="HIGH", passed=True),    # correct → rank 2
        _result("b", "ANOMALY", confidence="MEDIUM", passed=False), # wrong → rank 1
    ]
    m = compute_metrics(results, specs)
    assert m.mean_conf_correct == pytest.approx(2.0)
    assert m.mean_conf_incorrect == pytest.approx(1.0)


def test_mixed_three_cases():
    specs = [
        _spec("gps", "CRASH"),
        _spec("bat", "ANOMALY"),
        _spec("nominal", "NOMINAL"),
    ]
    results = [
        _result("gps", "CRASH", passed=True, score=1.0),
        _result("bat", "ANOMALY", passed=True, score=1.0),
        _result("nominal", "CRASH", passed=False, score=0.5),  # FP
    ]
    m = compute_metrics(results, specs)
    assert m.classification_accuracy == pytest.approx(2 / 3)
    assert m.false_positive_rate == pytest.approx(0.5)   # 1/2 non-CRASH
    assert m.false_negative_rate == 0.0
    assert m.passed_cases == 2
    assert m.failed_cases == 1
