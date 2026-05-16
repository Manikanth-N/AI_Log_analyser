"""Phase 6 GCS migration — add gcs_raw_uri, make sha256 nullable.

Revision ID: 7e4b9d1f5c28
Revises: 3a1f8c902d44
Create Date: 2026-05-15

Changes:
  - flights.gcs_raw_uri TEXT  (new column — GCS object URI for raw log)
  - flights.sha256             nullable=True  (GCS flow computes sha256 in worker,
                               not at upload time; pending_upload rows have no hash yet)

Downgrade removes gcs_raw_uri and restores NOT NULL on sha256.
Warning: downgrade will fail if any sha256 is NULL at the time of rollback.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "7e4b9d1f5c28"
down_revision: Union[str, None] = "3a1f8c902d44"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("flights", sa.Column("gcs_raw_uri", sa.Text, nullable=True))
    op.alter_column("flights", "sha256", existing_type=sa.String(64), nullable=True)


def downgrade() -> None:
    # Restoring NOT NULL will fail if any row has sha256 IS NULL.
    # Backfill or delete those rows before running this downgrade.
    op.alter_column("flights", "sha256", existing_type=sa.String(64), nullable=False)
    op.drop_column("flights", "gcs_raw_uri")
