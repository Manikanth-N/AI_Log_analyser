"""Safety Compliance Agent — checks regulatory/operational safety parameter compliance."""

from __future__ import annotations

import json
from pathlib import Path

import structlog

from agents.base import BaseAgent, make_hypothesis

InvestigationState = dict  # avoid circular import; state is typed as dict at runtime

log = structlog.get_logger(__name__)

_PROFILES_DIR = Path(__file__).parent.parent / "config" / "vehicle_profiles"

# Safety-critical parameters with hard limits (value must be != 0 / within range)
_SAFETY_CHECKS = [
    # (param_name, required_value_or_None, check_type, description)
    ("FS_THR_ENABLE", 1, "eq", "Throttle failsafe must be enabled"),
    ("FS_GCS_ENABLE", 1, "eq", "GCS heartbeat failsafe must be enabled"),
    ("FS_EKF_ACTION", None, "nonzero", "EKF failsafe action must be set"),
    ("GPS_HDOP_GOOD", 2.5, "lte", "GPS HDOP threshold must be ≤ 2.5"),
    ("FENCE_ENABLE", None, "any", "Geo-fence status documented"),
    ("BATT_LOW_VOLT", None, "nonzero", "Battery low voltage threshold must be set"),
    ("BATT_CRT_VOLT", None, "nonzero", "Battery critical voltage threshold must be set"),
    ("EK3_CHECK_SCALE", 1.0, "gte", "EKF innovation check scale must be ≥ 1.0"),
]


class SafetyComplianceAgent(BaseAgent):
    """Audits flight parameters against known safety requirements."""

    AGENT_NAME = "SafetyComplianceAgent"
    AGENT_ROLE = "[SAFETY] Regulatory Compliance Auditor"

    def run(self, state: InvestigationState) -> InvestigationState:
        self.emit(state, "Auditing safety parameter compliance")

        params = self._load_params()
        if not params:
            self.emit(state, "PARM messages not available — safety audit skipped")
            state["agent_findings"][self.AGENT_NAME] = {
                "summary": "Parameter data unavailable for compliance check",
                "violations": [],
                "warnings": [],
                "compliant_count": 0,
            }
            return state

        violations = []
        warnings = []
        compliant = []

        for param_name, required, check_type, description in _SAFETY_CHECKS:
            actual = params.get(param_name)
            if actual is None:
                warnings.append(f"{param_name}: not found in log (may be default)")
                continue

            passed = self._check(actual, required, check_type)
            if not passed:
                violations.append({
                    "parameter": param_name,
                    "actual": actual,
                    "required": required,
                    "check": check_type,
                    "description": description,
                })
            else:
                compliant.append(param_name)

        if violations:
            summary = (
                f"{len(violations)} safety parameter violation(s): "
                + ", ".join(v["parameter"] for v in violations)
            )
        else:
            summary = f"All {len(compliant)} checked safety parameters COMPLIANT"

        if warnings:
            summary += f"; {len(warnings)} parameter(s) not logged"

        self.emit(state, summary)

        if violations:
            v0 = violations[0]
            state.setdefault("hypotheses", []).append(make_hypothesis(
                title=f"Safety parameter non-compliance: {v0['parameter']}",
                description=(
                    f"{v0['parameter']} = {v0['actual']} but expected "
                    f"{v0['check']} {v0['required']}. "
                    f"{len(violations)} violation(s) total."
                ),
                agent_source=self.AGENT_NAME,
                confidence=0.60,
                status="forming",
                evidence_for=[v["description"] for v in violations],
                evidence_against=[],
                missing_evidence=["Full parameter history across flight"],
            ))

        state["agent_findings"][self.AGENT_NAME] = {
            "summary": summary,
            "violations": violations,
            "warnings": warnings,
            "compliant_count": len(compliant),
        }
        return state

    def _load_params(self) -> dict[str, float]:
        try:
            import polars as pl
            df = self.store.load(self.flight_id, "PARM")
            if df is None or df.is_empty():
                return {}
            name_col = "name" if "name" in df.columns else "param_id"
            val_col = "value" if "value" in df.columns else "param_value"
            return {
                row[name_col]: row[val_col]
                for row in df.select([name_col, val_col]).to_dicts()
                if row[name_col] and row[val_col] is not None
            }
        except Exception as e:
            log.warning("param_load_failed", error=str(e))
            return {}

    @staticmethod
    def _check(actual: float, required, check_type: str) -> bool:
        if check_type == "any":
            return True
        if check_type == "nonzero":
            return actual != 0
        if check_type == "eq":
            return actual == required
        if check_type == "lte":
            return actual <= required
        if check_type == "gte":
            return actual >= required
        return True
