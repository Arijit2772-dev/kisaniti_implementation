"""FPO tenant table (shared/v1: canonical ID, Asia/Kolkata, provenance).

Revision ID: 0001_fpo_tenant
Revises:
"""

from alembic import op

revision = "0001_fpo_tenant"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        CREATE TABLE fpo (
            id          text PRIMARY KEY CHECK (id ~ '^fpo_[0-9A-HJKMNP-TV-Z]{26}$'),
            name        text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
            timezone    text NOT NULL CHECK (timezone = 'Asia/Kolkata'),
            origin      text NOT NULL CHECK (origin IN ('REAL', 'POLICY', 'SYNTHETIC')),
            provenance  jsonb NOT NULL,
            created_at  timestamptz NOT NULL DEFAULT now(),
            CHECK (origin <> 'SYNTHETIC' OR provenance ? 'generator')
        )
        """
    )
