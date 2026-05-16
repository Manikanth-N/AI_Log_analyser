"""Initial schema — all tables as deployed on AWS before Phase 6 (GCS migration).

Revision ID: 3a1f8c902d44
Revises:
Create Date: 2026-05-15

Operator note for existing production DBs:
  The AWS deployment was created via SQLAlchemy create_all() without Alembic.
  If this DB already has the tables, stamp it at this revision WITHOUT running
  the DDL:

    alembic stamp 3a1f8c902d44

  Then apply Phase 6 changes:

    alembic upgrade head

  For fresh installs, run normally:

    alembic upgrade head
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "3a1f8c902d44"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "flights",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("sha256", sa.String(64), unique=True, nullable=False),
        sa.Column("filename", sa.Text, nullable=False),
        sa.Column("file_size", sa.BigInteger),
        sa.Column("format", sa.String(32)),
        sa.Column("autopilot", sa.String(32)),
        sa.Column("vehicle_class", sa.String(32)),
        sa.Column("fw_version", sa.Text),
        sa.Column("uploaded_at", sa.DateTime),
        sa.Column("flight_start_time", sa.DateTime, nullable=True),
        sa.Column("duration_s", sa.Float, nullable=True),
        sa.Column("status", sa.String(32), server_default="uploaded"),
        sa.Column("message_types", sa.JSON),
        sa.Column("missing_critical", sa.JSON),
        sa.Column("parameter_count", sa.Integer, server_default="0"),
        sa.Column("raw_path", sa.Text, nullable=True),
    )

    op.create_table(
        "investigations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("flight_id", UUID(as_uuid=True), sa.ForeignKey("flights.id"), nullable=False),
        sa.Column("query", sa.Text, nullable=True),
        sa.Column("status", sa.String(32), server_default="queued"),
        sa.Column("started_at", sa.DateTime, nullable=True),
        sa.Column("completed_at", sa.DateTime, nullable=True),
        sa.Column("root_cause", sa.Text, nullable=True),
        sa.Column("contributing_factors", sa.JSON),
        sa.Column("recommendations", sa.JSON),
        sa.Column("confidence", sa.String(16), nullable=True),
        sa.Column("report_path", sa.Text, nullable=True),
        sa.Column("iteration_count", sa.Integer, server_default="0"),
        sa.Column("agent_findings", sa.JSON),
        sa.Column("open_questions", sa.JSON),
    )

    op.create_table(
        "hypotheses",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "investigation_id",
            UUID(as_uuid=True),
            sa.ForeignKey("investigations.id"),
            nullable=False,
        ),
        sa.Column("agent_name", sa.String(64)),
        sa.Column("text", sa.Text),
        sa.Column("confidence", sa.Float, server_default="0.0"),
        sa.Column("status", sa.String(32), server_default="forming"),
        sa.Column("evidence", sa.JSON),
        sa.Column("reasoning", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime),
        sa.Column("updated_at", sa.DateTime),
    )

    op.create_table(
        "anomalies",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("flight_id", UUID(as_uuid=True), sa.ForeignKey("flights.id"), nullable=False),
        sa.Column(
            "investigation_id",
            UUID(as_uuid=True),
            sa.ForeignKey("investigations.id"),
            nullable=True,
        ),
        sa.Column("timestamp_us", sa.BigInteger, nullable=False),
        sa.Column("end_timestamp_us", sa.BigInteger, nullable=True),
        sa.Column("severity", sa.String(16)),
        sa.Column("category", sa.String(32)),
        sa.Column("rule_name", sa.String(64)),
        sa.Column("description", sa.Text),
        sa.Column("raw_values", sa.JSON),
        sa.Column("detected_by", sa.String(64)),
        sa.Column("correlation_hint", sa.Text, nullable=True),
    )

    op.create_table(
        "baselines",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("vehicle_type", sa.String(64)),
        sa.Column("flight_id", UUID(as_uuid=True), sa.ForeignKey("flights.id"), nullable=True),
        sa.Column("phase_metrics", sa.JSON),
        sa.Column("embedding_path", sa.Text, nullable=True),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("is_healthy", sa.Boolean, server_default="true"),
        sa.Column("created_at", sa.DateTime),
    )


def downgrade() -> None:
    op.drop_table("baselines")
    op.drop_table("anomalies")
    op.drop_table("hypotheses")
    op.drop_table("investigations")
    op.drop_table("flights")
