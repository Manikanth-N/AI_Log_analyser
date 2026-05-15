"""
Qdrant vector store for baseline comparison and semantic evidence search.

Version compatibility note:
  qdrant-client >= 1.14 uses query_points() (server >= 1.10 required).
  If the server is older, search operations return empty results with a warning.
  Upgrade the Qdrant server to 1.10+ to enable baseline comparison.
  Phase 7 infrastructure task: docker compose with qdrant:v1.10+
"""

import uuid
from dataclasses import dataclass

import numpy as np
import structlog
from qdrant_client import QdrantClient
from qdrant_client.http import models as qm
from qdrant_client.http.models import Distance, VectorParams

from config.settings import settings

log = structlog.get_logger(__name__)

EMBEDDING_DIM = 768  # nomic-embed-text:v1.5

_QDRANT_UNAVAILABLE_MSG = (
    "Qdrant server version incompatible with client — upgrade server to 1.10+. "
    "Baseline comparison disabled."
)


@dataclass
class BaselineRecord:
    flight_id: str
    vehicle_type: str
    phase: str
    metrics: dict
    is_healthy: bool
    notes: str = ""


class VectorStore:
    def __init__(self, url: str | None = None):
        self.client = QdrantClient(
            url=url or settings.qdrant_url,
            timeout=30,
            check_compatibility=False,   # suppress version mismatch warning in logs
        )
        self._server_compatible: bool | None = None
        self._ensure_collections()

    def is_available(self) -> bool:
        """Returns True if the Qdrant server supports the current client API."""
        if self._server_compatible is not None:
            return self._server_compatible
        try:
            # query_points is the minimum required endpoint (server >= 1.10)
            self.client.query_points(
                collection_name="__probe__",
                query=[0.0] * EMBEDDING_DIM,
                limit=1,
            )
            self._server_compatible = True
        except Exception as e:
            msg = str(e)
            # 404 = endpoint not found (old server), not found = collection missing
            if "404" in msg or "Not Found" in msg or "collection" in msg.lower():
                self._server_compatible = "404" not in msg or "collection" in msg.lower()
            else:
                self._server_compatible = False
            if not self._server_compatible:
                log.warning("qdrant_incompatible", error=_QDRANT_UNAVAILABLE_MSG)
        return bool(self._server_compatible)

    def _ensure_collections(self):
        existing = {c.name for c in self.client.get_collections().collections}

        if settings.qdrant_collection_baselines not in existing:
            self.client.create_collection(
                collection_name=settings.qdrant_collection_baselines,
                vectors_config=VectorParams(size=EMBEDDING_DIM, distance=Distance.COSINE),
            )

        if settings.qdrant_collection_evidence not in existing:
            self.client.create_collection(
                collection_name=settings.qdrant_collection_evidence,
                vectors_config=VectorParams(size=EMBEDDING_DIM, distance=Distance.COSINE),
            )

    def upsert_baseline(
        self,
        embedding: np.ndarray,
        record: BaselineRecord,
    ) -> str:
        point_id = str(uuid.uuid4())
        self.client.upsert(
            collection_name=settings.qdrant_collection_baselines,
            points=[
                qm.PointStruct(
                    id=point_id,
                    vector=embedding.tolist(),
                    payload={
                        "flight_id": record.flight_id,
                        "vehicle_type": record.vehicle_type,
                        "phase": record.phase,
                        "metrics": record.metrics,
                        "is_healthy": record.is_healthy,
                        "notes": record.notes,
                    },
                )
            ],
        )
        return point_id

    def search_baselines(
        self,
        query_embedding: np.ndarray,
        vehicle_type: str | None = None,
        phase: str | None = None,
        top_k: int = 5,
        healthy_only: bool = True,
    ) -> list[dict]:
        filters = []
        if vehicle_type:
            filters.append(qm.FieldCondition(
                key="vehicle_type",
                match=qm.MatchValue(value=vehicle_type),
            ))
        if phase:
            filters.append(qm.FieldCondition(
                key="phase",
                match=qm.MatchValue(value=phase),
            ))
        if healthy_only:
            filters.append(qm.FieldCondition(
                key="is_healthy",
                match=qm.MatchValue(value=True),
            ))

        query_filter = qm.Filter(must=filters) if filters else None

        try:
            response = self.client.query_points(
                collection_name=settings.qdrant_collection_baselines,
                query=query_embedding.tolist(),
                query_filter=query_filter,
                limit=top_k,
                with_payload=True,
            )
        except Exception:
            log.warning("qdrant_search_failed", reason=_QDRANT_UNAVAILABLE_MSG)
            return []

        return [
            {
                "id": str(r.id),
                "score": r.score,
                "flight_id": r.payload.get("flight_id"),
                "vehicle_type": r.payload.get("vehicle_type"),
                "phase": r.payload.get("phase"),
                "metrics": r.payload.get("metrics", {}),
                "is_healthy": r.payload.get("is_healthy"),
            }
            for r in response.points
        ]

    def upsert_evidence(
        self,
        embedding: np.ndarray,
        investigation_id: str,
        evidence_type: str,
        text: str,
        metadata: dict | None = None,
    ) -> str:
        point_id = str(uuid.uuid4())
        self.client.upsert(
            collection_name=settings.qdrant_collection_evidence,
            points=[
                qm.PointStruct(
                    id=point_id,
                    vector=embedding.tolist(),
                    payload={
                        "investigation_id": investigation_id,
                        "evidence_type": evidence_type,
                        "text": text,
                        **(metadata or {}),
                    },
                )
            ],
        )
        return point_id

    def search_evidence(
        self,
        query_embedding: np.ndarray,
        investigation_id: str | None = None,
        top_k: int = 5,
    ) -> list[dict]:
        filters = []
        if investigation_id:
            filters.append(qm.FieldCondition(
                key="investigation_id",
                match=qm.MatchValue(value=investigation_id),
            ))

        try:
            response = self.client.query_points(
                collection_name=settings.qdrant_collection_evidence,
                query=query_embedding.tolist(),
                query_filter=qm.Filter(must=filters) if filters else None,
                limit=top_k,
                with_payload=True,
            )
        except Exception:
            log.warning("qdrant_search_failed", reason=_QDRANT_UNAVAILABLE_MSG)
            return []

        return [
            {
                "id": str(r.id),
                "score": r.score,
                "text": r.payload.get("text", ""),
                "evidence_type": r.payload.get("evidence_type", ""),
                **{k: v for k, v in r.payload.items() if k not in ("text", "evidence_type")},
            }
            for r in response.points
        ]

    def compare_to_baselines(
        self,
        current_metrics: dict,
        current_embedding: np.ndarray,
        vehicle_type: str,
        phase: str,
    ) -> dict:
        """
        Compare current flight metrics to healthy baselines.
        Returns per-metric deviation analysis.
        """
        similar = self.search_baselines(
            query_embedding=current_embedding,
            vehicle_type=vehicle_type,
            phase=phase,
            top_k=10,
        )

        if not similar:
            return {"error": "No baseline flights found for comparison", "similar_flights": []}

        # Aggregate baseline statistics per metric
        baseline_values: dict[str, list[float]] = {}
        for r in similar:
            for k, v in r.get("metrics", {}).items():
                if isinstance(v, (int, float)):
                    baseline_values.setdefault(k, []).append(float(v))

        deviations = []
        for metric, current_val in current_metrics.items():
            if metric not in baseline_values or not isinstance(current_val, (int, float)):
                continue
            bl_vals = np.array(baseline_values[metric])
            bl_mean = float(np.mean(bl_vals))
            bl_std = float(np.std(bl_vals)) + 1e-10
            z_score = (float(current_val) - bl_mean) / bl_std

            deviations.append({
                "metric": metric,
                "current_value": float(current_val),
                "baseline_mean": bl_mean,
                "baseline_std": float(np.std(bl_vals)),
                "z_score": float(z_score),
                "is_anomalous": abs(z_score) > 2.5,
                "direction": "HIGH" if z_score > 0 else "LOW",
            })

        deviations.sort(key=lambda d: abs(d["z_score"]), reverse=True)

        return {
            "similar_flights": [r["flight_id"] for r in similar[:5]],
            "similarity_scores": [r["score"] for r in similar[:5]],
            "n_baselines_compared": len(similar),
            "metric_deviations": deviations,
            "anomalous_metrics": [d for d in deviations if d["is_anomalous"]],
        }
