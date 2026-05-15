#!/usr/bin/env python
"""
Phase 5 live benchmark runner — diagnostic accuracy validation.

Usage:
  python scripts/run_benchmark.py                   # all active cases, live mode
  python scripts/run_benchmark.py --stub            # stub mode (fast, structural only)
  python scripts/run_benchmark.py --case gps_crash_006
  python scripts/run_benchmark.py --output results.json

Output:
  - Per-case pass/fail with criterion detail
  - Aggregate metrics: classification accuracy, FP/FN rates, evidence integrity
  - Phase 5 exit criteria evaluation
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

# Project root on sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.benchmark.corpus import ACTIVE_CORPUS, CORPUS_BY_CASE_ID, BenchmarkCase
from tests.benchmark.metrics import BenchmarkMetrics, compute_metrics
from tests.benchmark.runner import BenchmarkResult, BenchmarkRunner


def run(
    cases: list[BenchmarkCase],
    mode: str,
    output_path: Path | None,
) -> BenchmarkMetrics:
    runner = BenchmarkRunner(mode=mode)
    results: list[BenchmarkResult] = []

    total = len(cases)
    for i, case in enumerate(cases, 1):
        print(f"\n[{i}/{total}] {case.case_id}  ({case.log_filename})")
        print(f"  expected: {case.expected_classification} / min {case.min_confidence_level}")
        t0 = time.monotonic()

        result = runner.run(case)

        elapsed = time.monotonic() - t0
        status = "PASS" if result.passed else "FAIL"
        print(f"  [{status}] score={result.score:.0%}  "
              f"actual={result.classification_actual}/{result.confidence_actual}  "
              f"{elapsed:.0f}s")
        for c in result.criteria:
            mark = "✓" if c.passed else "✗"
            print(f"    {mark} {c.name}: {c.detail}")
        if result.errors:
            for e in result.errors:
                print(f"    ! {e}")

        results.append(result)

    metrics = compute_metrics(results, cases)

    print("\n")
    print(metrics)

    if output_path:
        data = {
            "mode": mode,
            "cases": [
                {
                    "case_id": r.case_id,
                    "log_filename": r.log_filename,
                    "passed": r.passed,
                    "score": r.score,
                    "classification_actual": r.classification_actual,
                    "confidence_actual": r.confidence_actual,
                    "root_cause_actual": r.root_cause_actual,
                    "elapsed_s": r.elapsed_s,
                    "errors": r.errors,
                    "criteria": [
                        {"name": c.name, "passed": c.passed, "detail": c.detail}
                        for c in r.criteria
                    ],
                }
                for r in results
            ],
            "metrics": {
                "total_cases": metrics.total_cases,
                "passed_cases": metrics.passed_cases,
                "failed_cases": metrics.failed_cases,
                "error_cases": metrics.error_cases,
                "overall_pass_rate": metrics.overall_pass_rate,
                "classification_accuracy": metrics.classification_accuracy,
                "false_positive_rate": metrics.false_positive_rate,
                "false_negative_rate": metrics.false_negative_rate,
                "fabricated_evidence_rate": metrics.fabricated_evidence_rate,
                "fabricated_evidence_cases": metrics.fabricated_evidence_cases,
                "forbidden_terms_violations": metrics.forbidden_terms_violations,
                "criteria_pass_rates": metrics.criteria_pass_rates,
                "mean_conf_correct": metrics.mean_conf_correct,
                "mean_conf_incorrect": metrics.mean_conf_incorrect,
            },
        }
        output_path.write_text(json.dumps(data, indent=2))
        print(f"\nResults written to: {output_path}")

    return metrics


def main():
    parser = argparse.ArgumentParser(description="Phase 5 benchmark runner")
    parser.add_argument(
        "--stub", action="store_true",
        help="Use stub LLM (fast, structural only — skips diagnostic accuracy)"
    )
    parser.add_argument(
        "--case", metavar="CASE_ID", action="append", default=[],
        help="Run specific case(s) only (repeatable). Default: all active cases."
    )
    parser.add_argument(
        "--output", metavar="PATH", type=Path, default=None,
        help="Write JSON results to this file"
    )
    args = parser.parse_args()

    mode = "stub" if args.stub else "live"

    if args.case:
        cases = []
        for cid in args.case:
            if cid not in CORPUS_BY_CASE_ID:
                print(f"ERROR: unknown case_id {cid!r}", file=sys.stderr)
                print(f"Available: {sorted(CORPUS_BY_CASE_ID)}", file=sys.stderr)
                sys.exit(1)
            cases.append(CORPUS_BY_CASE_ID[cid])
    else:
        cases = ACTIVE_CORPUS

    print(f"Running {len(cases)} benchmark case(s) in {mode.upper()} mode")
    if mode == "live":
        print("NOTE: live mode calls real Ollama — expect ~25-90 min per case on CPU")
    print()

    metrics = run(cases, mode, args.output)
    passes, _ = metrics.passes_exit_criteria()
    sys.exit(0 if passes else 1)


if __name__ == "__main__":
    main()
