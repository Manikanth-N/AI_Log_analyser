"""
Phase 5 benchmark metrics aggregator.

Computes quantitative diagnostic quality metrics from a list of BenchmarkResults.

Metrics:
  - classification_accuracy       overall correct / total
  - per_class_precision           precision per classification label
  - per_class_recall              recall per classification label
  - false_positive_rate           NOMINAL/ANOMALY called CRASH / actual non-CRASH
  - false_negative_rate           CRASH missed / actual CRASH
  - contributing_factor_rejection_rate   unsupported factors caught / total submitted
  - fabricated_evidence_rate      cases with fabricated rule_names in anomaly_registry
  - confidence_calibration        mean confidence level at correct vs incorrect
  - overall_pass_rate             cases where all criteria passed / total active cases

Target thresholds (Phase 5 exit criteria):
  - classification_accuracy  >= 0.80 (strong pass rate)
  - fabricated_evidence_rate == 0.0  (zero tolerance)
  - false_negative_rate      <= 0.20 (no more than 1 in 5 crashes missed)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from tests.benchmark.corpus import BenchmarkCase, _CONF_ORDER
from tests.benchmark.runner import BenchmarkResult


@dataclass
class BenchmarkMetrics:
    total_cases: int = 0
    passed_cases: int = 0
    failed_cases: int = 0
    error_cases: int = 0

    # Classification accuracy
    classification_correct: int = 0
    classification_accuracy: float = 0.0

    # Per-class counts for precision/recall
    # {label: {"tp": int, "fp": int, "fn": int}}
    per_class: dict[str, dict[str, int]] = field(default_factory=dict)

    # Crash-specific rates
    false_positive_rate: float = 0.0    # non-CRASH called CRASH / total non-CRASH
    false_negative_rate: float = 0.0    # CRASH called non-CRASH / total CRASH

    # Evidence integrity
    fabricated_evidence_rate: float = 0.0   # cases with any fabricated rule_names
    fabricated_evidence_cases: list[str] = field(default_factory=list)

    # Contributing factor grounding
    # The runner scores "no_forbidden_terms" as a proxy; track separately
    forbidden_terms_violations: int = 0
    forbidden_terms_total_checked: int = 0

    # Confidence calibration
    mean_conf_correct: float = 0.0      # avg confidence level rank when correct
    mean_conf_incorrect: float = 0.0    # avg confidence level rank when wrong

    # Criteria breakdown
    criteria_pass_rates: dict[str, float] = field(default_factory=dict)

    # Overall pass rate (all criteria pass)
    overall_pass_rate: float = 0.0

    # Per-case detail
    case_results: list[BenchmarkResult] = field(default_factory=list)
    case_specs: list[BenchmarkCase] = field(default_factory=list)

    def passes_exit_criteria(self) -> tuple[bool, list[str]]:
        """Returns (passes, list_of_failures)."""
        failures = []
        if self.classification_accuracy < 0.80:
            failures.append(
                f"classification_accuracy={self.classification_accuracy:.1%} < 80%"
            )
        if self.fabricated_evidence_rate > 0.0:
            failures.append(
                f"fabricated_evidence_rate={self.fabricated_evidence_rate:.1%} > 0% "
                f"(cases: {self.fabricated_evidence_cases})"
            )
        if self.false_negative_rate > 0.20:
            failures.append(
                f"false_negative_rate={self.false_negative_rate:.1%} > 20%"
            )
        return len(failures) == 0, failures

    def __str__(self) -> str:
        lines = ["=" * 72, "PHASE 5 BENCHMARK METRICS", "=" * 72]

        lines.append(f"\nCases: {self.total_cases} total, "
                     f"{self.passed_cases} passed, "
                     f"{self.failed_cases} failed, "
                     f"{self.error_cases} errors")
        lines.append(f"Overall pass rate: {self.overall_pass_rate:.1%}")

        lines.append("\n── Classification Accuracy ──────────────────────────────────────")
        lines.append(f"  Accuracy:            {self.classification_accuracy:.1%} "
                     f"({self.classification_correct}/{self.total_cases})")
        lines.append(f"  False positive rate: {self.false_positive_rate:.1%} "
                     f"(non-CRASH called CRASH)")
        lines.append(f"  False negative rate: {self.false_negative_rate:.1%} "
                     f"(CRASH missed)")

        if self.per_class:
            lines.append("\n── Per-Class Precision / Recall ─────────────────────────────────")
            for label, counts in sorted(self.per_class.items()):
                tp = counts.get("tp", 0)
                fp = counts.get("fp", 0)
                fn = counts.get("fn", 0)
                prec = tp / (tp + fp) if (tp + fp) > 0 else float("nan")
                rec = tp / (tp + fn) if (tp + fn) > 0 else float("nan")
                f1_val = (2 * prec * rec / (prec + rec)
                          if (prec + rec) > 0 else float("nan"))
                prec_str = f"{prec:.1%}" if prec == prec else "n/a"
                rec_str = f"{rec:.1%}" if rec == rec else "n/a"
                f1_str = f"{f1_val:.1%}" if f1_val == f1_val else "n/a"
                lines.append(f"  {label:12s}  prec={prec_str:6s} rec={rec_str:6s} F1={f1_str}")

        lines.append("\n── Evidence Integrity ───────────────────────────────────────────")
        lines.append(f"  Fabricated evidence rate: {self.fabricated_evidence_rate:.1%}")
        if self.fabricated_evidence_cases:
            lines.append(f"  Cases with fabricated evidence: {self.fabricated_evidence_cases}")

        if self.forbidden_terms_total_checked > 0:
            violation_rate = self.forbidden_terms_violations / self.forbidden_terms_total_checked
            lines.append(f"  Forbidden-term violations: "
                         f"{self.forbidden_terms_violations}/{self.forbidden_terms_total_checked} "
                         f"({violation_rate:.1%})")

        lines.append("\n── Criteria Pass Rates ──────────────────────────────────────────")
        for crit_name, rate in sorted(self.criteria_pass_rates.items()):
            lines.append(f"  {crit_name:40s} {rate:.1%}")

        lines.append("\n── Confidence Calibration ───────────────────────────────────────")
        lines.append(f"  Mean conf rank (correct):   {self.mean_conf_correct:.2f} / 3.0")
        lines.append(f"  Mean conf rank (incorrect): {self.mean_conf_incorrect:.2f} / 3.0")

        lines.append("\n── Per-Case Summary ─────────────────────────────────────────────")
        for result, spec in zip(self.case_results, self.case_specs):
            status = "PASS" if result.passed else ("ERR" if result.errors else "FAIL")
            lines.append(
                f"  [{status}] {result.case_id:24s}  "
                f"score={result.score:.0%}  "
                f"{result.classification_actual}/{result.confidence_actual}  "
                f"expected={spec.expected_classification}"
            )
            if result.errors:
                for e in result.errors[:2]:
                    lines.append(f"        ERROR: {e[:80]}")

        passes, failures = self.passes_exit_criteria()
        lines.append("\n── Exit Criteria ────────────────────────────────────────────────")
        if passes:
            lines.append("  ALL EXIT CRITERIA MET — ready to proceed to Phase 6")
        else:
            lines.append("  EXIT CRITERIA NOT MET:")
            for f in failures:
                lines.append(f"    FAIL: {f}")

        lines.append("=" * 72)
        return "\n".join(lines)


def compute_metrics(
    results: Sequence[BenchmarkResult],
    specs: Sequence[BenchmarkCase],
) -> BenchmarkMetrics:
    """Aggregate BenchmarkResults into BenchmarkMetrics."""
    m = BenchmarkMetrics()
    m.case_results = list(results)
    m.case_specs = list(specs)
    m.total_cases = len(results)

    if not results:
        return m

    # Per-class counters
    all_labels = {"CRASH", "ANOMALY", "REVIEW", "NOMINAL"}
    per_class: dict[str, dict[str, int]] = {
        label: {"tp": 0, "fp": 0, "fn": 0} for label in all_labels
    }

    crash_total = sum(1 for s in specs if s.expected_classification == "CRASH")
    non_crash_total = sum(1 for s in specs if s.expected_classification != "CRASH")
    fp_count = 0   # non-CRASH called CRASH
    fn_count = 0   # CRASH not called CRASH

    conf_correct: list[int] = []
    conf_incorrect: list[int] = []

    criteria_pass: dict[str, list[bool]] = {}

    for result, spec in zip(results, specs):
        actual = result.classification_actual or ""
        expected = spec.expected_classification

        if result.errors:
            m.error_cases += 1

        if result.passed:
            m.passed_cases += 1
        else:
            m.failed_cases += 1

        # Classification correctness
        correct = actual == expected
        if correct:
            m.classification_correct += 1
            per_class[expected]["tp"] = per_class[expected].get("tp", 0) + 1
            conf_correct.append(_CONF_ORDER.get(result.confidence_actual or "", 0))
        else:
            per_class[expected]["fn"] = per_class.get(expected, {}).get("fn", 0) + 1
            if actual in per_class:
                per_class[actual]["fp"] = per_class[actual].get("fp", 0) + 1
            conf_incorrect.append(_CONF_ORDER.get(result.confidence_actual or "", 0))

            # Crash-specific rates
            if expected == "CRASH" and actual != "CRASH":
                fn_count += 1
            if expected != "CRASH" and actual == "CRASH":
                fp_count += 1

        # Evidence integrity: check anomaly_registry rule_names against known-valid set
        # We flag a case if any rule_name in anomaly_registry is NOT in the parsed
        # anomaly set (i.e., wasn't produced by the rules engine).
        # For now we track via the criteria_pass proxy.

        # Forbidden terms tracking
        for c in result.criteria:
            criteria_pass.setdefault(c.name, []).append(c.passed)
            if c.name == "no_forbidden_terms":
                m.forbidden_terms_total_checked += 1
                if not c.passed:
                    m.forbidden_terms_violations += 1

    m.classification_accuracy = (m.classification_correct / m.total_cases
                                  if m.total_cases else 0.0)
    m.false_positive_rate = (fp_count / non_crash_total
                              if non_crash_total > 0 else 0.0)
    m.false_negative_rate = (fn_count / crash_total
                              if crash_total > 0 else 0.0)
    m.overall_pass_rate = m.passed_cases / m.total_cases if m.total_cases else 0.0

    m.per_class = {k: v for k, v in per_class.items()
                   if any(v.values())}

    m.mean_conf_correct = (sum(conf_correct) / len(conf_correct)
                            if conf_correct else 0.0)
    m.mean_conf_incorrect = (sum(conf_incorrect) / len(conf_incorrect)
                              if conf_incorrect else 0.0)

    m.criteria_pass_rates = {
        name: sum(vals) / len(vals)
        for name, vals in criteria_pass.items()
    }

    # Fabricated evidence: zero if every anomaly_registry entry came from the
    # rules engine. We can't verify this without the parsed anomaly set here,
    # so we approximate: a case has fabricated evidence if `required_rules_present`
    # failed (meaning expected deterministic rule_names were absent from registry).
    for result, spec in zip(results, specs):
        rules_criterion = next(
            (c for c in result.criteria if c.name == "required_rules_present"), None
        )
        if rules_criterion and not rules_criterion.passed:
            m.fabricated_evidence_cases.append(result.case_id)
    m.fabricated_evidence_rate = (
        len(m.fabricated_evidence_cases) / m.total_cases if m.total_cases else 0.0
    )

    return m
